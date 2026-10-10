"""Route inventory and server-owned session gate; no login or approval transport."""

import sqlite3
from types import MappingProxyType

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles

from webapp.auth_models import IdentityKind, SessionCookiePolicy, UserRole
from webapp.auth_service import AuthenticationError


# Every registered route is explicitly classified, including HTML and the mount.
ROUTE_PERMISSIONS = MappingProxyType({
    ("/", "GET"): "read",
    ("/static", "MOUNT"): "read",
    ("/api/works", "GET"): "read",
    ("/api/works", "POST"): "write",
    ("/api/works/{work_id}", "GET"): "read",
    ("/api/works/{work_id}/labour", "PUT"): "write",
})
READ_ROLES = frozenset({UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN})
WRITE_ROLES = frozenset({UserRole.OWNER, UserRole.SERVICE_MANAGER})


def validate_route_coverage(app) -> dict:
    """Startup fails if a route or method has no reviewed policy (also mounts)."""
    classified = {}
    seen = set()
    for route in app.routes:
        if isinstance(route, Mount) and not isinstance(route.app, StaticFiles):
            raise ValueError("mounted application has no explicit access policy")
        methods = {"MOUNT"} if isinstance(route, Mount) else route.methods
        for method in methods:
            key = (route.path, method)
            if key not in ROUTE_PERMISSIONS or key in seen:
                raise ValueError("HTTP route has no unique, explicit access policy")
            seen.add(key)
            classified[(id(route), method)] = ROUTE_PERMISSIONS[key]
    if seen != set(ROUTE_PERMISSIONS):
        raise ValueError("HTTP route inventory differs from the reviewed access policy")
    return classified


def _error(code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=code, headers={"Cache-Control": "no-store"})


class HttpAccessGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        state = request.app.state
        settings = getattr(state, "auth_settings", None)
        service = getattr(state, "auth_service", None)
        if settings is None or service is None:
            return _error(503, "Authentication unavailable")
        # Only ASGI's scheme and an exact configured external authority are used.
        # Forwarded headers are never interpreted here; proxy trust is server setup.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or f"{request.scope['scheme']}://{hosts[0].lower()}" not in settings.external_origins:
            return _error(400, "Invalid HTTPS host")
        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or origins[0] not in settings.external_origins):
            return _error(403, "Origin not allowed")

        permission = None
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                method = "MOUNT" if isinstance(route, Mount) else request.method
                permission = state.route_access.get((id(route), method))
                if permission is None:
                    return _error(403, "HTTP route not permitted")
                break
        if permission is None:
            # Unknown paths/methods can produce only the router's 404/405/redirect.
            return await call_next(request)
        token = request.cookies.get(SessionCookiePolicy().name)
        if not token:
            return _error(401, "Authentication required")
        try:
            user = await run_in_threadpool(service.require_session, token)
        except AuthenticationError:
            return _error(401, "Authentication required")
        except sqlite3.OperationalError:
            return _error(503, "Authentication unavailable")
        if not user.enabled or not user.mfa_enabled or user.kind != IdentityKind.HUMAN:
            return _error(401, "Authentication required")
        roles = READ_ROLES if permission == "read" else WRITE_ROLES
        if user.role not in roles:
            return _error(403, "Permission denied")
        # Server-owned identity; not yet a labour-approval context or event actor.
        request.state.authenticated_user = user
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
