from datetime import datetime, timezone
from decimal import Decimal

from domain.models.labour_plan import (
    LabourPlan, LabourReviewDecision, LabourReviewStatus, LabourTimeEntry,
)
from domain.models.work_plan import WorkPlan


def build_labour_plan(
    work_plan: WorkPlan,
    original_times: list[LabourTimeEntry],
    selected_time_ids: list[str] | None = None,
) -> LabourPlan:
    """Select unique full-work times, then assess coverage without pricing.

    Explicit references choose combined times or resolve alternative original
    times. Uncovered works get a single matching full-scope time automatically.
    Multiple matching times require review; none is guessed. Combined times
    across distinct operations are never chosen implicitly. No workshop pair
    practice changes work scope:
    a pair can be one explicit BOTH work, or opposite-side works matching a
    confirmed workshop pair practice. Their separate times are not summed.
    Unconfirmed split-side groups require an explicit full-group time choice.
    """
    plan = LabourPlan(
        work_plan=work_plan.model_copy(deep=True),
        original_times=[entry.model_copy(deep=True) for entry in original_times],
        selected_time_ids=list(selected_time_ids or []),
    )
    entries = {entry.time_id: entry for entry in plan.original_times}
    explicitly_covered = {
        work_id
        for time_id in plan.selected_time_ids
        if time_id in entries
        for work_id in entries[time_id].covered_work_ids
    }
    pair_work_ids = set()
    confirmed_pairs = plan.work_plan.confirmed_split_pairs
    for group in plan.work_plan.split_side_groups:
        pair_work_ids.update(group)
        if explicitly_covered.intersection(group):
            continue
        if group not in confirmed_pairs:
            continue
        candidates = [
            entry for entry in plan.original_times
            if set(entry.covered_work_ids) == set(group)
        ]
        if len(candidates) == 1:
            plan.selected_time_ids.append(candidates[0].time_id)
    for work in plan.work_plan.work_items:
        if work.work_id in explicitly_covered or work.work_id in pair_work_ids:
            continue
        candidates = [
            entry for entry in plan.original_times
            if entry.covered_work_ids == [work.work_id]
            and entry.covers_full_work(work)
        ]
        if len(candidates) == 1:
            plan.selected_time_ids.append(candidates[0].time_id)
    return plan


def approve_labour_plan_unchanged(
    plan: LabourPlan,
    expected_content_id: str,
    reviewer_id: str,
) -> LabourPlan:
    """Explicitly accept the unchanged original total on an exact snapshot.

    The future application service must supply an authenticated human reviewer,
    enforce permissions, and atomically persist this decision with its event.
    Source inputs and a previous decision are not mutated by this function.
    """
    return _approve_labour_plan(
        plan, expected_content_id, reviewer_id,
        LabourReviewStatus.APPROVED_UNCHANGED,
    )


def approve_labour_plan_adjusted(
    plan: LabourPlan,
    expected_content_id: str,
    reviewer_id: str,
    final_hours: Decimal,
    reason: str,
) -> LabourPlan:
    """Record an explicit human final time and reason; suggest no adjustment.

    Zero is a valid nonnegative decision here, separate from the existing
    positive-hour LabourTime/pricing models. Pricing a zero-time decision
    needs an explicit future service policy; this function does not price it.
    Authentication, permissions and atomic storage belong to the caller.
    """
    return _approve_labour_plan(
        plan, expected_content_id, reviewer_id,
        LabourReviewStatus.APPROVED_ADJUSTED, final_hours, reason,
    )


def _approve_labour_plan(
    plan: LabourPlan,
    expected_content_id: str,
    reviewer_id: str,
    status: LabourReviewStatus,
    final_hours: Decimal | None = None,
    reason: str | None = None,
) -> LabourPlan:
    reviewed = LabourPlan.model_validate(plan.model_dump(round_trip=True))
    if reviewed.content_id != expected_content_id:
        raise ValueError("LabourPlan content changed since review")
    original_total = reviewed.require_total_hours()
    decision = LabourReviewDecision(
        plan_content_id=reviewed.content_id,
        rules_version=reviewed.review_rules_version,
        status=status,
        reviewer_id=reviewer_id,
        reviewed_at=datetime.now(timezone.utc),
        original_total_hours=original_total,
        final_hours=final_hours,
        reason=reason,
    )
    if reviewed.review_decision is not None:
        reviewed.review_history += (reviewed.review_decision,)
    reviewed.review_decision = decision
    return reviewed
