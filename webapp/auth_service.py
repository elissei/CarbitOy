"""Internal two-factor authentication. Never expose provisioning or keys over HTTP.

Business APIs intentionally remain unconnected. Resolve a real session at the
future HTTP boundary; User/TrustedWorkshopContext construction is not auth.
"""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

import pyotp
from cryptography.fernet import InvalidToken
from pydantic import SecretStr

from webapp.auth_crypto import (
    PasswordService, TotpCipher, matching_totp_step, new_recovery_codes,
    recovery_digest, token_digest,
)
from webapp.auth_models import (
    EnrollmentGrant, IdentityKind, LoginChallenge, MfaEnrollment, MfaResetRequest,
    MfaResetResult, SessionGrant, SessionRecord, User, UserRole,
)
from webapp.auth_repository import AuthRepository


class AuthenticationError(ValueError):
    """Intentionally generic: do not reveal account/factor existence."""


class PermissionDeniedError(AuthenticationError):
    pass


class RateLimitError(AuthenticationError):
    def __init__(self, message: str, *, retry_after: int | None = None):
        if retry_after is not None and (type(retry_after) is not int or retry_after <= 0):
            raise ValueError("retry delay must be a positive integer")
        self.retry_after = retry_after
        super().__init__(message)


class AuthVersionConflictError(ValueError):
    pass


class AuthIdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class AuthPolicy:
    challenge_seconds: int = 300
    session_seconds: int = 8 * 3600
    idle_seconds: int = 30 * 60
    admin_mfa_fresh_seconds: int = 300
    limit_window_seconds: int = 300
    individual_attempts: int = 5
    global_attempts: int = 30
    # Combined session/CSRF traffic has its own short availability budget.
    maintenance_window_seconds: int = 60
    maintenance_individual_attempts: int = 120
    maintenance_global_attempts: int = 1200

    def __post_init__(self):
        values = tuple(vars(self).values())
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("authentication limits must be positive integers")
        if max(self.limit_window_seconds, self.maintenance_window_seconds) > 86400:
            raise ValueError("rate-limit windows must not exceed 86400 seconds")
        if self.individual_attempts > self.global_attempts:
            raise ValueError("individual attempts exceed global attempts")
        if self.maintenance_individual_attempts > self.maintenance_global_attempts:
            raise ValueError("individual maintenance attempts exceed global attempts")


@dataclass(frozen=True)
class TrustedClientContext:
    """Server-derived connection key; never take it from an untrusted body/header."""

    connection_key: str

    def __post_init__(self):
        if not isinstance(self.connection_key, str) or not 1 <= len(self.connection_key) <= 128:
            raise ValueError("server connection key must be bounded")


@dataclass(frozen=True)
class ValidatedSession:
    """Server-validated identity and deadlines; contains no bearer token."""

    user: User
    absolute_expiry: datetime
    idle_expiry: datetime
    session_id: str


def _utc(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _user(row) -> User:
    return User(
        user_id=row["user_id"], username=row["username"], display_name=row["display_name"],
        role=row["role"], kind=row["kind"], enabled=bool(row["enabled"]),
        mfa_enabled=bool(row["mfa_enabled"]), auth_version=row["auth_version"],
        created_at=_utc(row["created_at"]),
    )


class AuthService:
    def __init__(
        self, repository: AuthRepository, cipher: TotpCipher, *,
        passwords: PasswordService | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        policy: AuthPolicy = AuthPolicy(),
    ) -> None:
        self.repository = repository
        self.cipher = cipher
        self.passwords = passwords or PasswordService()
        self.clock = clock
        self.policy = policy

    def _now(self) -> int:
        value = self.clock()
        if value.utcoffset() is None:
            raise ValueError("server clock must be timezone-aware")
        return int(value.timestamp())

    @staticmethod
    def _audit(connection, event, now, *, actor=None, subject=None, details=None) -> int:
        return connection.execute(
            "INSERT INTO auth_events(event_type,actor_user_id,subject_user_id,occurred_at,details_json) "
            "VALUES (?,?,?,?,?)",
            (event, actor, subject, now, json.dumps(details or {}, sort_keys=True)),
        ).lastrowid

    def _audit_sampled(self, connection, event: str, now: int, *, sample_key: str, details: dict) -> None:
        """Persist representative denials, normally one per key/60s, across workers.

        The caller holds BEGIN IMMEDIATE. No mutable process cache or new schema:
        suppressed repeats write neither counters nor events. This is sampling,
        not an exact count of all rejected requests. Lookups cover only the
        last 4096 event IDs; eviction can produce another representative sample.
        """
        previous = connection.execute(
            "SELECT event_id FROM auth_events WHERE event_type=? "
            "AND event_id > COALESCE((SELECT MAX(event_id) FROM auth_events),0)-4096 "
            "AND occurred_at>? AND occurred_at<=? "
            "AND json_extract(details_json,'$.sample_key')=? ORDER BY event_id DESC LIMIT 1",
            (event, now - 60, now, sample_key),
        ).fetchone()
        if previous is None:
            self._audit(connection, event, now, details={
                **details, "sample_key": sample_key, "sample_window_seconds": 60,
                "sampled": True,
            })

    def _limit_scopes(
        self, account: str | None, client: TrustedClientContext, *, maintenance: bool,
        include_global: bool = True, include_client: bool = True,
    ):
        window = self.policy.maintenance_window_seconds if maintenance else self.policy.limit_window_seconds
        individual = self.policy.maintenance_individual_attempts if maintenance else self.policy.individual_attempts
        global_limit = self.policy.maintenance_global_attempts if maintenance else self.policy.global_attempts
        scopes = (("global", global_limit),) if include_global else ()
        if account is not None:
            scopes += (("account:" + account, individual),)
        if include_client:
            scopes += (("client:" + client.connection_key, individual),)
        return window, scopes

    def _retry_after(
        self, connection, phase, account, client, now, *, maintenance=False,
        include_global=True, include_client=True,
    ) -> int:
        window, scopes = self._limit_scopes(account, client, maintenance=maintenance,
                                            include_global=include_global, include_client=include_client)
        delays = []
        for scope, limit in scopes:
            digest = hashlib.sha256((phase + ":" + scope).encode()).hexdigest()
            row = connection.execute("SELECT * FROM auth_rate_limits WHERE scope_hash=?", (digest,)).fetchone()
            if row and row["attempts"] >= limit and now < row["window_start"] + window:
                delays.append(row["window_start"] + window - now)
        return max(delays, default=window)

    @staticmethod
    def _opaque_digest(token: str) -> str:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            # Unknown token goes through the same limited authentication path.
            return "invalid-token"
        return token_digest(token)

    def _reserve(
        self, connection, phase: str, account: str | None, client: TrustedClientContext,
        now: int, *, maintenance: bool = False, include_global: bool = True,
        include_client: bool = True, operation: str | None = None,
    ) -> bool:
        if not isinstance(client, TrustedClientContext):
            raise TypeError("use a server-derived client context")
        window, scopes = self._limit_scopes(account, client, maintenance=maintenance,
                                            include_global=include_global, include_client=include_client)
        reservations = []
        # The caller holds BEGIN IMMEDIATE: check every scope before spending any
        # budget. A blocked client/account must not drain another scope or reset
        # its expired window. Concurrent requests cannot race this reservation.
        for scope, limit in scopes:
            digest = hashlib.sha256((phase + ":" + scope).encode()).hexdigest()
            row = connection.execute("SELECT * FROM auth_rate_limits WHERE scope_hash=?", (digest,)).fetchone()
            active = row is not None and now < row["window_start"] + window
            if active and row["attempts"] >= limit:
                scope_kind = scope.split(":", 1)[0]
                details = {"phase": phase, "scope_kind": scope_kind}
                if operation is not None:
                    details["operation"] = operation
                # Categorical markers only: never persist the counter digest,
                # raw account/client key, or a per-person window fingerprint.
                self._audit_sampled(connection, "AUTH_RATE_LIMITED", now,
                    sample_key=f"RATE:{phase}:{scope_kind}", details=details)
                return False
            reservations.append((digest, active))
        connection.execute("DELETE FROM auth_rate_limits WHERE window_start < ?", (now - 86400,))
        for digest, active in reservations:
            if not active:
                connection.execute(
                    "INSERT INTO auth_rate_limits VALUES (?,?,1) "
                    "ON CONFLICT(scope_hash) DO UPDATE SET window_start=excluded.window_start,attempts=1",
                    (digest, now),
                )
            else:
                connection.execute("UPDATE auth_rate_limits SET attempts=attempts+1 WHERE scope_hash=?", (digest,))
        return True

    def reserve_http_auth(self, operation: str, *, client: TrustedClientContext) -> None:
        """Bound every auth request; session/CSRF never spend credential budgets.

        Maintenance endpoints share a separate per-client/global budget, including
        malformed requests. Logout reserves only its per-client budget here;
        its global budget is reserved after session/CSRF/payload validation.
        Login and MFA retain their existing strict
        HTTP budgets; password/MFA account-level service limits are unchanged.
        """
        if operation not in {"csrf", "login", "mfa", "session", "logout"}:
            raise ValueError("unknown authentication operation")
        maintenance = operation in {"csrf", "session", "logout"}
        phase = ("HTTP_LOGOUT" if operation == "logout" else
                 "HTTP_MAINTENANCE" if maintenance else "HTTP_AUTH:" + operation)
        with self.repository.transaction() as connection:
            now = self._now()
            include_global = operation != "logout"
            allowed = self._reserve(connection, phase, None, client, now, maintenance=maintenance,
                                    include_global=include_global, operation=operation)
            retry_after = self._retry_after(connection, phase, None, client, now,
                maintenance=maintenance, include_global=include_global) if not allowed else None
        if not allowed:
            raise RateLimitError("authentication temporarily limited", retry_after=retry_after)

    def reserve_logout_global(self, *, client: TrustedClientContext) -> None:
        """Trusted HTTP adapter only, after valid session, CSRF and empty payload.

        Per-client attempts were already reserved at the HTTP boundary. Do not
        charge them twice, or let unauthenticated traffic spend this global cap.
        Revocation still revalidates the session in its own transaction.
        """
        with self.repository.transaction() as connection:
            now = self._now()
            allowed = self._reserve(connection, "HTTP_LOGOUT", None, client, now,
                                    maintenance=True, include_client=False, operation="logout")
            retry_after = self._retry_after(connection, "HTTP_LOGOUT", None, client, now,
                maintenance=True, include_client=False) if not allowed else None
        if not allowed:
            raise RateLimitError("authentication temporarily limited", retry_after=retry_after)

    def audit_http_auth(self, operation: str, status_code: int) -> None:
        if operation not in {"csrf", "login", "mfa", "session", "logout"} or type(status_code) is not int:
            raise ValueError("invalid authentication boundary event")
        with self.repository.transaction() as connection:
            now = self._now()
            details = {"operation": operation, "status": status_code}
            if status_code in {400, 403, 429}:
                self._audit_sampled(connection, "AUTH_HTTP_RESULT", now,
                    sample_key=f"HTTP:{operation}:{status_code}", details=details)
            else:
                self._audit(connection, "AUTH_HTTP_RESULT", now, details=details)

    def inspect_login_challenge(self, token: str) -> datetime:
        """Check persisted consumption, identity, auth version and expiry."""
        with self.repository.transaction() as connection:
            challenge, user = self._challenge(connection, token, "LOGIN", self._now())
            if challenge is None or not user["mfa_enabled"]:
                raise AuthenticationError("authentication failed")
            return _utc(challenge["expires_at"])

    def provision_identity(self, *, username: str, display_name: str, role: UserRole, password: str | None = None) -> User:
        """Offline trusted operator only. No registration route or default accounts.

        The future admin adapter must not publish this bootstrap method.
        """
        if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9._-]{2,63}", username):
            raise ValueError("invalid bounded account name")
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 200:
            raise ValueError("invalid display name")
        role = UserRole(role)
        kind = IdentityKind.SERVICE if role == UserRole.OPENCLAW else IdentityKind.HUMAN
        if kind == IdentityKind.SERVICE:
            if password is not None:
                raise ValueError("OpenClaw cannot receive a human password")
            password_hash = None
        else:
            password_hash = self.passwords.hash_password(password)
        now, user_id = self._now(), str(uuid4())
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO auth_users(user_id,username,display_name,kind,role,password_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, username.lower(), display_name.strip(), kind.value, role.value, password_hash, now),
            )
            self._audit(connection, "IDENTITY_PROVISIONED", now, subject=user_id, details={"role": role.value})
            return _user(connection.execute("SELECT * FROM auth_users WHERE user_id=?", (user_id,)).fetchone())

    def begin_login(self, username: str, password: str, *, client: TrustedClientContext) -> LoginChallenge:
        if not isinstance(username, str) or not 1 <= len(username) <= 64:
            raise AuthenticationError("authentication failed")
        username = username.lower()
        now = self._now()
        with self.repository.transaction() as connection:
            allowed = self._reserve(connection, "PASSWORD", username, client, now)
            retry_after = self._retry_after(connection, "PASSWORD", username, client, now) if not allowed else None
            row = connection.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
        if not allowed:
            raise RateLimitError("authentication temporarily limited", retry_after=retry_after)
        # A peer may upgrade the same hash. Reverify its replacement once, outside
        # the write lock. Never treat an arbitrary changed hash as verified, and
        # never cross an auth-version change (e.g. MFA reset) on a retry.
        original = row
        for attempt in range(2):
            human = row is not None and row["kind"] == "HUMAN" and row["enabled"] == 1
            valid = self.passwords.verify(row["password_hash"] if human else None, password)
            replacement = None
            if valid and self.passwords.check_needs_rehash(row["password_hash"]):
                replacement = self.passwords.hash_password(password)
            now = self._now()
            with self.repository.transaction() as connection:
                current = connection.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
                if not valid or current is None or not current["enabled"] or current["kind"] != "HUMAN" or current["user_id"] != original["user_id"] or current["auth_version"] != original["auth_version"]:
                    retry = False
                elif current["password_hash"] != row["password_hash"]:
                    retry = attempt == 0
                else:
                    if replacement is not None:
                        updated = connection.execute(
                            "UPDATE auth_users SET password_hash=? "
                            "WHERE user_id=? AND auth_version=? AND password_hash=? AND enabled=1",
                            (replacement, current["user_id"], current["auth_version"], row["password_hash"]),
                        )
                        if updated.rowcount != 1:
                            raise AuthenticationError("authentication failed")
                        self._audit(connection, "PASSWORD_REHASHED", now, subject=current["user_id"])
                    token = secrets.token_urlsafe(32)
                    purpose = "LOGIN" if current["mfa_enabled"] else "ENROLL"
                    connection.execute(
                        "INSERT INTO auth_challenges(token_hash,user_id,auth_version,purpose,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                        (token_digest(token), current["user_id"], current["auth_version"], purpose, now, now + self.policy.challenge_seconds),
                    )
                    self._audit(connection, "PASSWORD_VERIFIED", now, subject=current["user_id"])
                    return LoginChallenge(token=SecretStr(token), requires_enrollment=purpose == "ENROLL", expires_at=_utc(now + self.policy.challenge_seconds))
                if not retry:
                    self._audit(connection, "PASSWORD_REJECTED", now, subject=original["user_id"] if original else None)
            if not retry:
                raise AuthenticationError("authentication failed")
            row = current
        raise AuthenticationError("authentication failed")

    def _challenge(self, connection, token, purpose, now):
        digest = self._opaque_digest(token)
        row = connection.execute("SELECT * FROM auth_challenges WHERE token_hash=?", (digest,)).fetchone()
        if row is None:
            return None, None
        user = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (row["user_id"],)).fetchone()
        if row["purpose"] != purpose or row["consumed_at"] is not None or not row["created_at"] <= now < row["expires_at"] or row["auth_version"] != user["auth_version"] or not user["enabled"] or user["kind"] != "HUMAN":
            return None, None
        return row, user

    def begin_mfa_enrollment(self, challenge_token: str) -> MfaEnrollment:
        now = self._now()
        with self.repository.transaction() as connection:
            challenge, user = self._challenge(connection, challenge_token, "ENROLL", now)
            if challenge is None or user["mfa_enabled"]:
                raise AuthenticationError("authentication failed")
            existing = connection.execute("SELECT * FROM auth_enrollments WHERE user_id=?", (user["user_id"],)).fetchone()
            if existing and existing["challenge_hash"] == challenge["token_hash"] and existing["expires_at"] > now:
                secret = self.cipher.decrypt(user["user_id"], existing["totp_ciphertext"])
            else:
                secret = pyotp.random_base32()
                connection.execute("DELETE FROM auth_enrollments WHERE user_id=?", (user["user_id"],))
                connection.execute("INSERT INTO auth_enrollments VALUES (?,?,?,?,?)", (user["user_id"], challenge["token_hash"], user["auth_version"], self.cipher.encrypt(user["user_id"], secret), challenge["expires_at"]))
                self._audit(connection, "MFA_ENROLLMENT_STARTED", now, subject=user["user_id"])
            return MfaEnrollment(secret=SecretStr(secret), provisioning_uri=SecretStr(pyotp.TOTP(secret).provisioning_uri(user["username"], issuer_name="Carbit Oy")), expires_at=_utc(challenge["expires_at"]))

    def _new_session(self, connection, user, now, factor) -> SessionGrant:
        token, session_id = secrets.token_urlsafe(32), str(uuid4())
        expiry = now + self.policy.session_seconds
        connection.execute(
            "INSERT INTO auth_sessions(session_id,token_hash,user_id,auth_version,factor,created_at,mfa_verified_at,expires_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, token_digest(token), user["user_id"], user["auth_version"], factor, now, now, expiry, now),
        )
        return SessionGrant(token=SecretStr(token), user=_user(user), session=SessionRecord(session_id=session_id, user_id=user["user_id"], auth_version=user["auth_version"], factor=factor, created_at=_utc(now), mfa_verified_at=_utc(now), expires_at=_utc(expiry)))

    def confirm_mfa_enrollment(self, challenge_token: str, code: str, *, client: TrustedClientContext) -> EnrollmentGrant:
        now = self._now()
        with self.repository.transaction() as connection:
            challenge, user = self._challenge(connection, challenge_token, "ENROLL", now)
            account = user["user_id"] if user else "unknown"
            allowed = self._reserve(connection, "MFA", account, client, now)
            retry_after = self._retry_after(connection, "MFA", account, client, now) if not allowed else None
            pending = connection.execute("SELECT * FROM auth_enrollments WHERE challenge_hash=?", (self._opaque_digest(challenge_token),)).fetchone()
            step = None
            if allowed and user and not user["mfa_enabled"] and pending and pending["auth_version"] == user["auth_version"] and pending["expires_at"] > now:
                step = matching_totp_step(self.cipher.decrypt(user["user_id"], pending["totp_ciphertext"]), code, now, None)
            if step is None:
                if allowed:
                    self._audit(connection, "MFA_REJECTED", now, subject=user["user_id"] if user else None)
                result = None
            else:
                connection.execute("UPDATE auth_users SET mfa_enabled=1,totp_ciphertext=?,last_totp_step=? WHERE user_id=?", (pending["totp_ciphertext"], step, user["user_id"]))
                connection.execute("DELETE FROM auth_enrollments WHERE user_id=?", (user["user_id"],))
                connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL", (now, user["user_id"]))
                codes = new_recovery_codes()
                connection.executemany("INSERT INTO auth_recovery_codes(user_id,auth_version,code_hash) VALUES (?,?,?)", [(user["user_id"], user["auth_version"], recovery_digest(user["user_id"], item)) for item in codes])
                current = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (user["user_id"],)).fetchone()
                grant = self._new_session(connection, current, now, "TOTP")
                self._audit(connection, "MFA_ENROLLED", now, subject=user["user_id"])
                self._audit(connection, "SESSION_GRANTED", now, subject=user["user_id"], details={"factor": "TOTP"})
                result = EnrollmentGrant(session=grant, recovery_codes=tuple(SecretStr(item) for item in codes))
        if not allowed:
            raise RateLimitError("authentication temporarily limited", retry_after=retry_after)
        if result is None:
            raise AuthenticationError("authentication failed")
        return result

    def complete_login(self, challenge_token: str, *, client: TrustedClientContext, totp_code: str | None = None, recovery_code: str | None = None) -> SessionGrant:
        now = self._now()
        with self.repository.transaction() as connection:
            challenge, user = self._challenge(connection, challenge_token, "LOGIN", now)
            account = user["user_id"] if user else "unknown"
            allowed = self._reserve(connection, "MFA", account, client, now)
            retry_after = self._retry_after(connection, "MFA", account, client, now) if not allowed else None
            valid, factor = False, "TOTP"
            if allowed and user and user["mfa_enabled"] and ((totp_code is None) != (recovery_code is None)):
                if totp_code is not None:
                    step = matching_totp_step(self.cipher.decrypt(user["user_id"], user["totp_ciphertext"]), totp_code, now, user["last_totp_step"])
                    if step is not None:
                        connection.execute("UPDATE auth_users SET last_totp_step=? WHERE user_id=?", (step, user["user_id"]))
                        valid = True
                else:
                    digest = recovery_digest(user["user_id"], recovery_code)
                    valid = connection.execute("UPDATE auth_recovery_codes SET consumed_at=? WHERE user_id=? AND auth_version=? AND code_hash=? AND consumed_at IS NULL", (now, user["user_id"], user["auth_version"], digest)).rowcount == 1
                    factor = "RECOVERY"
            if not valid:
                if allowed:
                    self._audit(connection, "MFA_REJECTED", now, subject=user["user_id"] if user else None)
                result = None
            else:
                connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE token_hash=?", (now, challenge["token_hash"]))
                result = self._new_session(connection, user, now, factor)
                self._audit(connection, "SESSION_GRANTED", now, subject=user["user_id"], details={"factor": factor})
        if not allowed:
            raise RateLimitError("authentication temporarily limited", retry_after=retry_after)
        if result is None:
            raise AuthenticationError("authentication failed")
        return result

    def _session(self, connection, token: str, now: int):
        session = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (self._opaque_digest(token),)).fetchone()
        return self._validate_session_row(connection, session, now)

    def _validate_session_row(self, connection, session, now: int):
        """One validator for bearer-token checks and guarded server session IDs."""
        user = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (session["user_id"],)).fetchone() if session else None
        if session is None or user is None or session["revoked_at"] is not None or not session["created_at"] <= now < session["expires_at"] or now >= session["last_seen_at"] + self.policy.idle_seconds or not user["enabled"] or not user["mfa_enabled"] or user["kind"] != "HUMAN" or session["auth_version"] != user["auth_version"]:
            raise AuthenticationError("authentication failed")
        return session, user

    def require_session(self, token: str) -> User:
        return self.inspect_session(token, touch=True).user

    def inspect_session(self, token: str, *, touch: bool = False) -> ValidatedSession:
        """Validate before optionally refreshing idle time; never extend absolute time."""
        with self.repository.transaction() as connection:
            now = self._now()
            session, user = self._session(connection, token, now)
            last_seen = session["last_seen_at"]
            if touch:
                last_seen = now
                connection.execute("UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?", (now, session["session_id"]))
            return ValidatedSession(_user(user), _utc(session["expires_at"]),
                                    _utc(min(session["expires_at"], last_seen + self.policy.idle_seconds)),
                                    session["session_id"])

    def refresh_session_for_roles(self, token: str, *, allowed_roles: frozenset[UserRole]) -> bool:
        """After an accepted operation, atomically revalidate identity/role and refresh.

        This does not authorize the operation itself. BEGIN IMMEDIATE serializes
        the checks and update with role changes and revocation; sample time only
        after acquiring the lock so a queued refresh cannot revive an expiry.
        """
        with self.repository.transaction() as connection:
            now = self._now()
            try:
                session, user = self._session(connection, token, now)
            except AuthenticationError:
                return False
            if UserRole(user["role"]) not in allowed_roles:
                return False
            connection.execute("UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?", (now, session["session_id"]))
            return True

    def revoke_session(self, token: str) -> None:
        now = self._now()
        with self.repository.transaction() as connection:
            session, user = self._session(connection, token, now)
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE session_id=?", (now, session["session_id"]))
            self._audit(connection, "SESSION_REVOKED", now, actor=user["user_id"], subject=user["user_id"])

    def reset_user_mfa(self, admin_session_token: str, request: MfaResetRequest) -> MfaResetResult:
        # Browser-supplied role/reviewer/timestamp cannot authorize this call.
        request = MfaResetRequest.model_validate(request.model_dump() if isinstance(request, MfaResetRequest) else request)
        try:
            return self._reset_user_mfa(admin_session_token, request)
        except (AuthenticationError, AuthVersionConflictError, AuthIdempotencyConflictError):
            # Failed authorization is auditable, but never commits reset side effects.
            with self.repository.transaction() as connection:
                actor = connection.execute("SELECT user_id FROM auth_sessions WHERE token_hash=?", (self._opaque_digest(admin_session_token),)).fetchone()
                target = connection.execute("SELECT user_id FROM auth_users WHERE user_id=?", (str(request.target_user_id),)).fetchone()
                self._audit(connection, "MFA_RESET_REJECTED", self._now(), actor=actor[0] if actor else None, subject=target[0] if target else None)
            raise

    def _reset_user_mfa(self, admin_session_token: str, request: MfaResetRequest) -> MfaResetResult:
        payload, now = request.model_dump_json(), self._now()
        with self.repository.transaction() as connection:
            session, admin = self._session(connection, admin_session_token, now)
            if admin["role"] != UserRole.IT_ADMIN.value or session["factor"] != "TOTP" or not 0 <= now - session["mfa_verified_at"] < self.policy.admin_mfa_fresh_seconds:
                raise PermissionDeniedError("a recent administrator TOTP session is required")
            previous = connection.execute("SELECT * FROM auth_reset_requests WHERE actor_user_id=? AND request_id=?", (admin["user_id"], str(request.request_id))).fetchone()
            if previous:
                if previous["request_json"] != payload:
                    raise AuthIdempotencyConflictError("recovery request key has different content")
                return MfaResetResult.model_validate_json(previous["result_json"])
            target = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (str(request.target_user_id),)).fetchone()
            if target is None or target["kind"] != "HUMAN" or target["role"] not in (UserRole.OWNER.value, UserRole.SERVICE_MANAGER.value):
                raise PermissionDeniedError("administrator/service recovery requires a separate procedure")
            if target["auth_version"] != request.expected_auth_version:
                raise AuthVersionConflictError("authentication state has changed")
            connection.execute("UPDATE auth_users SET mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL,auth_version=auth_version+1 WHERE user_id=?", (target["user_id"],))
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, target["user_id"]))
            connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL", (now, target["user_id"]))
            connection.execute("DELETE FROM auth_enrollments WHERE user_id=?", (target["user_id"],))
            connection.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (target["user_id"],))
            event_id = self._audit(connection, "MFA_RESET", now, actor=admin["user_id"], subject=target["user_id"], details={"identity_verification_reference": request.identity_verification_reference, "reason": request.reason, "previous_auth_version": target["auth_version"]})
            notification_id = str(uuid4())
            connection.execute("INSERT INTO auth_notification_outbox(notification_id,user_id,event_id,message_type,created_at) VALUES (?,?,?,?,?)", (notification_id, target["user_id"], event_id, "MFA_RESET", now))
            result = MfaResetResult(target_user_id=target["user_id"], auth_version=target["auth_version"] + 1, audit_event_id=event_id, notification_id=notification_id)
            connection.execute("INSERT INTO auth_reset_requests VALUES (?,?,?,?)", (admin["user_id"], str(request.request_id), payload, result.model_dump_json()))
            return result
