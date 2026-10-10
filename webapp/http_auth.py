"""Cookie-only HTTP transport over the existing authentication/CSRF services."""

import json
import re

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from webapp.auth_csrf import (
    COOKIE_NAMES, MFA_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE, CsrfValidationError,
    clear_auth_cookies, set_mfa_cookie, set_preauth_cookie, set_session_cookie,
)
from webapp.auth_models import SessionCookiePolicy
from webapp.auth_service import AuthenticationError


MAX_BODY_BYTES = 4096


class HttpAuthError(ValueError):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


def security_cookies(request: Request) -> dict[str, str]:
    """Parse raw headers before any lossy cookie mapping can hide duplicates."""
    headers = request.headers.getlist("cookie")
    if len(headers) > 1 or sum(len(value) for value in headers) > 8192:
        raise HttpAuthError(400, "Invalid authentication cookies")
    result = {}
    if not headers:
        return result
    for part in headers[0].split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise HttpAuthError(400, "Invalid authentication cookies")
        if name in COOKIE_NAMES:
            if name in result or not 1 <= len(value) <= 512 or not re.fullmatch(r"[A-Za-z0-9_.=-]+", value):
                raise HttpAuthError(400, "Invalid authentication cookies")
            result[name] = value
    return result


def _state(request: Request) -> tuple[dict, str]:
    cookies = request.state.security_cookies
    names = set(cookies)
    if SESSION_COOKIE in names:
        if names != {SESSION_COOKIE}:
            raise HttpAuthError(409, "Authentication state conflict")
        if not re.fullmatch(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", cookies[SESSION_COOKIE]):
            raise AuthenticationError("authentication failed")
        return cookies, "session"
    if names == {PREAUTH_COOKIE, MFA_COOKIE}:
        return cookies, "mfa"
    if names == {PREAUTH_COOKIE}:
        return cookies, "preauth"
    if names:
        raise HttpAuthError(403, "Invalid authentication context")
    return cookies, "absent"


def _response(payload) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _session_bindings(token, session):
    return dict(session_token=token, session_absolute_expiry=session.absolute_expiry,
                session_idle_expiry=session.idle_expiry)


def _proof_response(protector, purpose, origin, **bindings):
    proof = protector.issue_proof(purpose, origin, **bindings)
    # Only the public CSRF proof is returned; never a cookie or bearer secret.
    return _response({"state": purpose, "csrf_token": proof,
                      "expires_at": int(proof.split(".")[4])})


def _csrf(request):
    service, protector = request.app.state.auth_service, request.app.state.csrf_protector
    origin = request.state.target_origin
    cookies, state = _state(request)
    stale_session = False
    if state == "session":
        try:
            session = service.inspect_session(cookies[SESSION_COOKIE], touch=True)
        except AuthenticationError:
            # Confirmed invalid/expired/revoked state can bootstrap again. Storage
            # errors propagate as 503, never as anonymous state or cookie deletion.
            state = "absent"
            stale_session = True
        else:
            return _proof_response(protector, "session", origin, **_session_bindings(cookies[SESSION_COOKIE], session))
    if state == "absent":
        context = protector.create_preauth(origin)
        response = _proof_response(protector, "preauth", origin, preauth_cookie=context.value.get_secret_value())
        if stale_session:
            response.delete_cookie(SESSION_COOKIE, **SessionCookiePolicy().options())
        set_preauth_cookie(response, context, now=service.clock())
        return response
    bindings = {"preauth_cookie": cookies[PREAUTH_COOKIE]}
    if state == "mfa":
        bindings["mfa_cookie"] = cookies[MFA_COOKIE]
        challenge = protector.open_mfa(cookies[PREAUTH_COOKIE], cookies[MFA_COOKIE], origin)
        service.inspect_login_challenge(challenge.challenge_token.get_secret_value())
    return _proof_response(protector, state, origin, **bindings)


def _verify(request, purpose, **bindings):
    proofs = request.headers.getlist("x-csrf-token")
    if len(proofs) != 1:
        raise CsrfValidationError("Invalid CSRF security context")
    request.app.state.csrf_protector.verify_proof(proofs[0], purpose, request.state.target_origin, **bindings)


def _login(request, payload):
    cookies, state = _state(request)
    if state in {"session", "mfa"}:
        raise HttpAuthError(409, "Authentication state conflict")
    if state != "preauth":
        raise CsrfValidationError("Invalid CSRF security context")
    _verify(request, "preauth", preauth_cookie=cookies[PREAUTH_COOKIE])
    if set(payload) != {"username", "password"} or not all(isinstance(value, str) for value in payload.values()):
        raise HttpAuthError(422, "Invalid authentication request")
    if not 1 <= len(payload["username"]) <= 64 or not 1 <= len(payload["password"]) <= 256:
        raise HttpAuthError(422, "Invalid authentication request")
    service, protector = request.app.state.auth_service, request.app.state.csrf_protector
    challenge = service.begin_login(payload["username"], payload["password"], client=request.state.auth_client)
    if challenge.requires_enrollment:
        # Enrollment and recovery/reset transports are deliberately unavailable.
        # The internal ENROLL challenge is not disclosed, and grants no session.
        raise AuthenticationError("authentication failed")
    # Rotate the anonymous secret on successful password verification. Old login
    # proofs cannot be used with the new MFA flow or the eventual session cookie.
    context = protector.create_preauth(request.state.target_origin)
    preauth = context.value.get_secret_value()
    mfa = protector.seal_mfa(preauth, challenge.token.get_secret_value(), request.state.target_origin,
                             challenge_expires_at=challenge.expires_at)
    response = _proof_response(protector, "mfa", request.state.target_origin,
                               preauth_cookie=preauth, mfa_cookie=mfa.value.get_secret_value())
    set_preauth_cookie(response, context, now=service.clock())
    set_mfa_cookie(response, mfa, now=service.clock())
    return response


def _mfa(request, payload):
    cookies, state = _state(request)
    if state != "mfa":
        raise HttpAuthError(403, "Invalid authentication context")
    _verify(request, "mfa", preauth_cookie=cookies[PREAUTH_COOKIE], mfa_cookie=cookies[MFA_COOKIE])
    if set(payload) not in ({"totp_code"}, {"recovery_code"}):
        raise HttpAuthError(422, "Invalid authentication request")
    field, value = next(iter(payload.items()))
    if not isinstance(value, str) or not 1 <= len(value) <= (6 if field == "totp_code" else 40):
        raise HttpAuthError(422, "Invalid authentication request")
    service, protector = request.app.state.auth_service, request.app.state.csrf_protector
    context = protector.open_mfa(cookies[PREAUTH_COOKIE], cookies[MFA_COOKIE], request.state.target_origin)
    grant = service.complete_login(context.challenge_token.get_secret_value(), client=request.state.auth_client, **payload)
    response = _response({"state": "session"})
    for name in (PREAUTH_COOKIE, MFA_COOKIE):
        response.delete_cookie(name, **SessionCookiePolicy().options())
    set_session_cookie(response, grant, now=service.clock())
    return response


def _session(request):
    cookies, state = _state(request)
    if state != "session":
        raise AuthenticationError("authentication failed")
    session = request.app.state.auth_service.inspect_session(cookies[SESSION_COOKIE], touch=True)
    user = session.user
    return _response({"user": {"user_id": str(user.user_id), "username": user.username,
                                "display_name": user.display_name, "role": user.role.value},
                      "absolute_expires_at": int(session.absolute_expiry.timestamp()),
                      "idle_expires_at": int(session.idle_expiry.timestamp())})


def _logout(request, payload):
    cookies, state = _state(request)
    if state != "session":
        raise AuthenticationError("authentication failed")
    service = request.app.state.auth_service
    session = service.inspect_session(cookies[SESSION_COOKIE])
    _verify(request, "session", **_session_bindings(cookies[SESSION_COOKIE], session))
    if payload:
        raise HttpAuthError(422, "Invalid authentication request")
    service.revoke_session(cookies[SESSION_COOKIE])
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    clear_auth_cookies(response)
    return response


async def _payload(request):
    types = request.headers.getlist("content-type")
    if len(types) != 1 or types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
        raise HttpAuthError(415, "JSON authentication request required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HttpAuthError(413, "Authentication request too large")
        body.extend(chunk)
    lengths = request.headers.getlist("content-length")
    if lengths and (not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 10 or int(lengths[0]) != len(body)):
        raise HttpAuthError(400, "Invalid authentication request")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise HttpAuthError(422, "Invalid authentication request") from None
    if not isinstance(payload, dict):
        raise HttpAuthError(422, "Invalid authentication request")
    return payload


async def _dispatch(request, operation):
    if request.url.query:
        raise HttpAuthError(422, "Invalid authentication request")
    if request.method == "POST":
        # Verify the cookie-bound proof before reading credential input.
        proofs = request.headers.getlist("x-csrf-token")
        if len(proofs) != 1 or not 1 <= len(proofs[0]) <= 512:
            raise HttpAuthError(403, "Invalid CSRF security context")
        await run_in_threadpool(_check_write, request, operation)
        payload = await _payload(request)
        handler = {"login": _login, "mfa": _mfa, "logout": _logout}[operation]
        return await run_in_threadpool(handler, request, payload)
    return await run_in_threadpool({"csrf": _csrf, "session": _session}[operation], request)


def _check_write(request, operation):
    cookies, state = _state(request)
    if operation == "login":
        if state in {"session", "mfa"}:
            raise HttpAuthError(409, "Authentication state conflict")
        if state != "preauth":
            raise CsrfValidationError("Invalid CSRF security context")
        _verify(request, "preauth", preauth_cookie=cookies[PREAUTH_COOKIE])
    elif operation == "mfa":
        if state != "mfa":
            raise HttpAuthError(403, "Invalid authentication context")
        _verify(request, "mfa", preauth_cookie=cookies[PREAUTH_COOKIE], mfa_cookie=cookies[MFA_COOKIE])
    else:
        if state != "session":
            raise AuthenticationError("authentication failed")
        session = request.app.state.auth_service.inspect_session(cookies[SESSION_COOKIE])
        _verify(request, "session", **_session_bindings(cookies[SESSION_COOKIE], session))


async def csrf(request: Request):
    return await _dispatch(request, "csrf")


async def login(request: Request):
    return await _dispatch(request, "login")


async def mfa(request: Request):
    return await _dispatch(request, "mfa")


async def session(request: Request):
    return await _dispatch(request, "session")


async def logout(request: Request):
    return await _dispatch(request, "logout")


def register_auth_routes(app):
    # Register concrete operations directly so the existing fail-closed inventory
    # covers every route; no nested router or mounted app can bypass that review.
    for path, method, handler in (("csrf", "GET", csrf), ("login", "POST", login),
                                  ("mfa", "POST", mfa), ("session", "GET", session),
                                  ("logout", "POST", logout)):
        app.add_api_route("/api/auth/" + path, handler, methods=[method], include_in_schema=False)
