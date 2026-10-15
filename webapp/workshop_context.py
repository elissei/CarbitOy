"""Internal server snapshots, never browser-supplied authorization credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from types import MappingProxyType

from webapp.auth_service import PermissionDeniedError
from webapp.auth_models import UserRole
from webapp.business_identity import BusinessActor

if TYPE_CHECKING:
    from webapp.business_authorization import WorkshopAuthBinding


class WorkshopAccessDenied(PermissionDeniedError, PermissionError):
    """Generic denial; does not disclose workshops, memberships or inquiries."""


class WorkshopPermission(str, Enum):
    READ_LABOUR_PLAN = "READ_LABOUR_PLAN"
    SAVE_LABOUR_PLAN = "SAVE_LABOUR_PLAN"
    APPROVE_LABOUR_PLAN = "APPROVE_LABOUR_PLAN"
    READ_OWN_DRAFT = "READ_OWN_DRAFT"
    WRITE_OWN_DRAFT = "WRITE_OWN_DRAFT"


_READ = frozenset({WorkshopPermission.READ_LABOUR_PLAN})
_WRITE = _READ | {WorkshopPermission.SAVE_LABOUR_PLAN, WorkshopPermission.APPROVE_LABOUR_PLAN}
_DRAFT = frozenset({WorkshopPermission.READ_OWN_DRAFT, WorkshopPermission.WRITE_OWN_DRAFT})
_ROLE_PERMISSIONS = MappingProxyType({UserRole.OWNER: _WRITE | _DRAFT, UserRole.SERVICE_MANAGER: _WRITE | _DRAFT,
                                     UserRole.IT_ADMIN: _READ})


def permissions_for_role(role):
    return _ROLE_PERMISSIONS.get(role, frozenset())


@dataclass(frozen=True)
class TrustedWorkshopContext:
    employee_id: str
    permissions: frozenset[WorkshopPermission]
    actor: BusinessActor | None = None
    auth_store_id: str | None = None
    organization_id: str | None = None
    workshop_id: str | None = None
    membership_version: int | None = None
    auth_binding: WorkshopAuthBinding | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.employee_id, str) or not self.employee_id.strip():
            raise ValueError("trusted employee identity is required")
        if not isinstance(self.permissions, frozenset) or any(
            not isinstance(permission, WorkshopPermission) for permission in self.permissions
        ):
            raise ValueError("trusted permissions must be an immutable permission set")
        if self.actor is not None and (
            not isinstance(self.actor, BusinessActor) or str(self.actor.user_id) != self.employee_id
        ):
            raise ValueError("attribution must match the trusted employee UUID")

