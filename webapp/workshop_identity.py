"""Token-only adapter for server callers; never deserialize request identities."""

from webapp.auth_models import IdentityKind, UserRole
from webapp.auth_service import AuthService, AuthenticationError, PermissionDeniedError
from webapp.business_identity import BusinessActor
from webapp.labour_service import TrustedWorkshopContext, WorkshopPermission
from webapp.workshop_repository import WorkshopDirectory
from webapp.workshop_context import permissions_for_role
from webapp.business_authorization import WorkshopAuthBinding


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
    session = service.inspect_session(session_token)
    user = session.user
    if not user.enabled or not user.mfa_enabled or user.kind != IdentityKind.HUMAN:
        raise AuthenticationError("authentication required")
    permissions = permissions_for_role(user.role)
    if required_permission not in permissions:
        raise PermissionDeniedError("workshop permission denied")
    directory = getattr(service, "workshop_directory", None)
    if not isinstance(directory, WorkshopDirectory):
        raise PermissionDeniedError("workshop access denied")
    membership = directory.resolve(user.user_id)
    return TrustedWorkshopContext(
        employee_id=str(user.user_id), permissions=permissions,
        actor=BusinessActor(user.user_id, user.display_name),
        auth_binding=WorkshopAuthBinding(service, session.session_id),
        **membership,
    )
