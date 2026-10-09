from collections import Counter
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from domain.models.enums import Side
from domain.models.labour_time import LabourTime
from domain.models.work_plan import PlannedWork, WorkPlan


LABOUR_REVIEW_RULES_VERSION = "3A-v2"


class LabourTimeEntry(BaseModel):
    """An original time and its explicit coverage, not an adjusted time.

    A single-work entry names its measured side. A multi-work entry is an
    explicit Autodata pair/combined time for the FULL scope of listed works;
    its side is UNKNOWN because the works supply their individual scopes.
    """

    model_config = ConfigDict(extra="forbid")

    time_id: str = Field(min_length=1)
    labour_time: LabourTime
    covered_work_ids: list[str] = Field(min_length=1)
    side: Side = Side.UNKNOWN

    @field_validator("time_id")
    @classmethod
    def validate_time_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("time_id must not be blank")
        return value

    @field_validator("covered_work_ids")
    @classmethod
    def validate_coverage_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("covered work IDs must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("an entry must not cover the same work twice")
        return values

    @model_validator(mode="after")
    def validate_time_scope(self) -> "LabourTimeEntry":
        if not self.labour_time.hours.is_finite():
            raise ValueError("original hours must be finite")
        if len(self.covered_work_ids) > 1 and self.side != Side.UNKNOWN:
            raise ValueError("combined entries use the scopes of their works")
        return self

    def covers_full_work(self, work: PlannedWork) -> bool:
        if work.work_id not in self.covered_work_ids:
            return False
        if len(self.covered_work_ids) > 1:
            return True
        return self.side == work.work_item.side


class LabourPlanStatus(str, Enum):
    READY = "READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class LabourPlanIssueCode(str, Enum):
    UNKNOWN_WORK = "UNKNOWN_WORK"
    UNKNOWN_TIME = "UNKNOWN_TIME"
    DUPLICATE_SELECTION = "DUPLICATE_SELECTION"
    INCOMPLETE_SCOPE = "INCOMPLETE_SCOPE"
    OVERLAPPING_COVERAGE = "OVERLAPPING_COVERAGE"
    MISSING_TIME = "MISSING_TIME"
    MISSING_PAIR_TIME = "MISSING_PAIR_TIME"
    AMBIGUOUS_TIME = "AMBIGUOUS_TIME"


class LabourPlanIssue(BaseModel):
    code: LabourPlanIssueCode
    work_ids: list[str] = Field(default_factory=list)
    time_ids: list[str] = Field(default_factory=list)


class LabourReviewFlagCode(str, Enum):
    MULTI_WORK_REVIEW_REQUIRED = "MULTI_WORK_REVIEW_REQUIRED"
    POSSIBLE_LABOUR_OVERLAP = "POSSIBLE_LABOUR_OVERLAP"


class LabourReviewStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED_UNCHANGED = "APPROVED_UNCHANGED"
    APPROVED_ADJUSTED = "APPROVED_ADJUSTED"


class LabourReviewFlag(BaseModel):
    code: LabourReviewFlagCode
    work_ids: list[str]


class LabourReviewDecision(BaseModel):
    """Explicit decision supplied by a trusted application service.

    Authentication, permissions and persistent audit history belong to that
    service. This domain record does not authenticate the reviewer itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_content_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: LabourReviewStatus
    reviewer_id: str = Field(min_length=1)
    reviewed_at: datetime

    @model_validator(mode="after")
    def validate_unchanged_decision(self) -> "LabourReviewDecision":
        if self.status != LabourReviewStatus.APPROVED_UNCHANGED:
            raise ValueError("Only APPROVED_UNCHANGED is available in phase 3A")
        if not self.reviewer_id.strip():
            raise ValueError("reviewer_id must not be blank")
        if self.reviewed_at.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone")
        return self


class LabourPlan(BaseModel):
    """Original records and selected references, with derived billing readiness.

    Unselected original alternatives may overlap. Selected coverage must be
    complete and disjoint. No price, VAT or human adjustment is calculated.
    """

    model_config = ConfigDict(extra="forbid")

    work_plan: WorkPlan
    original_times: list[LabourTimeEntry] = Field(default_factory=list)
    selected_time_ids: list[str] = Field(default_factory=list)
    review_decision: LabourReviewDecision | None = None

    @computed_field
    @property
    def review_flags(self) -> list[LabourReviewFlag]:
        flags = []
        work_ids = sorted(work.work_id for work in self.work_plan.work_items)
        if len(work_ids) > 1:
            flags.append(LabourReviewFlag(
                code=LabourReviewFlagCode.MULTI_WORK_REVIEW_REQUIRED,
                work_ids=work_ids,
            ))
        # POSSIBLE_LABOUR_OVERLAP is reserved. No confirmed source or rule
        # currently exists; operation, axle and side must not imply overlap.
        return flags

    @computed_field
    @property
    def content_id(self) -> str:
        """Hash the full source snapshot and selection, excluding the decision.

        List ordering and equivalent Decimal representations are immaterial.
        All original alternatives are included, so changing even an unselected
        Autodata record invalidates the decision. Never cache this value.
        """
        works = self.work_plan.model_dump(mode="json", round_trip=True)
        works["work_items"].sort(key=lambda work: work["work_id"])
        times = []
        for entry in sorted(self.original_times, key=lambda item: item.time_id):
            value = entry.model_dump(mode="json", round_trip=True)
            value["covered_work_ids"].sort()
            hours = entry.labour_time.hours
            # Avoid Decimal.normalize(), which can round under the context.
            decimal_text = format(hours, "f")
            if "." in decimal_text:
                decimal_text = decimal_text.rstrip("0").rstrip(".")
            value["labour_time"]["hours"] = decimal_text
            times.append(value)
        payload = {"review_rules_version": LABOUR_REVIEW_RULES_VERSION, "work_plan": works,
                   "original_times": times,
                   "selected_time_ids": sorted(self.selected_time_ids)}
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    @computed_field
    @property
    def review_status(self) -> LabourReviewStatus:
        decision = self.review_decision
        if (self.is_billable and decision is not None
                and decision.status == LabourReviewStatus.APPROVED_UNCHANGED
                and decision.plan_content_id == self.content_id):
            return LabourReviewStatus.APPROVED_UNCHANGED
        return LabourReviewStatus.PENDING

    @computed_field
    @property
    def is_ready_for_final_approval(self) -> bool:
        """Coverage READY alone is insufficient for final approval or sending."""
        return self.is_billable and self.review_status == LabourReviewStatus.APPROVED_UNCHANGED

    def require_final_approval_ready(self) -> None:
        if not self.is_ready_for_final_approval:
            raise ValueError("LabourPlan requires explicit current human review before final approval")

    @field_validator("selected_time_ids")
    @classmethod
    def validate_selected_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("selected time IDs must not be blank")
        return values

    @model_validator(mode="after")
    def validate_original_ids(self) -> "LabourPlan":
        ids = [entry.time_id for entry in self.original_times]
        if len(ids) != len(set(ids)):
            raise ValueError("original time IDs must be unique")
        return self

    @computed_field
    @property
    def issues(self) -> list[LabourPlanIssue]:
        issues = []
        works = {work.work_id: work for work in self.work_plan.work_items}
        entries = {entry.time_id: entry for entry in self.original_times}
        coverage: dict[str, list[str]] = {work_id: [] for work_id in works}

        for entry in self.original_times:
            unknown = [key for key in entry.covered_work_ids if key not in works]
            if unknown:
                issues.append(LabourPlanIssue(
                    code=LabourPlanIssueCode.UNKNOWN_WORK,
                    work_ids=unknown,
                    time_ids=[entry.time_id],
                ))

        for time_id, count in Counter(self.selected_time_ids).items():
            if count > 1:
                issues.append(LabourPlanIssue(
                    code=LabourPlanIssueCode.DUPLICATE_SELECTION,
                    time_ids=[time_id],
                ))
            entry = entries.get(time_id)
            if entry is None:
                issues.append(LabourPlanIssue(
                    code=LabourPlanIssueCode.UNKNOWN_TIME, time_ids=[time_id],
                ))
                continue
            for work_id in entry.covered_work_ids:
                work = works.get(work_id)
                if work is None:
                    continue
                if not entry.covers_full_work(work):
                    issues.append(LabourPlanIssue(
                        code=LabourPlanIssueCode.INCOMPLETE_SCOPE,
                        work_ids=[work_id], time_ids=[time_id],
                    ))
                    continue
                coverage[work_id].append(time_id)

        for work_id, time_ids in coverage.items():
            if len(time_ids) > 1:
                issues.append(LabourPlanIssue(
                    code=LabourPlanIssueCode.OVERLAPPING_COVERAGE,
                    work_ids=[work_id], time_ids=time_ids,
                ))
            elif not time_ids:
                work = works[work_id]
                candidates = [
                    entry.time_id for entry in self.original_times
                    if entry.covered_work_ids == [work_id]
                    and entry.covers_full_work(work)
                ]
                if len(candidates) > 1:
                    code = LabourPlanIssueCode.AMBIGUOUS_TIME
                elif work.work_item.side == Side.BOTH and not candidates:
                    code = LabourPlanIssueCode.MISSING_PAIR_TIME
                else:
                    code = LabourPlanIssueCode.MISSING_TIME
                issues.append(LabourPlanIssue(
                    code=code, work_ids=[work_id], time_ids=candidates,
                ))

        for group in self.work_plan.split_side_groups:
            if any(
                time_id in entries
                and set(group).issubset(entries[time_id].covered_work_ids)
                for time_id in self.selected_time_ids
            ):
                continue
            candidates = [
                entry.time_id for entry in self.original_times
                if set(entry.covered_work_ids) == set(group)
            ]
            issues.append(LabourPlanIssue(
                code=(LabourPlanIssueCode.AMBIGUOUS_TIME if len(candidates) > 1
                      else LabourPlanIssueCode.MISSING_PAIR_TIME),
                work_ids=group, time_ids=candidates,
            ))
        return issues

    @computed_field
    @property
    def status(self) -> LabourPlanStatus:
        if self.issues:
            return LabourPlanStatus.REVIEW_REQUIRED
        return LabourPlanStatus.READY

    @computed_field
    @property
    def is_billable(self) -> bool:
        return self.status == LabourPlanStatus.READY

    @computed_field
    @property
    def total_hours(self) -> Decimal | None:
        if not self.is_billable:
            return None
        entries = {entry.time_id: entry for entry in self.original_times}
        return sum(
            (entries[key].labour_time.hours for key in self.selected_time_ids),
            Decimal("0"),
        )

    def require_total_hours(self) -> Decimal:
        """Refuse a partial or contradictory time total at the billing boundary."""
        total = self.total_hours
        if total is None:
            raise ValueError("LabourPlan requires review before billing")
        return total
