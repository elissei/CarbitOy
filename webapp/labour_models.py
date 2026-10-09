"""Persistence contracts for Web 4A, populated by the internal Web 4B service.

Identifiers and content hashes come from the trusted application layer.
Operation identifiers remain strings for persistence compatibility.
Decimal values serialize as exact JSON strings.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum

from pydantic import (
    BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator,
)

from domain.models.labour_time import LabourTime, LabourTimeSource


def decimal_storage_text(value: Decimal) -> str:
    """Exact fixed-point TEXT for SQLite, including unsigned zero.

    Preserve decimal places; never convert through float or SQLite REAL.
    """
    if not value.is_finite():
        raise ValueError("stored decimals must be finite")
    return format(value.copy_abs() if value.is_zero() else value, "f")


class CoverageStatus(str, Enum):
    READY = "READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReviewDecisionStatus(str, Enum):
    APPROVED_UNCHANGED = "APPROVED_UNCHANGED"
    APPROVED_ADJUSTED = "APPROVED_ADJUSTED"


class StorageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_text(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @field_validator(
        "original_hours", "original_total_hours", "final_billable_hours",
        mode="before", check_fields=False,
    )
    @classmethod
    def require_exact_decimal_input(cls, value):
        if isinstance(value, (bool, float)):
            raise ValueError("use Decimal or decimal text, not bool or float")
        return value

    @field_serializer(
        "original_hours", "original_total_hours", "final_billable_hours",
        check_fields=False, when_used="json",
    )
    def serialize_decimal_text(self, value: Decimal | None) -> str | None:
        return decimal_storage_text(value) if value is not None else None

    @field_validator("created_at", "reviewed_at", check_fields=False)
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class OriginalWorkTimeRecord(StorageRecord):
    """Full original work snapshot; missing time represents unfinished work."""

    work_item_id: str = Field(min_length=1)
    operation_identifier: str = Field(min_length=1)
    axle: str = Field(default="UNKNOWN", pattern="^(FRONT|REAR|BOTH|UNKNOWN)$")
    side: str = Field(
        default="UNKNOWN", pattern="^(LEFT|RIGHT|BOTH|NOT_APPLICABLE|UNKNOWN)$",
    )
    original_hours: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    source: LabourTimeSource | None = None
    work_description: str | None = None
    autodata_reference: str | None = None
    recorded_by: str | None = None

    @model_validator(mode="after")
    def validate_original(self) -> "OriginalWorkTimeRecord":
        if self.original_hours is None:
            if self.source is not None:
                raise ValueError("unfinished original time must not claim a source")
        else:
            if self.source is None:
                raise ValueError("original time requires its source")
            LabourTime(
                hours=self.original_hours, source=self.source,
                work_description=self.work_description,
                autodata_reference=self.autodata_reference,
                recorded_by=self.recorded_by,
            )
        return self


class LabourPlanRevisionRecord(StorageRecord):
    """One immutable revision; inquiry version is deliberately not its revision.

    Content identity is supplied by domain/app code, not recomputed using a
    different hashing policy here. READY means coverage, not human approval.
    """

    revision_id: str = Field(min_length=1)
    inquiry_id: str = Field(min_length=1)
    revision: int = Field(ge=1, strict=True)
    content_id: str = Field(pattern="^[0-9a-f]{64}$")
    rules_version: str = Field(min_length=1)
    coverage_status: CoverageStatus
    original_total_hours: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    items: tuple[OriginalWorkTimeRecord, ...] = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_original_sum(self) -> "LabourPlanRevisionRecord":
        ids = [item.work_item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("work item IDs must be unique within a revision")
        if self.coverage_status == CoverageStatus.READY:
            if any(item.original_hours is None for item in self.items):
                raise ValueError("READY requires each original work time")
            hours = [item.original_hours for item in self.items]
            # Sum without implicit rounding even if the caller lowered precision.
            with localcontext() as context:
                context.prec = max(
                    context.prec,
                    max(value.adjusted() for value in hours)
                    - min(value.as_tuple().exponent for value in hours)
                    + len(str(len(hours))) + 1,
                )
                expected = sum(hours, Decimal("0"))
            if self.original_total_hours != expected:
                raise ValueError("original total must equal the unchanged work-time sum")
        elif self.original_total_hours is not None:
            raise ValueError("unfinished coverage must not claim a complete total")
        return self


class LabourReviewDecisionRecord(StorageRecord):
    """Stored outcome, constructed only after a domain approval call.

    final_billable_hours is an explicit storage snapshot also for unchanged
    approval. Domain APPROVED_UNCHANGED still uses final_hours=None; its stored
    effective time is the unchanged original total. This model grants no role.
    """

    decision_id: str = Field(min_length=1)
    inquiry_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    content_id: str = Field(pattern="^[0-9a-f]{64}$")
    rules_version: str = Field(min_length=1)
    status: ReviewDecisionStatus
    original_total_hours: Decimal = Field(gt=0, allow_inf_nan=False)
    final_billable_hours: Decimal = Field(ge=0, allow_inf_nan=False)
    reason: str | None = None
    reviewer_id: str = Field(min_length=1)
    reviewed_at: datetime
    event_id: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def validate_outcome(self) -> "LabourReviewDecisionRecord":
        if self.status == ReviewDecisionStatus.APPROVED_UNCHANGED:
            if self.final_billable_hours != self.original_total_hours:
                raise ValueError("unchanged decision must retain the original total")
        elif self.reason is None or not self.reason.strip():
            raise ValueError("adjusted decision requires a reason")
        return self


class ActiveLabourPlanRecord(StorageRecord):
    inquiry_id: str = Field(min_length=1)
    active_revision_id: str = Field(min_length=1)
    active_decision_id: str | None = None


class LabourPlanRequestRecord(StorageRecord):
    """Idempotent service result, separate from inquiry creation retries."""

    inquiry_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    operation: str = Field(pattern="^(SAVE_PLAN|APPROVE_LABOUR)$")
    idempotency_key: str = Field(min_length=1)
    request_json: str
    response_json: str
    revision_id: str = Field(min_length=1)
    decision_id: str | None = None
    result_work_version: int = Field(ge=1, strict=True)
    created_at: datetime

    @field_validator("request_json", "response_json")
    @classmethod
    def validate_json_object(cls, value: str) -> str:
        if not isinstance(json.loads(value), dict):
            raise ValueError("request and response snapshots must be JSON objects")
        return value

    @model_validator(mode="after")
    def validate_decision_reference(self) -> "LabourPlanRequestRecord":
        if (self.operation == "APPROVE_LABOUR") != (self.decision_id is not None):
            raise ValueError("only approval results must reference a decision")
        return self
