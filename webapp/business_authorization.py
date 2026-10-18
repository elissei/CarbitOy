"""Auth reservation held until the independent business transaction completes.

Lock order is always auth -> business. This guard writes no auth data, so it
needs no distributed commit. It orders auth mutations against business commit
using SQLite's real writer reservation, including across processes and in WAL.
Never hold this guard across HTTP, external I/O or response generation.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from webapp.auth_models import UserRole
from webapp.auth_service import AuthService
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, permissions_for_role


TRANSACTION_SECONDS = 5.0


@dataclass(frozen=True)
class WorkshopAuthBinding:
    """Server-only reference, created after real token validation; no bearer secret."""
    service: AuthService = field(repr=False, compare=False)
    session_id: str = field(repr=False)


class AuthorizationReservation:
    def __init__(self, binding, connection, context, permission):
        self.binding = binding
        self.connection = connection
        self.context = context
        self.permission = permission
        self.deadline = monotonic() + TRANSACTION_SECONDS

    def validate(self):
        if (not self.connection.in_transaction or self.connection.total_changes != 0
                or self.connection.execute('PRAGMA query_only').fetchone()[0] != 1):
            raise sqlite3.OperationalError('auth reservation is not an active read-only transaction')
        if monotonic() >= self.deadline:
            raise sqlite3.OperationalError("business authorization deadline exceeded")
        service = self.binding.service
        session = self.connection.execute('SELECT * FROM auth_sessions WHERE session_id=?',
                                          (self.binding.session_id,)).fetchone()
        _, user = service._validate_session_row(self.connection, session, service._now())
        if (user['user_id'] != self.context.employee_id
                or service.repository.store_id(self.connection) != self.context.auth_store_id
                or self.permission not in permissions_for_role(UserRole(user['role']))):
            raise WorkshopAccessDenied('workshop access denied')

    def limit_business_connection(self, connection):
        remaining_ms = max(1, int((self.deadline - monotonic()) * 1000))
        connection.execute(f'PRAGMA busy_timeout={remaining_ms}')
        connection.set_progress_handler(lambda: int(monotonic() >= self.deadline), 1000)


@contextmanager
def reserve_business_authorization(context, permission, business_path):
    if not isinstance(context, TrustedWorkshopContext):
        raise WorkshopAccessDenied('workshop access denied')
    binding = context.auth_binding
    if binding is None:
        if context.actor is not None:
            raise WorkshopAccessDenied('authenticated operations require an auth binding')
        # Explicit legacy offline scopes remain trusted server calls, never an
        # HTTP option. Production request contexts always carry a real binding.
        yield None
        return
    if not isinstance(binding, WorkshopAuthBinding) or not isinstance(binding.service, AuthService):
        raise WorkshopAccessDenied('workshop access denied')
    auth_path = Path(binding.service.repository.database_path)
    work_path = Path(business_path)
    if (not auth_path.is_file() or auth_path.resolve() == work_path.resolve()
            or (work_path.exists() and auth_path.samefile(work_path))):
        raise sqlite3.OperationalError('distinct existing auth and business databases required')
    with binding.service.repository._connect() as connection:
        try:
            # The existing auth connection has a finite five-second busy timeout.
            connection.execute('BEGIN IMMEDIATE')
            # Keep the acquired writer reservation, but prohibit durable auth
            # writes on this connection. Business commit has no second DB commit.
            connection.execute('PRAGMA query_only=ON')
            reservation = AuthorizationReservation(binding, connection, context, permission)
            reservation.validate()
            yield reservation
        finally:
            # No auth mutation, idle update, audit write or second durable commit.
            connection.rollback()
