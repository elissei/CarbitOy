"""Local MFA recovery for an OWNER or SERVICE_MANAGER, bound to a one-time grant. Never a route.

Chain: a recently TOTP-verified IT_ADMIN session authorizes the recovery after an
in-person identity check. The check is a recorded statement; the program cannot
verify it, and neither a name nor the reference grants anything. One transaction
removes the old factor, recovery codes, sessions, challenges and pending
enrollment, raises ``auth_version``, sets ``mfa_recovery_required`` and issues a
short-lived single-use grant (only its hash is stored). While that flag is set,
``AuthService.begin_login`` never issues an enrollment challenge, so the password
alone cannot enroll a factor. The user then presents the grant, their own
password and a working code from a new TOTP. Completion clears the flag, stores
the factor and issues new recovery codes. No session is issued; the first
session comes from a normal login. Notification rows record intent only.

Interruption: the grant stays usable until it expires; a pending TOTP secret can
be completed with the same grant or replaced by repeating enrollment. Expiry
keeps the account in the recovery-required state. A new recovery needs a new
IT_ADMIN authorization and request ID; it supersedes an open grant explicitly.
"""

import secrets
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

import pyotp
from pydantic import Field, SecretStr, field_validator

from webapp.admin_offline_recovery import _bounded_reference
from webapp.auth_crypto import matching_totp_step, new_recovery_codes, recovery_digest, token_digest
from webapp.auth_models import AuthRecord, UserRole
from webapp.auth_service import (
    AuthenticationError, AuthIdempotencyConflictError, AuthService, AuthVersionConflictError,
    PermissionDeniedError, RateLimitError, TrustedClientContext,
)


GRANT_SECONDS = 15 * 60  # Same short validity as the approved offline recovery (D4).
PROCEDURE_VERSION = "user-mfa-recovery-v1"
PURPOSE = "MFA_RECOVERY_ENROLLMENT"
RECOVERABLE_ROLES = frozenset({UserRole.OWNER.value, UserRole.SERVICE_MANAGER.value})
CLOCK_SKEW_SECONDS = 300


class UserRecoveryUnavailable(AuthenticationError):
    """The grant is expired, superseded, completed or otherwise no longer usable."""


class UserMfaRecoveryRequest(AuthRecord):
    """The authorizing IT_ADMIN comes from the server session, never from this request."""

    target_user_id: UUID
    expected_auth_version: int = Field(ge=1, strict=True)
    request_id: UUID
    method: Literal["IN_PERSON_ID_DOCUMENT"] = "IN_PERSON_ID_DOCUMENT"
    identity_verification_reference: str = Field(min_length=3, max_length=200)
    verified_at: datetime
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("identity_verification_reference")
    @classmethod
    def bounded_reference(cls, value: str) -> str:
        return _bounded_reference(value)

    @field_validator("verified_at")
    @classmethod
    def aware_verification_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("verification time requires a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a recovery reason is required")
        return value.strip()


class UserRecoveryState(AuthRecord):
    """Truthful grant state. ``grant_token`` is present only on the first start."""

    request_id: UUID
    target_user_id: UUID
    status: Literal["OPEN", "COMPLETED", "EXPIRED", "SUPERSEDED"]
    recovery_auth_version: int
    expires_at: datetime
    replayed: bool
    superseded_request_id: UUID | None = None
    grant_token: SecretStr | None = None


class UserRecoveryEnrollment(AuthRecord):
    secret: SecretStr
    provisioning_uri: SecretStr
    expires_at: datetime
    replaced_previous_secret: bool


class UserRecoveryCompletion(AuthRecord):
    """Recovery codes are shown once; a replay returns none and grants no session."""

    request_id: UUID
    target_user_id: UUID
    status: Literal["COMPLETED", "ALREADY_COMPLETED"]
    auth_version: int
    recovery_codes: tuple[SecretStr, ...]


def _utc(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _row(connection, request_id):
    return connection.execute("SELECT * FROM auth_user_recovery_grants WHERE request_id=?", (str(request_id),)).fetchone()


def _state(row, *, replayed: bool, token: str | None = None, superseded=None) -> UserRecoveryState:
    return UserRecoveryState(
        request_id=row["request_id"], target_user_id=row["target_user_id"], status=row["status"],
        recovery_auth_version=row["recovery_auth_version"], expires_at=_utc(row["expires_at"]),
        replayed=replayed, superseded_request_id=superseded, grant_token=SecretStr(token) if token else None,
    )


def _finish(service, connection, row, now, status, event):
    connection.execute("UPDATE auth_user_recovery_grants SET status=?,finished_at=?,pending_totp_ciphertext=NULL "
                       "WHERE request_id=? AND status='OPEN'", (status, now, row["request_id"]))
    service._audit(connection, event, now, subject=row["target_user_id"], details={"request_id": row["request_id"]})
    return _row(connection, row["request_id"])


def _expire(service, connection, row, now):
    return _finish(service, connection, row, now, "EXPIRED", "USER_MFA_RECOVERY_EXPIRED")


def start_user_mfa_recovery(service: AuthService, admin_session_token: str, request) -> UserRecoveryState:
    """Reset the target's factor and issue a grant bound to target, request, version and purpose.

    A replay of the same request by the same IT_ADMIN returns the current state
    without a token: only its hash exists. Rejected authorization is audited
    without committing any recovery side effect.
    """
    request = UserMfaRecoveryRequest.model_validate(
        request.model_dump() if isinstance(request, UserMfaRecoveryRequest) else request)
    try:
        return _start(service, admin_session_token, request)
    except (AuthenticationError, AuthVersionConflictError, AuthIdempotencyConflictError):
        with service.repository.transaction() as connection:
            actor = connection.execute("SELECT user_id FROM auth_sessions WHERE token_hash=?",
                                       (service._opaque_digest(admin_session_token),)).fetchone()
            target = connection.execute("SELECT user_id FROM auth_users WHERE user_id=?",
                                        (str(request.target_user_id),)).fetchone()
            service._audit(connection, "USER_MFA_RECOVERY_REJECTED", service._now(),
                           actor=actor[0] if actor else None, subject=target[0] if target else None,
                           details={"request_id": str(request.request_id), "stage": "START"})
        raise


def _start(service, admin_session_token, request) -> UserRecoveryState:
    payload = request.model_dump_json()
    with service.repository.transaction() as connection:
        now = service._now()
        _, admin = service._require_recent_admin(connection, admin_session_token, now)
        if request.verified_at.timestamp() > now + CLOCK_SKEW_SECONDS:
            raise ValueError("identity verification cannot be in the future")
        previous = _row(connection, request.request_id)
        if previous is not None:
            if previous["actor_user_id"] != admin["user_id"] or previous["request_json"] != payload:
                raise AuthIdempotencyConflictError("recovery request key has different content")
            if previous["status"] == "OPEN" and now >= previous["expires_at"]:
                previous = _expire(service, connection, previous, now)
            return _state(previous, replayed=True)
        target = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (str(request.target_user_id),)).fetchone()
        if target is None or target["kind"] != "HUMAN" or target["role"] not in RECOVERABLE_ROLES or not target["enabled"]:
            raise PermissionDeniedError("only an enabled OWNER or SERVICE_MANAGER account is recovered here")
        if target["auth_version"] != request.expected_auth_version:
            raise AuthVersionConflictError("authentication state has changed")
        superseded = None
        open_row = connection.execute("SELECT * FROM auth_user_recovery_grants WHERE target_user_id=? AND status='OPEN'",
                                      (target["user_id"],)).fetchone()
        if open_row is not None:
            if now >= open_row["expires_at"]:
                _expire(service, connection, open_row, now)
            else:
                _finish(service, connection, open_row, now, "SUPERSEDED", "USER_MFA_RECOVERY_SUPERSEDED")
                superseded = open_row["request_id"]
        service._invalidate_mfa(connection, target, now)
        token = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO auth_user_recovery_grants (request_id,actor_user_id,target_user_id,request_json,purpose,"
            "previous_auth_version,recovery_auth_version,grant_hash,created_at,expires_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'OPEN')",
            (str(request.request_id), admin["user_id"], target["user_id"], payload, PURPOSE, target["auth_version"],
             target["auth_version"] + 1, token_digest(token), now, now + GRANT_SECONDS))
        event_id = service._audit(connection, "USER_MFA_RECOVERY_STARTED", now, actor=admin["user_id"],
                                  subject=target["user_id"], details={
            "request_id": str(request.request_id), "procedure_version": PROCEDURE_VERSION, "purpose": PURPOSE,
            "method": request.method, "identity_verification_reference": request.identity_verification_reference,
            "verified_at": request.verified_at.isoformat(), "identity_check_verified_by_program": False,
            "reason": request.reason, "previous_auth_version": target["auth_version"],
            "expires_at": now + GRANT_SECONDS, "superseded_request_id": superseded,
        })
        connection.execute("INSERT INTO auth_notification_outbox(notification_id,user_id,event_id,message_type,created_at) "
                           "VALUES (?,?,?,'MFA_RESET',?)", (str(uuid4()), target["user_id"], event_id, now))
        return _state(_row(connection, request.request_id), replayed=False, token=token, superseded=superseded)


def _grant(service, connection, grant_token):
    """Return (grant row, user row) for a known grant; never reveals unknown grants."""
    row = connection.execute("SELECT * FROM auth_user_recovery_grants WHERE grant_hash=?",
                             (service._opaque_digest(grant_token),)).fetchone()
    if row is None:
        return None, None
    return row, connection.execute("SELECT * FROM auth_users WHERE user_id=?", (row["target_user_id"],)).fetchone()


def _usable(row, user, now) -> bool:
    return (row["status"] == "OPEN" and now < row["expires_at"] and row["purpose"] == PURPOSE
            and user["kind"] == "HUMAN" and user["role"] in RECOVERABLE_ROLES and user["enabled"]
            and not user["mfa_enabled"] and user["mfa_recovery_required"]
            and user["auth_version"] == row["recovery_auth_version"])


def _reserve_attempt(service, connection, row, client, now):
    account = row["target_user_id"] if row is not None else "unknown"
    allowed = service._reserve(connection, "USER_MFA_RECOVERY", account, client, now)
    retry_after = service._retry_after(connection, "USER_MFA_RECOVERY", account, client, now) if not allowed else None
    return allowed, retry_after


def begin_user_recovery_enrollment(service: AuthService, grant_token: str, password: str, *,
                                   client: TrustedClientContext) -> UserRecoveryEnrollment:
    """Verify the grant and the user's own password; return a new TOTP secret.

    No session is issued. Repeating this step replaces the pending secret
    (reported as ``replaced_previous_secret``); the earlier one no longer completes.
    """
    with service.repository.transaction() as connection:
        now = service._now()
        row, user = _grant(service, connection, grant_token)
        allowed, retry_after = _reserve_attempt(service, connection, row, client, now)
        if allowed and row is not None and row["status"] == "OPEN" and now >= row["expires_at"]:
            row = _expire(service, connection, row, now)
    if not allowed:
        raise RateLimitError("recovery temporarily limited", retry_after=retry_after)
    if row is None:
        raise AuthenticationError("authentication failed")
    if not _usable(row, user, now):
        raise UserRecoveryUnavailable("recovery authorization is no longer valid; a new recovery is required")
    # Password hashing runs outside the writer reservation; the state is rechecked below.
    valid_password = service.passwords.verify(user["password_hash"], password)
    with service.repository.transaction() as connection:
        now = service._now()
        current, current_user = _grant(service, connection, grant_token)
        if current is None or not _usable(current, current_user, now) or (
                current_user["password_hash"] != user["password_hash"]):
            raise UserRecoveryUnavailable("recovery authorization is no longer valid; a new recovery is required")
        if not valid_password:
            service._audit(connection, "USER_MFA_RECOVERY_REJECTED", now, subject=current_user["user_id"],
                           details={"request_id": current["request_id"], "stage": "PASSWORD"})
            result = None
        else:
            secret = pyotp.random_base32()
            replaced = current["pending_totp_ciphertext"] is not None
            connection.execute("UPDATE auth_user_recovery_grants SET pending_totp_ciphertext=? WHERE request_id=?",
                               (service.cipher.encrypt(current_user["user_id"], secret), current["request_id"]))
            service._audit(connection, "USER_MFA_RECOVERY_ENROLLMENT_STARTED", now, subject=current_user["user_id"],
                           details={"request_id": current["request_id"], "replaced_previous_secret": replaced})
            result = UserRecoveryEnrollment(
                secret=SecretStr(secret),
                provisioning_uri=SecretStr(pyotp.TOTP(secret).provisioning_uri(current_user["username"],
                                                                               issuer_name="Carbit Oy")),
                expires_at=_utc(current["expires_at"]), replaced_previous_secret=replaced)
    if result is None:
        raise AuthenticationError("authentication failed")
    return result


def complete_user_mfa_recovery(service: AuthService, grant_token: str, totp_code: str, *,
                               client: TrustedClientContext) -> UserRecoveryCompletion:
    """Confirm the new TOTP, store it and clear the recovery-required state in one transaction.

    The conditional update makes completion single-use under concurrency. A
    replay of the consumed grant reports the completed state without codes.
    """
    with service.repository.transaction() as connection:
        now = service._now()
        row, user = _grant(service, connection, grant_token)
        allowed, retry_after = _reserve_attempt(service, connection, row, client, now)
        outcome, result = "failed", None
        if allowed and row is not None:
            if row["status"] == "COMPLETED":
                outcome = "replay"
                result = UserRecoveryCompletion(
                    request_id=row["request_id"], target_user_id=row["target_user_id"], status="ALREADY_COMPLETED",
                    auth_version=row["recovery_auth_version"], recovery_codes=())
            elif row["status"] == "OPEN" and now >= row["expires_at"]:
                _expire(service, connection, row, now)
                outcome = "unavailable"
            elif not _usable(row, user, now) or row["pending_totp_ciphertext"] is None:
                outcome = "unavailable"
            else:
                secret = service.cipher.decrypt(user["user_id"], row["pending_totp_ciphertext"])
                step = matching_totp_step(secret, totp_code, now, None)
                if step is None:
                    service._audit(connection, "USER_MFA_RECOVERY_REJECTED", now, subject=user["user_id"],
                                   details={"request_id": row["request_id"], "stage": "TOTP"})
                else:
                    enrolled = connection.execute(
                        "UPDATE auth_users SET mfa_enabled=1,totp_ciphertext=?,last_totp_step=?,mfa_recovery_required=0 "
                        "WHERE user_id=? AND auth_version=? AND enabled=1 AND mfa_enabled=0 AND mfa_recovery_required=1",
                        (row["pending_totp_ciphertext"], step, user["user_id"], row["recovery_auth_version"]))
                    if enrolled.rowcount != 1:
                        raise AuthVersionConflictError("authentication state has changed")
                    connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL",
                                       (now, user["user_id"]))
                    codes = new_recovery_codes()
                    connection.executemany(
                        "INSERT INTO auth_recovery_codes(user_id,auth_version,code_hash) VALUES (?,?,?)",
                        [(user["user_id"], row["recovery_auth_version"], recovery_digest(user["user_id"], code))
                         for code in codes])
                    connection.execute("UPDATE auth_user_recovery_grants SET status='COMPLETED',finished_at=?,"
                                       "pending_totp_ciphertext=NULL WHERE request_id=? AND status='OPEN'",
                                       (now, row["request_id"]))
                    service._audit(connection, "USER_MFA_RECOVERY_COMPLETED", now, subject=user["user_id"],
                                   details={"request_id": row["request_id"]})
                    outcome = "completed"
                    result = UserRecoveryCompletion(
                        request_id=row["request_id"], target_user_id=user["user_id"], status="COMPLETED",
                        auth_version=row["recovery_auth_version"],
                        recovery_codes=tuple(SecretStr(code) for code in codes))
    if not allowed:
        raise RateLimitError("recovery temporarily limited", retry_after=retry_after)
    if outcome == "unavailable":
        raise UserRecoveryUnavailable("recovery authorization is no longer valid; a new recovery is required")
    if result is None:
        raise AuthenticationError("authentication failed")
    return result


def recovery_status(connection, target_user_id: str) -> list:
    """Grant rows for a target, without hashes or ciphertexts (for the console status view)."""
    return connection.execute(
        "SELECT request_id,status,created_at,expires_at,finished_at,pending_totp_ciphertext IS NOT NULL AS pending "
        "FROM auth_user_recovery_grants WHERE target_user_id=? ORDER BY created_at, rowid", (target_user_id,)).fetchall()
