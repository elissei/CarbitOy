"""Trusted offline IT_ADMIN own-MFA emergency recovery. Never register these as routes.

For an IT_ADMIN who lost both the authenticator and the recovery codes but knows
the password. Forgotten passwords and other roles are out of scope; the existing
``AuthService.reset_user_mfa`` keeps rejecting IT_ADMIN targets.

Trust boundary: the caller has operating-system access to the stopped service
host. The recorded in-person approval is an audit record, not programmatic proof
that a physical identity check happened, and nothing here protects against a
malicious OS administrator who can change the database, keys or program.
Notification rows record intent only; no deliverer exists.

Two approval forms exist. The externally authorized form records a separate
authorizer and an in-person identity check. The approved local self-recovery
form records that the operator recovers their own IT_ADMIN account without an
external authorizer or identity check; its trust rests on OS access and control
of the recovery means, and its confirmations are operator statements.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

import pyotp
from pydantic import Discriminator, Field, SecretStr, Tag, field_validator

from webapp.auth_crypto import matching_totp_step, new_recovery_codes, recovery_digest, token_digest
from webapp.auth_models import AuthRecord, UserRole
from webapp.auth_service import (
    AuthenticationError, AuthIdempotencyConflictError, AuthService, AuthVersionConflictError,
    PermissionDeniedError, RateLimitError, TrustedClientContext,
)


GRANT_SECONDS = 15 * 60  # Approved D4: authorization is valid for 15 minutes.
PROCEDURE_VERSION = "admin-offline-mfa-recovery-v1"
IN_PERSON_METHOD = "IN_PERSON_ID_DOCUMENT"
SELF_RECOVERY_METHOD = "LOCAL_OPERATOR_SELF_RECOVERY"
CLOCK_SKEW_SECONDS = 300
# Approved D2 forbids storing national identity or document numbers. This is a
# conservative heuristic guard for the free-text references, not a guarantee.
_IDENTIFIER_LIKE = re.compile(r"\d{6}[-+A-FU-Ya-fu-y]\d{3}[0-9A-Ya-y]|\d{6,}")


def _bounded_reference(value: str) -> str:
    value = value.strip()
    if len(value) < 3:
        raise ValueError("approval references are required")
    if _IDENTIFIER_LIKE.search(value):
        raise ValueError("do not record identity or document numbers")
    return value


class AdminRecoveryUnavailable(AuthenticationError):
    """The authorization is expired, cancelled or otherwise no longer usable."""


class AdminRecoveryApproval(AuthRecord):
    """Approved D2: in-person check against an ID document; the document is not copied."""

    method: Literal["IN_PERSON_ID_DOCUMENT"] = "IN_PERSON_ID_DOCUMENT"
    authorizer_reference: str = Field(min_length=3, max_length=200)
    verified_person_reference: str = Field(min_length=3, max_length=200)
    verified_at: datetime
    approval_reference: str = Field(min_length=3, max_length=200)

    @field_validator("authorizer_reference", "verified_person_reference", "approval_reference")
    @classmethod
    def bounded_reference(cls, value: str) -> str:
        return _bounded_reference(value)

    @field_validator("verified_at")
    @classmethod
    def aware_verification_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("verification time requires a timezone")
        return value.astimezone(timezone.utc)


class AdminSelfRecoveryApproval(AuthRecord):
    """Approved D1 exception: the operator recovers their own IT_ADMIN account locally.

    No external authorizer and no in-person identity check exist in this form.
    Both confirmations must be given explicitly; they are the operator's
    statements, not facts this program can verify.
    """

    method: Literal["LOCAL_OPERATOR_SELF_RECOVERY"]
    operator_reference: str = Field(min_length=3, max_length=200)
    record_reference: str = Field(min_length=3, max_length=200)
    operator_confirmed_own_account: Literal[True]
    operator_confirmed_service_stopped: Literal[True]

    @field_validator("operator_reference", "record_reference")
    @classmethod
    def bounded_reference(cls, value: str) -> str:
        return _bounded_reference(value)


def _approval_form(value) -> str:
    # Self-recovery must be selected explicitly; anything else is validated as the
    # externally authorized form, whose method defaults to the in-person check.
    method = value.get("method") if isinstance(value, dict) else getattr(value, "method", None)
    return SELF_RECOVERY_METHOD if method == SELF_RECOVERY_METHOD else IN_PERSON_METHOD


class AdminRecoveryStartRequest(AuthRecord):
    target_user_id: UUID
    expected_auth_version: int = Field(ge=1, strict=True)
    request_id: UUID
    approval: Annotated[Union[Annotated[AdminRecoveryApproval, Tag(IN_PERSON_METHOD)],
                              Annotated[AdminSelfRecoveryApproval, Tag(SELF_RECOVERY_METHOD)]],
                        Discriminator(_approval_form)]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a recovery reason is required")
        return value.strip()


class AdminRecoveryState(AuthRecord):
    """Truthful request state. ``grant_token`` is present only on the first start."""

    request_id: UUID
    target_user_id: UUID
    status: Literal["OPEN", "COMPLETED", "CANCELLED", "EXPIRED"]
    recovery_auth_version: int
    expires_at: datetime
    replayed: bool
    grant_token: SecretStr | None = None


class AdminRecoveryEnrollment(AuthRecord):
    secret: SecretStr
    provisioning_uri: SecretStr
    expires_at: datetime
    replaced_previous_secret: bool


class AdminRecoveryCompletion(AuthRecord):
    """Recovery codes are shown once; a replay returns none and grants no session."""

    request_id: UUID
    target_user_id: UUID
    status: Literal["COMPLETED", "ALREADY_COMPLETED"]
    auth_version: int
    recovery_codes: tuple[SecretStr, ...]


def _utc(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _state(row, *, replayed: bool, token: str | None = None) -> AdminRecoveryState:
    return AdminRecoveryState(
        request_id=row["request_id"], target_user_id=row["target_user_id"], status=row["status"],
        recovery_auth_version=row["recovery_auth_version"], expires_at=_utc(row["expires_at"]),
        replayed=replayed, grant_token=SecretStr(token) if token else None,
    )


def _expire(service, connection, row, now):
    connection.execute("UPDATE auth_admin_recovery_requests SET status='EXPIRED',finished_at=?,"
                       "pending_totp_ciphertext=NULL WHERE request_id=? AND status='OPEN'",
                       (now, row["request_id"]))
    service._audit(connection, "ADMIN_OFFLINE_RECOVERY_EXPIRED", now, subject=row["target_user_id"],
                   details={"request_id": row["request_id"]})
    return connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE request_id=?",
                              (row["request_id"],)).fetchone()


def start_admin_offline_recovery(service: AuthService, request: AdminRecoveryStartRequest) -> AdminRecoveryState:
    """Atomically remove the old factor/sessions, close the account and issue a grant.

    The account stays closed (``enabled=0``) until completion, so neither the
    password alone nor an ordinary login can enroll a new factor. A replay of the
    same request returns the current state without a token: only its hash exists.
    """
    request = AdminRecoveryStartRequest.model_validate(
        request.model_dump() if isinstance(request, AdminRecoveryStartRequest) else request)
    try:
        return _start(service, request)
    except ValueError:  # a service rejection after model validation; not storage errors
        # The rolled-back start changed nothing; only the rejection is recorded. The
        # offline path has no authenticated actor, so none is claimed.
        with service.repository.transaction() as connection:
            target = connection.execute("SELECT user_id FROM auth_users WHERE user_id=?",
                                        (str(request.target_user_id),)).fetchone()
            service._audit(connection, "ADMIN_OFFLINE_RECOVERY_REJECTED", service._now(),
                           subject=target[0] if target else None,
                           details={"request_id": str(request.request_id), "stage": "START"})
        raise


def _start(service, request) -> AdminRecoveryState:
    payload = request.model_dump_json()
    with service.repository.transaction() as connection:
        now = service._now()
        if isinstance(request.approval, AdminRecoveryApproval) and (
                request.approval.verified_at.timestamp() > now + CLOCK_SKEW_SECONDS):
            raise ValueError("identity verification cannot be in the future")
        previous = connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE request_id=?",
                                      (str(request.request_id),)).fetchone()
        if previous is not None:
            if previous["request_json"] != payload:
                raise AuthIdempotencyConflictError("recovery request key has different content")
            if previous["status"] == "OPEN" and now >= previous["expires_at"]:
                previous = _expire(service, connection, previous, now)
            return _state(previous, replayed=True)
        target = connection.execute("SELECT * FROM auth_users WHERE user_id=?",
                                    (str(request.target_user_id),)).fetchone()
        if target is None or target["kind"] != "HUMAN" or target["role"] != UserRole.IT_ADMIN.value:
            raise PermissionDeniedError("only an IT_ADMIN's own offline recovery is supported here")
        if target["auth_version"] != request.expected_auth_version:
            raise AuthVersionConflictError("authentication state has changed")
        open_request = connection.execute("SELECT * FROM auth_admin_recovery_requests "
                                          "WHERE target_user_id=? AND status='OPEN'", (target["user_id"],)).fetchone()
        if open_request is not None:
            if now < open_request["expires_at"]:
                raise AuthIdempotencyConflictError("an open recovery exists; cancel it before a new attempt")
            _expire(service, connection, open_request, now)
        if not target["enabled"]:
            # Only an account closed by an unfinished recovery may be retried; a
            # recovery never reopens an account that was otherwise closed.
            latest = connection.execute("SELECT status,recovery_auth_version FROM auth_admin_recovery_requests "
                                        "WHERE target_user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                        (target["user_id"],)).fetchone()
            if latest is None or latest["status"] not in ("CANCELLED", "EXPIRED") or (
                    latest["recovery_auth_version"] != target["auth_version"]):
                raise PermissionDeniedError("a closed account is not reopened by recovery")
        changed = connection.execute(
            "UPDATE auth_users SET mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL,enabled=0,"
            "auth_version=auth_version+1 WHERE user_id=? AND auth_version=?",
            (target["user_id"], target["auth_version"]))
        if changed.rowcount != 1:
            raise AuthVersionConflictError("authentication state has changed")
        connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                           (now, target["user_id"]))
        connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL",
                           (now, target["user_id"]))
        connection.execute("DELETE FROM auth_enrollments WHERE user_id=?", (target["user_id"],))
        connection.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (target["user_id"],))
        token = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO auth_admin_recovery_requests (request_id,target_user_id,request_json,"
            "previous_auth_version,recovery_auth_version,grant_hash,created_at,expires_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,'OPEN')",
            (str(request.request_id), target["user_id"], payload, target["auth_version"],
             target["auth_version"] + 1, token_digest(token), now, now + GRANT_SECONDS))
        approval = request.approval
        if isinstance(approval, AdminSelfRecoveryApproval):
            approval_details = {
                "recovery_mode": "LOCAL_SELF_RECOVERY", "method": approval.method,
                "operator_reference": approval.operator_reference, "record_reference": approval.record_reference,
                "external_authorizer": None, "in_person_identity_check": None,
                "operator_confirmed_own_account": True, "operator_confirmed_service_stopped": True,
                "confirmations_verified_by_program": False,
            }
        else:
            approval_details = {
                "recovery_mode": "EXTERNALLY_AUTHORIZED", "method": approval.method,
                "authorizer_reference": approval.authorizer_reference,
                "verified_person_reference": approval.verified_person_reference,
                "verified_at": approval.verified_at.isoformat(), "approval_reference": approval.approval_reference,
            }
        event_id = service._audit(connection, "ADMIN_OFFLINE_RECOVERY_STARTED", now, subject=target["user_id"], details={
            "request_id": str(request.request_id), "procedure_version": PROCEDURE_VERSION, **approval_details,
            "reason": request.reason, "previous_auth_version": target["auth_version"],
            "expires_at": now + GRANT_SECONDS,
        })
        connection.execute("INSERT INTO auth_admin_recovery_outbox VALUES (?,?,?,'ADMIN_RECOVERY_STARTED',?,NULL)",
                           (str(uuid4()), str(request.request_id), event_id, now))
        row = connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE request_id=?",
                                 (str(request.request_id),)).fetchone()
        return _state(row, replayed=False, token=token)


def _grant(service, connection, grant_token, now):
    """Return (request row, user row) for a known grant; never reveals unknown grants."""
    row = connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE grant_hash=?",
                             (service._opaque_digest(grant_token),)).fetchone()
    if row is None:
        return None, None
    user = connection.execute("SELECT * FROM auth_users WHERE user_id=?", (row["target_user_id"],)).fetchone()
    return row, user


def _usable(row, user, now) -> bool:
    return (row["status"] == "OPEN" and now < row["expires_at"] and user["role"] == UserRole.IT_ADMIN.value
            and user["kind"] == "HUMAN" and user["auth_version"] == row["recovery_auth_version"]
            and not user["enabled"] and not user["mfa_enabled"])


def _reserve_attempt(service, connection, row, client, now):
    account = row["target_user_id"] if row is not None else "unknown"
    allowed = service._reserve(connection, "ADMIN_OFFLINE_RECOVERY", account, client, now)
    retry_after = service._retry_after(connection, "ADMIN_OFFLINE_RECOVERY", account, client, now) if not allowed else None
    return allowed, retry_after


def begin_admin_recovery_enrollment(service: AuthService, grant_token: str, password: str, *,
                                    client: TrustedClientContext) -> AdminRecoveryEnrollment:
    """Verify the grant and the administrator's own password; return a new TOTP secret.

    The account stays closed and no session is issued. Repeating this step
    replaces the pending secret (reported as ``replaced_previous_secret``); an
    earlier unconfirmed secret then no longer completes the recovery.
    """
    with service.repository.transaction() as connection:
        now = service._now()
        row, user = _grant(service, connection, grant_token, now)
        allowed, retry_after = _reserve_attempt(service, connection, row, client, now)
        expired = allowed and row is not None and row["status"] == "OPEN" and now >= row["expires_at"]
        if expired:
            row = _expire(service, connection, row, now)
    if not allowed:
        raise RateLimitError("recovery temporarily limited", retry_after=retry_after)
    if row is None:
        raise AuthenticationError("authentication failed")
    if not _usable(row, user, now):
        raise AdminRecoveryUnavailable("recovery authorization is no longer valid; start a new recovery")
    # Password hashing runs outside the writer reservation; the state is rechecked below.
    valid_password = service.passwords.verify(user["password_hash"], password)
    with service.repository.transaction() as connection:
        now = service._now()
        current, current_user = _grant(service, connection, grant_token, now)
        if current is None or not _usable(current, current_user, now) or (
                current_user["password_hash"] != user["password_hash"]):
            raise AdminRecoveryUnavailable("recovery authorization is no longer valid; start a new recovery")
        if not valid_password:
            service._audit(connection, "ADMIN_OFFLINE_RECOVERY_REJECTED", now, subject=current_user["user_id"],
                           details={"request_id": current["request_id"], "stage": "PASSWORD"})
            result = None
        else:
            secret = pyotp.random_base32()
            replaced = current["pending_totp_ciphertext"] is not None
            connection.execute("UPDATE auth_admin_recovery_requests SET pending_totp_ciphertext=? WHERE request_id=?",
                               (service.cipher.encrypt(current_user["user_id"], secret), current["request_id"]))
            service._audit(connection, "ADMIN_OFFLINE_RECOVERY_ENROLLMENT_STARTED", now,
                           subject=current_user["user_id"],
                           details={"request_id": current["request_id"], "replaced_previous_secret": replaced})
            result = AdminRecoveryEnrollment(
                secret=SecretStr(secret),
                provisioning_uri=SecretStr(pyotp.TOTP(secret).provisioning_uri(
                    current_user["username"], issuer_name="Carbit Oy")),
                expires_at=_utc(current["expires_at"]), replaced_previous_secret=replaced)
    if result is None:
        raise AuthenticationError("authentication failed")
    return result


def complete_admin_offline_recovery(service: AuthService, grant_token: str, totp_code: str, *,
                                    client: TrustedClientContext) -> AdminRecoveryCompletion:
    """Confirm the new TOTP with a working code, then reopen the account.

    One transaction stores the factor, issues ten new recovery codes, reopens
    the account, consumes the grant and records audit/notification intent. No
    login session is issued. A replay of the consumed grant reports the
    completed state without codes; lost codes require a new offline recovery.
    """
    with service.repository.transaction() as connection:
        now = service._now()
        row, user = _grant(service, connection, grant_token, now)
        allowed, retry_after = _reserve_attempt(service, connection, row, client, now)
        outcome, result = "failed", None
        if allowed and row is not None:
            if row["status"] == "COMPLETED":
                outcome = "replay"
                result = AdminRecoveryCompletion(
                    request_id=row["request_id"], target_user_id=row["target_user_id"],
                    status="ALREADY_COMPLETED", auth_version=row["recovery_auth_version"], recovery_codes=())
            elif row["status"] == "OPEN" and now >= row["expires_at"]:
                _expire(service, connection, row, now)
                outcome = "unavailable"
            elif not _usable(row, user, now) or row["pending_totp_ciphertext"] is None:
                outcome = "unavailable"
            else:
                secret = service.cipher.decrypt(user["user_id"], row["pending_totp_ciphertext"])
                step = matching_totp_step(secret, totp_code, now, None)
                if step is None:
                    service._audit(connection, "ADMIN_OFFLINE_RECOVERY_REJECTED", now, subject=user["user_id"],
                                   details={"request_id": row["request_id"], "stage": "TOTP"})
                else:
                    # This grant-bound enrollment also satisfies a recovery-required state
                    # (e.g. set by the version 4 upgrade after a legacy reset).
                    reopened = connection.execute(
                        "UPDATE auth_users SET mfa_enabled=1,totp_ciphertext=?,last_totp_step=?,enabled=1,"
                        "mfa_recovery_required=0 "
                        "WHERE user_id=? AND auth_version=? AND enabled=0 AND mfa_enabled=0",
                        (row["pending_totp_ciphertext"], step, user["user_id"], row["recovery_auth_version"]))
                    if reopened.rowcount != 1:
                        raise AuthVersionConflictError("authentication state has changed")
                    codes = new_recovery_codes()
                    connection.executemany(
                        "INSERT INTO auth_recovery_codes(user_id,auth_version,code_hash) VALUES (?,?,?)",
                        [(user["user_id"], row["recovery_auth_version"], recovery_digest(user["user_id"], code))
                         for code in codes])
                    connection.execute("UPDATE auth_admin_recovery_requests SET status='COMPLETED',finished_at=?,"
                                       "pending_totp_ciphertext=NULL WHERE request_id=? AND status='OPEN'",
                                       (now, row["request_id"]))
                    event_id = service._audit(connection, "ADMIN_OFFLINE_RECOVERY_COMPLETED", now,
                                              subject=user["user_id"], details={"request_id": row["request_id"]})
                    connection.execute(
                        "INSERT INTO auth_admin_recovery_outbox VALUES (?,?,?,'ADMIN_RECOVERY_COMPLETED',?,NULL)",
                        (str(uuid4()), row["request_id"], event_id, now))
                    outcome = "completed"
                    result = AdminRecoveryCompletion(
                        request_id=row["request_id"], target_user_id=user["user_id"], status="COMPLETED",
                        auth_version=row["recovery_auth_version"],
                        recovery_codes=tuple(SecretStr(code) for code in codes))
    if not allowed:
        raise RateLimitError("recovery temporarily limited", retry_after=retry_after)
    if outcome == "unavailable":
        raise AdminRecoveryUnavailable("recovery authorization is no longer valid; start a new recovery")
    if result is None:
        raise AuthenticationError("authentication failed")
    return result


def cancel_admin_offline_recovery(service: AuthService, request_id: UUID | str, *, reason: str) -> AdminRecoveryState:
    """Cancel an open recovery. The account stays closed until a new recovery completes."""
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError("a cancellation reason is required")
    request_id = str(UUID(str(request_id)))
    with service.repository.transaction() as connection:
        now = service._now()
        row = connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE request_id=?",
                                 (request_id,)).fetchone()
        if row is None:
            raise LookupError("unknown recovery request")
        if row["status"] == "COMPLETED":
            raise AuthIdempotencyConflictError("a completed recovery cannot be cancelled")
        if row["status"] != "OPEN":
            return _state(row, replayed=True)
        if now >= row["expires_at"]:
            return _state(_expire(service, connection, row, now), replayed=False)
        connection.execute("UPDATE auth_admin_recovery_requests SET status='CANCELLED',finished_at=?,"
                           "pending_totp_ciphertext=NULL WHERE request_id=? AND status='OPEN'", (now, request_id))
        service._audit(connection, "ADMIN_OFFLINE_RECOVERY_CANCELLED", now, subject=row["target_user_id"],
                       details={"request_id": request_id, "reason": reason.strip()})
        row = connection.execute("SELECT * FROM auth_admin_recovery_requests WHERE request_id=?",
                                 (request_id,)).fetchone()
        return _state(row, replayed=False)
