"""Business-DB membership checks and trusted offline provisioning, no HTTP admin."""

import json
from datetime import datetime, timezone
from uuid import UUID
from pathlib import Path

from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied


def audit(connection, operation, operator_reference, details):
    if not isinstance(operator_reference, str) or not 1 <= len(operator_reference.strip()) <= 500:
        raise ValueError("a bounded offline operator/approval reference is required")
    connection.execute("""INSERT INTO workshop_audit_events
        (occurred_at,operation,operator_reference,details_json) VALUES (?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(), operation, operator_reference,
         json.dumps(details, sort_keys=True)))


def active_membership(connection, auth_store_id, user_id):
    """Explicit server selection, or the sole currently usable membership."""
    rows = connection.execute("""
        SELECT m.workshop_id, w.organization_id, m.version
        FROM workshop_memberships m JOIN workshops w ON w.workshop_id=m.workshop_id
        JOIN repair_organizations o ON o.organization_id=w.organization_id
        WHERE m.auth_store_id=? AND m.user_id=? AND m.enabled=1
          AND w.status='ACTIVE' AND o.enabled=1
    """, (auth_store_id, user_id)).fetchall()
    selected = connection.execute("""SELECT workshop_id FROM active_user_workshops
        WHERE auth_store_id=? AND user_id=?""", (auth_store_id, user_id)).fetchone()
    if selected is not None:
        rows = [row for row in rows if row["workshop_id"] == selected[0]]
    if len(rows) != 1:
        raise WorkshopAccessDenied("workshop access denied")
    return rows[0]


def authorize_workshop(connection, context, permission):
    """Membership half of authorization; callers use WorkRepository.operation.

    Auth validity is protected by that operation's outer auth reservation. This
    function alone authorizes neither a session nor a global role snapshot.
    """
    if not connection.in_transaction:
        raise RuntimeError("workshop authorization requires an operation transaction")
    if not isinstance(context, TrustedWorkshopContext) or permission not in context.permissions:
        raise WorkshopAccessDenied("workshop access denied")
    if (not context.auth_store_id or not context.organization_id or not context.workshop_id
            or type(context.membership_version) is not int):
        raise WorkshopAccessDenied("workshop access denied")
    row = active_membership(connection, context.auth_store_id, context.employee_id)
    if (row["workshop_id"], row["organization_id"], row["version"]) != (
        context.workshop_id, context.organization_id, context.membership_version,
    ):
        raise WorkshopAccessDenied("workshop access denied")


class WorkshopDirectory:
    def __init__(self, repository, auth_repository):
        self.repository = repository
        self.auth_repository = auth_repository

    def _store_id(self):
        if not Path(self.auth_repository.database_path).is_file():
            raise ValueError("an existing auth database is required; provisioning creates no accounts")
        return self.auth_repository.store_id()

    def resolve(self, user_id):
        store_id = self._store_id()
        with self.repository._connect() as connection, connection:
            connection.execute("BEGIN")
            row = active_membership(connection, store_id, str(user_id))
            return {"workshop_id": row["workshop_id"], "organization_id": row["organization_id"],
                    "membership_version": row["version"], "auth_store_id": store_id}

    def _human_principal(self, user_id):
        if not Path(self.auth_repository.database_path).is_file():
            raise ValueError("an existing auth database is required; provisioning creates no accounts")
        user_id = str(UUID(str(user_id)))
        with self.auth_repository._connect() as connection, connection:
            connection.execute("BEGIN")
            store_id = self.auth_repository.store_id(connection)
            row = connection.execute("SELECT kind,enabled FROM auth_users WHERE user_id=?",
                                     (user_id,)).fetchone()
            if row is None or row["kind"] != "HUMAN" or not row["enabled"]:
                raise WorkshopAccessDenied("an existing enabled human account is required")
        return store_id, user_id

    def grant_membership(self, user_id, workshop_id, *, operator_reference):
        """Trusted offline caller chooses a real UUID explicitly; role gives no grant."""
        store_id, user_id = self._human_principal(user_id)
        workshop_id = str(UUID(str(workshop_id)))
        with self.repository._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO workshop_principals VALUES (?,?) ON CONFLICT DO NOTHING",
                               (store_id, user_id))
            connection.execute("""INSERT INTO workshop_memberships VALUES (?,?,?,1,1)
                ON CONFLICT(auth_store_id,user_id,workshop_id) DO UPDATE
                SET enabled=1,version=workshop_memberships.version+1""",
                (store_id, user_id, workshop_id))
            audit(connection, "MEMBERSHIP_GRANTED", operator_reference,
                  {"auth_store_id": store_id, "user_id": user_id, "workshop_id": workshop_id})

    def revoke_membership(self, user_id, workshop_id, *, operator_reference):
        store_id = self._store_id()
        with self.repository._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""UPDATE workshop_memberships SET enabled=0,version=version+1
                WHERE auth_store_id=? AND user_id=? AND workshop_id=?""",
                (store_id, str(UUID(str(user_id))), str(UUID(str(workshop_id)))))
            if cursor.rowcount != 1:
                raise WorkshopAccessDenied("workshop access denied")
            audit(connection, "MEMBERSHIP_REVOKED", operator_reference,
                  {"auth_store_id": store_id, "user_id": str(user_id), "workshop_id": str(workshop_id)})

    def select_active_workshop(self, user_id, workshop_id, *, operator_reference):
        store_id, user_id = self._human_principal(user_id)
        workshop_id = str(UUID(str(workshop_id)))
        with self.repository._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            usable = connection.execute("""SELECT 1 FROM workshop_memberships m
                JOIN workshops w ON w.workshop_id=m.workshop_id
                JOIN repair_organizations o ON o.organization_id=w.organization_id
                WHERE m.auth_store_id=? AND m.user_id=? AND m.workshop_id=?
                  AND m.enabled=1 AND w.status='ACTIVE' AND o.enabled=1""",
                (store_id, user_id, workshop_id)).fetchone()
            if usable is None:
                raise WorkshopAccessDenied("workshop access denied")
            connection.execute("""INSERT INTO active_user_workshops VALUES (?,?,?)
                ON CONFLICT(auth_store_id,user_id) DO UPDATE SET workshop_id=excluded.workshop_id""",
                (store_id, user_id, workshop_id))
            audit(connection, "ACTIVE_WORKSHOP_SELECTED", operator_reference,
                  {"auth_store_id": store_id, "user_id": user_id, "workshop_id": workshop_id})
