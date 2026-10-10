"""Internal authentication contracts; no HTTP routes or business-context adapter."""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class UserRole(str, Enum):
    OWNER = "OWNER"
    SERVICE_MANAGER = "SERVICE_MANAGER"
    IT_ADMIN = "IT_ADMIN"
    OPENCLAW = "OPENCLAW"


class IdentityKind(str, Enum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


class AuthRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "expires_at", "mfa_verified_at", check_fields=False)
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("authentication timestamps require a timezone")
        return value.astimezone(timezone.utc)


class User(AuthRecord):
    user_id: UUID
    username: str
    display_name: str
    role: UserRole
    kind: IdentityKind
    enabled: bool
    mfa_enabled: bool
    auth_version: int = Field(ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def valid_identity(self) -> "User":
        if (self.kind == IdentityKind.SERVICE) != (self.role == UserRole.OPENCLAW):
            raise ValueError("service and human roles must remain separate")
        if self.kind == IdentityKind.SERVICE and self.mfa_enabled:
            raise ValueError("service identities cannot use human MFA")
        return self

    @property
    def can_approve_labour(self) -> bool:
        # Eligibility only. This model is not an authenticated workshop context.
        return self.enabled and self.mfa_enabled and self.kind == IdentityKind.HUMAN and self.role in {
            UserRole.OWNER, UserRole.SERVICE_MANAGER,
        }


class LoginChallenge(AuthRecord):
    token: SecretStr
    requires_enrollment: bool
    expires_at: datetime


class MfaEnrollment(AuthRecord):
    secret: SecretStr
    provisioning_uri: SecretStr
    expires_at: datetime


class SessionRecord(AuthRecord):
    session_id: UUID
    user_id: UUID
    auth_version: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    mfa_verified_at: datetime
    factor: str = Field(pattern="^(TOTP|RECOVERY)$")

    @model_validator(mode="after")
    def valid_lifetime(self) -> "SessionRecord":
        if not self.created_at <= self.mfa_verified_at < self.expires_at:
            raise ValueError("session requires MFA before its expiry")
        return self


class SessionGrant(AuthRecord):
    token: SecretStr
    user: User
    session: SessionRecord


class EnrollmentGrant(AuthRecord):
    session: SessionGrant
    recovery_codes: tuple[SecretStr, ...]


class SessionCookiePolicy(AuthRecord):
    """Policy for a future HTTPS adapter; no cookie is issued by this phase."""

    name: str = Field(default="__Host-carbit-session", frozen=True)

    def options(self) -> dict:
        return {"httponly": True, "secure": True, "samesite": "strict", "path": "/"}

    @model_validator(mode="after")
    def require_host_cookie(self) -> "SessionCookiePolicy":
        if self.name != "__Host-carbit-session":
            raise ValueError("use the host-only secure session cookie")
        return self


class MfaResetRequest(AuthRecord):
    target_user_id: UUID
    expected_auth_version: int = Field(ge=1, strict=True)
    request_id: UUID
    identity_verification_reference: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recovery needs identity-verification evidence and a reason")
        return value.strip()

    @field_validator("identity_verification_reference")
    @classmethod
    def require_verification_reference(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("recovery needs an identity-verification reference")
        return value.strip()


class MfaResetResult(AuthRecord):
    target_user_id: UUID
    auth_version: int = Field(ge=1)
    audit_event_id: int = Field(ge=1)
    notification_id: UUID
