"""Autodata labour-time lookup contracts; integration records, not domain models.

Choice identity: a vehicle, operation or time variant is identified by the
identifier the source itself shows (source_id), when there is one, and always
by its selection path: the ordered visible labels from the start of the
selection to the choice. CARBIT never invents a source_id. Within one result,
candidate identities must be unique; otherwise no choice can be made safely.

FOUND requires a resolved vehicle, operation and time variant plus one parsed
time reading. Every other outcome has hours=None, never zero.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, ValidationInfo, computed_field, field_validator,
    model_validator,
)


class AutodataRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Displayed source text kept exactly as evidence, whitespace included.
    RAW_TEXT_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_text(cls, value, info: ValidationInfo):
        if info.field_name in cls.RAW_TEXT_FIELDS:
            return value
        if isinstance(value, str) and not value.strip():
            raise ValueError("use None for missing text, not blank text")
        return value


class ChoiceIdentity(AutodataRecord):
    source_id: str | None = None
    selection_path: tuple[str, ...] = Field(min_length=1)

    @field_validator("selection_path")
    @classmethod
    def validate_path(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("selection path labels must not be blank")
        return values


class Choice(AutodataRecord):
    identity: ChoiceIdentity
    # Extra visible distinguishing text, e.g. engine code, kW or years.
    details: tuple[str, ...] = ()

    @field_validator("details")
    @classmethod
    def validate_details(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("choice details must not be blank")
        return values

    @property
    def label(self) -> str:
        return self.identity.selection_path[-1]


class ChoiceLevel(str, Enum):
    VEHICLE = "VEHICLE"
    OPERATION = "OPERATION"
    VARIANT = "VARIANT"


class ResolvedBy(str, Enum):
    EXPLICIT_SELECTION = "EXPLICIT_SELECTION"
    ONLY_CANDIDATE = "ONLY_CANDIDATE"


class ResolvedChoice(AutodataRecord):
    choice: Choice
    resolved_by: ResolvedBy


class VehicleQuery(AutodataRecord):
    """Known vehicle facts; which ones a real source needs is not yet mapped."""

    registration: str | None = Field(default=None, max_length=20)
    vin: str | None = Field(default=None, max_length=17)
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    model_year: int | None = Field(default=None, ge=1900, le=2100, strict=True)
    engine_code: str | None = Field(default=None, max_length=40)
    fuel: str | None = Field(default=None, max_length=40)
    power_kw: int | None = Field(default=None, gt=0, le=2000, strict=True)
    displacement_cc: int | None = Field(default=None, gt=0, le=20000, strict=True)

    @model_validator(mode="after")
    def require_some_fact(self) -> "VehicleQuery":
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("a vehicle query requires at least one known fact")
        return self


class WorkQuery(AutodataRecord):
    """Staff search text for an operation; never derived from customer text."""

    search_text: str = Field(min_length=1, max_length=200)


class LabourTimeRequest(AutodataRecord):
    """One stateless lookup; earlier staff selections are replayed each time."""

    vehicle_query: VehicleQuery
    work_query: WorkQuery
    vehicle_selection: ChoiceIdentity | None = None
    operation_selection: ChoiceIdentity | None = None
    variant_selection: ChoiceIdentity | None = None

    @model_validator(mode="after")
    def require_selection_order(self) -> "LabourTimeRequest":
        if self.operation_selection is not None and self.vehicle_selection is None:
            raise ValueError("an operation selection requires a vehicle selection")
        if self.variant_selection is not None and self.operation_selection is None:
            raise ValueError("a variant selection requires an operation selection")
        return self


class LabourTimeReading(AutodataRecord):
    """Exact displayed time text, its visible unit evidence and exact hours."""

    raw_time_text: str = Field(max_length=64)
    number_text: str
    unit_evidence: str
    unit: Literal["HOURS"] = "HOURS"
    hours: Decimal = Field(gt=0, allow_inf_nan=False)

    @field_validator("hours", mode="before")
    @classmethod
    def require_exact_decimal(cls, value):
        if isinstance(value, (bool, float)):
            raise ValueError("use Decimal or decimal text, not bool or float")
        return value


class LookupOutcome(str, Enum):
    FOUND = "FOUND"
    VEHICLE_AMBIGUOUS = "VEHICLE_AMBIGUOUS"
    VEHICLE_NOT_FOUND = "VEHICLE_NOT_FOUND"
    WORK_AMBIGUOUS = "WORK_AMBIGUOUS"
    WORK_NOT_FOUND = "WORK_NOT_FOUND"
    VARIANT_AMBIGUOUS = "VARIANT_AMBIGUOUS"
    TIME_MISSING = "TIME_MISSING"
    UNPARSEABLE = "UNPARSEABLE"
    SELECTION_INVALID = "SELECTION_INVALID"
    SELECTION_NOT_UNIQUE = "SELECTION_NOT_UNIQUE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    BLOCKED = "BLOCKED"
    SITE_CHANGED = "SITE_CHANGED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


AMBIGUOUS_LEVEL = {
    LookupOutcome.VEHICLE_AMBIGUOUS: ChoiceLevel.VEHICLE,
    LookupOutcome.WORK_AMBIGUOUS: ChoiceLevel.OPERATION,
    LookupOutcome.VARIANT_AMBIGUOUS: ChoiceLevel.VARIANT,
}
NOT_FOUND_LEVEL = {
    LookupOutcome.VEHICLE_NOT_FOUND: ChoiceLevel.VEHICLE,
    LookupOutcome.WORK_NOT_FOUND: ChoiceLevel.OPERATION,
}
# Outcomes that need the level whose candidates or selection they concern.
LEVEL_OUTCOMES = frozenset({
    *AMBIGUOUS_LEVEL, *NOT_FOUND_LEVEL,
    LookupOutcome.SELECTION_INVALID, LookupOutcome.SELECTION_NOT_UNIQUE,
})
ORDERED_LEVELS = (ChoiceLevel.VEHICLE, ChoiceLevel.OPERATION, ChoiceLevel.VARIANT)
REJECTED_TEXT_CAP = 200


class LookupProvenance(AutodataRecord):
    adapter_name: str = Field(max_length=100)
    adapter_version: str = Field(max_length=40)
    retrieved_at: datetime
    # True for fake and synthetic adapters; such results are not Autodata data.
    synthetic: bool

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone")
        return value.astimezone(timezone.utc)


class LabourTimeLookupResult(AutodataRecord):
    # A capped rejected text may end up all whitespace; it is still evidence.
    RAW_TEXT_FIELDS: ClassVar[frozenset[str]] = frozenset({"rejected_time_text"})

    outcome: LookupOutcome
    provenance: LookupProvenance
    vehicle: ResolvedChoice | None = None
    operation: ResolvedChoice | None = None
    variant: ResolvedChoice | None = None
    level: ChoiceLevel | None = None
    candidates: tuple[Choice, ...] = ()
    time: LabourTimeReading | None = None
    # Rejected displayed time text, kept as evidence up to the cap; a longer
    # text is cut at the cap and flagged rather than silently shortened.
    rejected_time_text: str | None = Field(default=None, max_length=REJECTED_TEXT_CAP)
    rejected_time_text_truncated: bool = False
    rejection_reason: str | None = None

    @computed_field
    @property
    def hours(self) -> Decimal | None:
        return self.time.hours if self.outcome == LookupOutcome.FOUND else None

    @model_validator(mode="after")
    def validate_outcome(self) -> "LabourTimeLookupResult":
        outcome = self.outcome
        resolved = (self.vehicle, self.operation, self.variant)
        if outcome == LookupOutcome.FOUND:
            if any(choice is None for choice in resolved) or self.time is None:
                raise ValueError("FOUND requires a vehicle, operation, variant and time")
        elif self.time is not None:
            raise ValueError("only FOUND may carry a time reading")
        if any(later is not None and earlier is None
               for earlier, later in zip(resolved, resolved[1:])):
            raise ValueError("choices resolve in vehicle, operation, variant order")
        if outcome in LEVEL_OUTCOMES:
            if self.level is None:
                raise ValueError(f"{outcome.value} requires its choice level")
            expected = AMBIGUOUS_LEVEL.get(outcome) or NOT_FOUND_LEVEL.get(outcome)
            if expected is not None and self.level != expected:
                raise ValueError(f"{outcome.value} concerns the {expected.value} level")
            depth = ORDERED_LEVELS.index(self.level)
            if any(choice is None for choice in resolved[:depth]) or resolved[depth] is not None:
                raise ValueError("the level must be the first unresolved choice")
        elif self.level is not None:
            raise ValueError(f"{outcome.value} does not concern a choice level")
        if outcome in AMBIGUOUS_LEVEL:
            if len(self.candidates) < 2:
                raise ValueError("an ambiguous result requires at least two candidates")
            keys = [candidate.identity for candidate in self.candidates]
            if len(keys) != len(set(keys)):
                raise ValueError("ambiguous candidates must have unique identities")
        elif outcome not in (LookupOutcome.SELECTION_INVALID, LookupOutcome.SELECTION_NOT_UNIQUE):
            if self.candidates:
                raise ValueError(f"{outcome.value} must not return candidates")
        if outcome == LookupOutcome.TIME_MISSING and self.operation is None:
            # The variant is absent when the operation lists no time rows.
            raise ValueError("TIME_MISSING requires the resolved operation")
        if outcome == LookupOutcome.UNPARSEABLE:
            if self.variant is None or self.rejected_time_text is None or self.rejection_reason is None:
                raise ValueError("UNPARSEABLE requires the variant, its text and a reason")
        elif (self.rejected_time_text is not None or self.rejection_reason is not None
              or self.rejected_time_text_truncated):
            raise ValueError("only UNPARSEABLE keeps rejected time text and its reason")
        return self
