"""WD-2 application snapshots; no persistence, HTTP or approval actions.

Caller allocates IDs once and retains them across revisions. Display order
never supplies identity. Schema v2 is independent of private draft payload v1.
"""

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import (
    AliasChoices, BaseModel, ConfigDict, Field, ValidationError,
    field_validator, model_validator,
)

from domain.models.enums import Axle, Side
from domain.models.labour_plan import LabourReviewDecision
from domain.models.labour_time import LabourTimeSource
from domain.models.part_requirement import RequirementUnit
from domain.models.work_item import Operation


Identifier = Annotated[str, Field(min_length=1, strict=True)]
ContentId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ContractRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_text(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("use None for missing text, not blank text")
        return value


class SourceRevision(ContractRecord):
    """Origin version is independent of the WorkPlan revision."""

    kind: Literal["V1_DRAFT", "INQUIRY"]
    source_id: Identifier
    revision: int = Field(ge=1, strict=True)


class VehicleData(ContractRecord):
    registration: str | None = None
    make: str | None = None
    model: str | None = None
    year: int | None = Field(default=None, strict=True)


class PartNeed(ContractRecord):
    """Work-local requirement, separate from future part selections."""

    requirement_id: Identifier
    canonical_product_type: str | None = None
    axle: Axle | None = None
    required_quantity: int | None = Field(default=None, gt=0, strict=True)
    required_unit: RequirementUnit | None = None


class WorkData(ContractRecord):
    work_id: Identifier
    display_order: int = Field(ge=0, strict=True)
    description: str | None = None
    operation: Operation | None = None
    axle: Axle | None = None
    side: Side | None = None
    part_needs: tuple[PartNeed, ...] = ()

    @model_validator(mode="after")
    def unique_requirements(self):
        ids = [need.requirement_id for need in self.part_needs]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement IDs must be unique within a work")
        return self


class TimeData(ContractRecord):
    """FULL covered scope, never a per-piece time to multiply by quantity.

    Alternatives and explicit selection are separate. Incomplete source data
    is allowed here; adapters report it without inventing a domain time.
    """

    time_id: Identifier
    covered_work_ids: tuple[Identifier, ...] = Field(min_length=1)
    side: Side = Side.UNKNOWN
    hours: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    source: LabourTimeSource | None = None
    work_description: str | None = None
    autodata_reference: str | None = None
    recorded_by: str | None = None

    @field_validator("hours", mode="before")
    @classmethod
    def exact_hours(cls, value):
        if isinstance(value, (bool, float)):
            raise ValueError("hours require Decimal or decimal text")
        return value

    @model_validator(mode="after")
    def unique_coverage(self):
        if len(self.covered_work_ids) != len(set(self.covered_work_ids)):
            raise ValueError("a time must not cover the same work twice")
        return self


class PlanRevisionRef(ContractRecord):
    organization_id: Identifier
    workshop_id: Identifier
    inquiry_id: Identifier
    plan_id: Identifier
    revision_id: Identifier
    revision: int = Field(ge=1, strict=True)
    content_id: ContentId


class WorkReviewRecord(ContractRecord):
    """Employee's work check, never a customer or labour-total approval.

    Absence is pending. Historical records remain readable. Trusted caller
    supplies the employee identity; this record does not authenticate it.
    """

    review_id: Identifier
    plan_revision: PlanRevisionRef
    work_id: Identifier
    status: Literal["CHECKED"] = "CHECKED"
    reviewer_id: Identifier
    reviewed_at: datetime
    note: str | None = None

    @field_validator("reviewed_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.utcoffset() is None:
            raise ValueError("review timestamp must include a timezone")
        return value


class LabourApprovalRecord(ContractRecord):
    """Historical link to a domain decision, never current approval authority.

    WD-5 must authorize, enforce work checks and persist atomically. WD-2
    adapters do not attach this record to a domain LabourPlan.
    """

    decision_id: Identifier
    labour_revision_id: Identifier
    plan_revision: PlanRevisionRef
    decision: LabourReviewDecision


class WorkPlanSnapshot(ContractRecord):
    schema_version: Literal[2] = 2
    organization_id: Identifier
    workshop_id: Identifier
    inquiry_id: Identifier
    plan_id: Identifier
    revision_id: Identifier
    revision: int = Field(ge=1, strict=True)
    previous_revision_id: Identifier | None = None
    source_revision: SourceRevision | None = None
    customer_description: str | None = None
    vehicle: VehicleData = Field(default_factory=VehicleData)
    works: tuple[WorkData, ...] = ()
    original_times: tuple[TimeData, ...] = ()
    selected_time_ids: tuple[Identifier, ...] = ()
    work_reviews: tuple[WorkReviewRecord, ...] = ()
    historical_labour_approval: LabourApprovalRecord | None = Field(
        default=None,
        validation_alias=AliasChoices("historical_labour_approval", "labour_approval"),
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value):
        if type(value) is not int or value != 2:
            raise ValueError("schema_version must be integer 2")
        return value

    @model_validator(mode="after")
    def valid_references(self):
        for values, label in (
            ([work.work_id for work in self.works], "work IDs"),
            ([work.display_order for work in self.works], "display orders"),
            ([entry.time_id for entry in self.original_times], "time IDs"),
            (self.selected_time_ids, "selected time IDs"),
            ([review.review_id for review in self.work_reviews], "review IDs"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        work_ids = {work.work_id for work in self.works}
        if any(set(entry.covered_work_ids) - work_ids for entry in self.original_times):
            raise ValueError("time coverage references an unknown work")
        if set(self.selected_time_ids) - {entry.time_id for entry in self.original_times}:
            raise ValueError("selection references an unknown time")
        if self.previous_revision_id == self.revision_id:
            raise ValueError("previous revision must be a different revision")
        if self.revision == 1 and self.previous_revision_id is not None:
            raise ValueError("first revision cannot have a predecessor")
        if self.revision > 1 and self.previous_revision_id is None:
            raise ValueError("later revisions require a predecessor")
        return self

    @property
    def content_id(self) -> str:
        # Editor/source/revision context included; review outcomes excluded.
        payload = self.model_dump(
            mode="json", exclude={"work_reviews", "historical_labour_approval"},
        )
        payload["works"].sort(key=lambda work: work["work_id"])
        payload["original_times"].sort(key=lambda entry: entry["time_id"])
        for entry in payload["original_times"]:
            entry["covered_work_ids"].sort()
            if entry["hours"] is not None:
                value = format(Decimal(entry["hours"]), "f")
                entry["hours"] = value.rstrip("0").rstrip(".") if "." in value else value
        payload["selected_time_ids"].sort()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def revision_ref(self) -> PlanRevisionRef:
        return PlanRevisionRef(**{name: getattr(self, name) for name in (
            "organization_id", "workshop_id", "inquiry_id", "plan_id", "revision_id", "revision",
        )}, content_id=self.content_id)

    @property
    def unchecked_work_ids(self) -> tuple[str, ...]:
        # Copies can bypass validation even for frozen models. Validate the
        # current work/revision context separately from historical outcomes.
        current = WorkPlanSnapshot.model_validate(self.model_dump(
            round_trip=True, exclude={"work_reviews", "historical_labour_approval"},
        ))
        current_ref = current.revision_ref
        work_ids = {work.work_id for work in current.works}
        checked = set()
        for record in self.work_reviews:
            if not isinstance(record, WorkReviewRecord):
                continue
            try:
                # Dump first so copied nested PlanRevisionRefs are validated too.
                review = WorkReviewRecord.model_validate(record.model_dump(
                    round_trip=True, warnings=False,
                ))
            except ValidationError:
                continue
            if (review.status == "CHECKED" and review.work_id in work_ids
                    and review.plan_revision == current_ref):
                checked.add(review.work_id)
        return tuple(work.work_id for work in sorted(current.works, key=lambda work: work.display_order)
                     if work.work_id not in checked)

    @property
    def all_works_checked(self) -> bool:
        # Never a plan, labour-total or customer-offer approval.
        return bool(self.works) and not self.unchecked_work_ids
