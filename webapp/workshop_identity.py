"""Token-only adapter for server callers; never deserialize request identities."""

from types import MappingProxyType

from webapp.auth_models import IdentityKind, UserRole
from webapp.auth_service import AuthService, AuthenticationError, PermissionDeniedError
from webapp.business_identity import BusinessActor
from webapp.labour_service import TrustedWorkshopContext, WorkshopPermission


_READ = frozenset({WorkshopPermission.READ_LABOUR_PLAN})
_WRITE = _READ | {WorkshopPermission.SAVE_LABOUR_PLAN, WorkshopPermission.APPROVE_LABOUR_PLAN}
_PERMISSIONS = MappingProxyType({UserRole.OWNER: _WRITE, UserRole.SERVICE_MANAGER: _WRITE,
                               UserRole.IT_ADMIN: _READ})


def resolve_workshop_context(
    service: AuthService, session_token: str, *, required_permission: WorkshopPermission,
) -> TrustedWorkshopContext:
    """Revalidate a persisted human session without touching its idle time.

    The service and required permission are server-owned. A User, actor UUID,
    permission set or JSON context is never accepted as authentication proof.
    The returned context is a per-operation snapshot, not a reusable credential.
    """
    if not isinstance(required_permission, WorkshopPermission):
        raise PermissionDeniedError("workshop permission denied")
    user = service.inspect_session(session_token).user
    if not user.enabled or not user.mfa_enabled or user.kind != IdentityKind.HUMAN:
        raise AuthenticationError("authentication required")
    permissions = _PERMISSIONS.get(user.role, frozenset())
    if required_permission not in permissions:
        raise PermissionDeniedError("workshop permission denied")
    return TrustedWorkshopContext(
        employee_id=str(user.user_id), permissions=permissions,
        actor=BusinessActor(user.user_id, user.display_name),
    )
