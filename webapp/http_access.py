"""Explicit route inventory, auth HTTP boundary and server-owned business gate."""

import sqlite3
from types import MappingProxyType

from cryptography.fernet import InvalidToken

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles

from webapp.auth_models import IdentityKind, UserRole
from webapp.auth_crypto import CryptoBusyError
from webapp.auth_csrf import SESSION_COOKIE, CsrfValidationError
from webapp.auth_service import AuthenticationError, RateLimitError, TrustedClientContext
from webapp.http_auth import HttpAuthError, security_cookies


# Every registered route is explicitly classified, including HTML and the mount.
ROUTE_PERMISSIONS = MappingProxyType({
    ("/", "GET"): "read",
    ("/static", "MOUNT"): "read",
    ("/api/works", "GET"): "read",
    ("/api/works", "POST"): "write",
    ("/api/works/{work_id}", "GET"): "read",
    ("/api/works/{work_id}/labour", "PUT"): "write",
    ("/api/auth/csrf", "GET"): "auth",
    ("/api/auth/login", "POST"): "auth",
    ("/api/auth/mfa", "POST"): "auth",
    ("/api/auth/session", "GET"): "auth",
    ("/api/auth/logout", "POST"): "auth",
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
    @staticmethod
    def _transport(request, settings, *, auth=False):
        # Only ASGI's scheme and an exact configured external authority are used.
        # Forwarded headers are never interpreted here; proxy trust is server setup.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or f"{request.scope['scheme']}://{hosts[0].lower()}" not in settings.external_origins:
            raise HttpAuthError(400, "Invalid HTTPS host")
        target = f"{request.scope['scheme']}://{hosts[0].lower()}"
        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or origins[0] not in settings.external_origins):
            raise HttpAuthError(403, "Origin not allowed")
        if auth:
            if (request.method == "POST" and len(origins) != 1) or (origins and origins[0] != target):
                raise HttpAuthError(403, "Origin not allowed")
            for name in ("x-csrf-token", "x-carbit-auth", "content-type", "content-length", "authorization"):
                if len(request.headers.getlist(name)) > 1:
                    raise HttpAuthError(400, "Ambiguous authentication headers")
            if request.headers.getlist("authorization") or request.headers.getlist("transfer-encoding"):
                raise HttpAuthError(400, "Unsupported authentication headers")
            if request.url.path == "/api/auth/csrf" and request.headers.getlist("x-carbit-auth") != ["bootstrap"]:
                # Navigation/image/form requests cannot set this custom header.
                # Cross-origin fetch needs a preflight, which we never grant.
                raise HttpAuthError(403, "Authentication bootstrap header required")
        request.state.target_origin = target
        request.state.security_cookies = security_cookies(request)

    async def _auth(self, request, call_next, settings, service):
        operation = request.url.path.rsplit("/", 1)[-1]
        try:
            if getattr(request.app.state, "csrf_protector", None) is None or request.client is None:
                return _error(503, "Authentication unavailable")
            # The ASGI connection address is server-owned. Forwarded identity/IP
            # headers, ports, request bodies and cookies cannot change this key.
            client = TrustedClientContext(request.client.host)
            request.state.auth_client = client
            await run_in_threadpool(service.reserve_http_auth, operation, client=client)
            self._transport(request, settings, auth=True)
            response = await call_next(request)
        except HttpAuthError as error:
            response = _error(error.status, error.detail)
        except CsrfValidationError:
            response = _error(403, "Invalid CSRF security context")
        except RateLimitError:
            response = _error(429, "Authentication temporarily limited")
        except AuthenticationError:
            response = _error(401, "Authentication failed")
        except (sqlite3.DatabaseError, CryptoBusyError, InvalidToken, ValueError, TypeError, KeyError):
            response = _error(503, "Authentication unavailable")
        try:
            await run_in_threadpool(service.audit_http_auth, operation, response.status_code)
        except sqlite3.DatabaseError:
            return _error(503, "Authentication unavailable")
        response.headers["Cache-Control"] = "no-store"
        return response

    async def dispatch(self, request: Request, call_next):
        state = request.app.state
        settings = getattr(state, "auth_settings", None)
        service = getattr(state, "auth_service", None)
        if settings is None or service is None:
            return _error(503, "Authentication unavailable")
        permission = None
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                method = "MOUNT" if isinstance(route, Mount) else request.method
                permission = state.route_access.get((id(route), method))
                if permission is None:
                    return _error(403, "HTTP route not permitted")
                break
        if permission == "auth":
            return await self._auth(request, call_next, settings, service)
        try:
            self._transport(request, settings)
        except HttpAuthError as error:
            return _error(error.status, error.detail)
        if permission is None:
            # Unknown paths/methods can produce only the router's 404/405/redirect.
            return await call_next(request)
        token = request.state.security_cookies.get(SESSION_COOKIE)
        if not token:
            return _error(401, "Authentication required")
        try:
            user = await run_in_threadpool(service.require_session, token)
        except AuthenticationError:
            return _error(401, "Authentication required")
        except sqlite3.DatabaseError:
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
