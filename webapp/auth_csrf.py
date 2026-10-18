"""Isolated CSRF proofs and encrypted cookies. No HTTP transport or auth grants.

Session bindings and expiry limits MUST come from a validated server session.
These proofs establish context possession, never identity or authorization.
"""

import base64
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from webapp.auth_models import SessionCookiePolicy, SessionGrant
from webapp.auth_settings import AuthSettings


PREAUTH_SECONDS = 600
MFA_SECONDS = 300
PROOF_SECONDS = 600
PREAUTH_COOKIE = "__Host-carbit-preauth"
MFA_COOKIE = "__Host-carbit-mfa-challenge"
SESSION_COOKIE = SessionCookiePolicy().name
COOKIE_NAMES = (PREAUTH_COOKIE, MFA_COOKIE, SESSION_COOKIE)


class CsrfValidationError(ValueError):
    """Generic failure; never include untrusted inputs or secret values."""


def _fail():
    raise CsrfValidationError("Invalid CSRF security context")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != (size * 8 + 5) // 6 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        _fail()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        _fail()
    if len(raw) != size or _encode(raw) != value:
        _fail()
    return raw


def _epoch(value: datetime) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _fail()
    stamp = int(value.timestamp())
    if not 0 <= stamp <= 9999999999:
        _fail()
    return stamp


def _utc(stamp: int) -> datetime:
    return datetime.fromtimestamp(stamp, timezone.utc)


def _frame(domain: str, fields: list[str], origin: str, binding: tuple[bytes, ...]) -> bytes:
    # A canonical JSON array provides unambiguous boundaries, including bindings.
    return json.dumps(
        [domain, fields, origin, [_encode(item) for item in binding]],
        ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")


@dataclass(frozen=True)
class ProtectedCookie:
    value: SecretStr = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class BrowserContext:
    secret: SecretStr = field(repr=False)
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class MfaContext:
    challenge_token: SecretStr = field(repr=False)
    issued_at: datetime
    expires_at: datetime


class CsrfProtector:
    def __init__(self, settings: AuthSettings, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(settings, AuthSettings):
            _fail()
        self._keys = {item.key_id: item.key_bytes() for item in settings.csrf_keys}
        self._active = settings.csrf_active_key_id
        self._origins = frozenset(settings.external_origins)
        self._clock = clock

    def _origin(self, origin: str):
        if not isinstance(origin, str) or origin not in self._origins:
            _fail()

    def _now(self) -> int:
        return _epoch(self._clock())

    def _parse(self, value: str, version: str, purpose: str, lifetime: int):
        if not isinstance(value, str) or not 1 <= len(value) <= 512:
            _fail()
        fields = value.split(".")
        if len(fields) != 7 or fields[0] != version or fields[2] != purpose or fields[1] not in self._keys:
            _fail()
        for stamp in fields[3:5]:
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", stamp):
                _fail()
        issued, expires = int(fields[3]), int(fields[4])
        if not issued <= self._now() < expires or not 0 < expires - issued <= lifetime:
            _fail()
        return fields, issued, expires

    def _aead(self, key_id: str, purpose: str) -> AESGCM:
        # Signing material is never used directly as an encryption key.
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=f"carbit-csrf-envelope-e1:{key_id}:{purpose}".encode("ascii"),
        ).derive(self._keys[key_id])
        return AESGCM(key)

    def _seal(self, purpose: str, origin: str, payload: bytes, expires: int, binding: tuple[bytes, ...]) -> ProtectedCookie:
        self._origin(origin)
        now = self._now()
        if expires <= now:
            _fail()
        nonce = secrets.token_bytes(12)
        fields = ["e1", self._active, purpose, str(now), str(expires), _encode(nonce)]
        ciphertext = self._aead(self._active, purpose).encrypt(
            nonce, payload, _frame("carbit-csrf-envelope-e1", fields, origin, binding),
        )
        return ProtectedCookie(SecretStr(".".join(fields + [_encode(ciphertext)])), _utc(expires))

    def _open(self, value: str, purpose: str, origin: str, size: int, lifetime: int, binding: tuple[bytes, ...]):
        self._origin(origin)
        fields, issued, expires = self._parse(value, "e1", purpose, lifetime)
        nonce, ciphertext = _decode(fields[5], 12), _decode(fields[6], size + 16)
        try:
            payload = self._aead(fields[1], purpose).decrypt(
                nonce, ciphertext, _frame("carbit-csrf-envelope-e1", fields[:6], origin, binding),
            )
        except InvalidTag:
            _fail()
        return payload, issued, expires

    def create_preauth(self, origin: str) -> ProtectedCookie:
        return self._seal("preauth", origin, secrets.token_bytes(32), self._now() + PREAUTH_SECONDS, ())

    def open_preauth(self, cookie: str, origin: str) -> BrowserContext:
        secret, issued, expires = self._open(cookie, "preauth", origin, 32, PREAUTH_SECONDS, ())
        return BrowserContext(SecretStr(_encode(secret)), _utc(issued), _utc(expires))

    def seal_mfa(self, preauth_cookie: str, challenge_token: str, origin: str, *, challenge_expires_at: datetime) -> ProtectedCookie:
        context = self.open_preauth(preauth_cookie, origin)
        _decode(challenge_token, 32)  # Canonical existing 256-bit opaque challenge.
        expires = min(self._now() + MFA_SECONDS, _epoch(context.expires_at), _epoch(challenge_expires_at))
        return self._seal("mfa", origin, challenge_token.encode("ascii"), expires, (_decode(context.secret.get_secret_value(), 32),))

    def open_mfa(self, preauth_cookie: str, mfa_cookie: str, origin: str) -> MfaContext:
        context = self.open_preauth(preauth_cookie, origin)
        token, issued, expires = self._open(
            mfa_cookie, "mfa", origin, 43, MFA_SECONDS, (_decode(context.secret.get_secret_value(), 32),),
        )
        if expires > _epoch(context.expires_at) or issued < _epoch(context.issued_at):
            _fail()
        try:
            challenge = token.decode("ascii")
        except UnicodeError:
            _fail()
        _decode(challenge, 32)
        return MfaContext(SecretStr(challenge), _utc(issued), _utc(expires))

    def _binding(self, purpose, origin, preauth_cookie, mfa_cookie, session_token, session_absolute_expiry, session_idle_expiry):
        self._origin(origin)
        if not isinstance(purpose, str):
            _fail()
        if purpose in {"preauth", "mfa"}:
            if any(item is not None for item in (session_token, session_absolute_expiry, session_idle_expiry)):
                _fail()
            context = self.open_preauth(preauth_cookie, origin)
            bindings = (_decode(context.secret.get_secret_value(), 32),)
            issued, expires = _epoch(context.issued_at), _epoch(context.expires_at)
            if purpose == "mfa":
                challenge = self.open_mfa(preauth_cookie, mfa_cookie, origin)
                bindings += (_decode(challenge.challenge_token.get_secret_value(), 32),)
                issued, expires = _epoch(challenge.issued_at), min(expires, _epoch(challenge.expires_at))
            elif mfa_cookie is not None:
                _fail()
        elif purpose == "session":
            if preauth_cookie is not None or mfa_cookie is not None:
                _fail()
            bindings = (_decode(session_token, 32),)
            issued = 0
            expires = min(_epoch(session_absolute_expiry), _epoch(session_idle_expiry))
        else:
            _fail()
        if self._now() >= expires:
            _fail()
        return bindings, issued, expires

    def issue_proof(
        self, purpose: str, origin: str, *, preauth_cookie: str | None = None,
        mfa_cookie: str | None = None, session_token: str | None = None,
        session_absolute_expiry: datetime | None = None, session_idle_expiry: datetime | None = None,
        lifetime_seconds: int = PROOF_SECONDS,
    ) -> str:
        if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= PROOF_SECONDS:
            _fail()
        binding, _, limit = self._binding(purpose, origin, preauth_cookie, mfa_cookie, session_token, session_absolute_expiry, session_idle_expiry)
        now = self._now()
        fields = ["v1", self._active, purpose, str(now), str(min(now + lifetime_seconds, limit)), _encode(secrets.token_bytes(16))]
        mac = hmac.digest(self._keys[self._active], _frame("carbit-csrf-proof-v1", fields, origin, binding), "sha256")
        return ".".join(fields + [_encode(mac)])

    def verify_proof(
        self, proof: str, purpose: str, origin: str, *, preauth_cookie: str | None = None,
        mfa_cookie: str | None = None, session_token: str | None = None,
        session_absolute_expiry: datetime | None = None, session_idle_expiry: datetime | None = None,
    ) -> None:
        binding, context_issued, limit = self._binding(purpose, origin, preauth_cookie, mfa_cookie, session_token, session_absolute_expiry, session_idle_expiry)
        fields, issued, expires = self._parse(proof, "v1", purpose, PROOF_SECONDS)
        if issued < context_issued or expires > limit:
            _fail()
        _decode(fields[5], 16)
        supplied = _decode(fields[6], 32)
        expected = hmac.digest(self._keys[fields[1]], _frame("carbit-csrf-proof-v1", fields[:6], origin, binding), "sha256")
        if not hmac.compare_digest(supplied, expected):
            _fail()


def _set_cookie(response, name: str, value: SecretStr, expires_at: datetime, now: datetime, maximum: int):
    remaining = _epoch(expires_at) - _epoch(now)
    if not isinstance(value, SecretStr) or not 0 < remaining <= maximum:
        _fail()
    response.set_cookie(
        name, value.get_secret_value(), max_age=remaining, expires=_utc(_epoch(expires_at)),
        **SessionCookiePolicy().options(),
    )


def set_preauth_cookie(response, cookie: ProtectedCookie, *, now: datetime):
    _set_cookie(response, PREAUTH_COOKIE, cookie.value, cookie.expires_at, now, PREAUTH_SECONDS)


def set_mfa_cookie(response, cookie: ProtectedCookie, *, now: datetime):
    _set_cookie(response, MFA_COOKIE, cookie.value, cookie.expires_at, now, MFA_SECONDS)


def set_session_cookie(response, grant: SessionGrant, *, now: datetime):
    # Reissuing a grant cannot move its original absolute deadline.
    _decode(grant.token.get_secret_value(), 32)
    if _epoch(grant.session.created_at) > _epoch(now) or _epoch(grant.session.expires_at) - _epoch(grant.session.created_at) > 8 * 3600:
        _fail()
    _set_cookie(response, SESSION_COOKIE, grant.token, grant.session.expires_at, now, 8 * 3600)


def clear_auth_cookies(response):
    for name in COOKIE_NAMES:
        response.delete_cookie(name, **SessionCookiePolicy().options())
