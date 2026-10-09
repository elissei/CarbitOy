from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.labour_plan import (
    LabourPlan, LabourReviewDecision, LabourReviewFlagCode, LabourReviewStatus,
    LabourTimeEntry,
)
from domain.models.labour_time import LabourTime
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from services.labour_planning import (
    approve_labour_plan_unchanged, build_labour_plan,
)
from services.proposal_builder import build_raw_proposal


def make_plan(multi=False, side=Side.LEFT, axle=Axle.FRONT):
    works = [PlannedWork(work_id="brakes", work_item=WorkItem(
        operation=Operation.BRAKE_REPLACEMENT, axle=Axle.FRONT, side=side,
    ))]
    if multi:
        works.append(PlannedWork(work_id="shocks", work_item=WorkItem(
            operation=Operation.SHOCK_ABSORBER_REPLACEMENT, axle=axle, side=side,
        )))
    times = [LabourTimeEntry(
        time_id=f"time-{work.work_id}", covered_work_ids=[work.work_id],
        side=work.work_item.side,
        labour_time=LabourTime(
            hours=Decimal("1.60"), source="MANUAL_AUTODATA",
            work_description="Fictional test operation",
            recorded_by="Test recorder", autodata_reference="TEST-REFERENCE",
        ),
    ) for work in works]
    return build_labour_plan(WorkPlan(
        vehicle=Vehicle(registration="TEST-123", make="Test", model="Test"),
        work_items=works,
    ), times)


def codes(plan):
    return {flag.code for flag in plan.review_flags}


def approve(plan):
    return approve_labour_plan_unchanged(plan, plan.content_id, "test-user-id")


@pytest.mark.parametrize("side", [Side.LEFT, Side.BOTH])
def test_one_work_including_pair_does_not_require_multi_work_flag(side):
    plan = make_plan(side=side)
    assert plan.review_flags == []
    assert plan.require_total_hours() == Decimal("1.60")


def test_two_works_require_explicit_review_without_changing_original_times():
    plan = make_plan(multi=True)
    before = [time.model_dump() for time in plan.original_times]
    assert LabourReviewFlagCode.MULTI_WORK_REVIEW_REQUIRED in codes(plan)
    assert plan.review_status == LabourReviewStatus.PENDING
    assert not plan.is_ready_for_final_approval
    with pytest.raises(ValueError, match="human review"):
        plan.require_final_approval_ready()
    assert plan.require_total_hours() == Decimal("3.20")
    assert [time.model_dump() for time in plan.original_times] == before


@pytest.mark.parametrize("axle", list(Axle))
@pytest.mark.parametrize("side", list(Side))
def test_operation_axle_and_side_never_imply_labour_overlap(axle, side):
    plan = make_plan(multi=True, axle=axle, side=side)
    before = [entry.model_dump() for entry in plan.original_times]
    assert codes(plan) == {LabourReviewFlagCode.MULTI_WORK_REVIEW_REQUIRED}
    assert LabourReviewFlagCode.POSSIBLE_LABOUR_OVERLAP not in codes(plan)
    assert plan.require_total_hours() == Decimal("3.20")
    approved = approve(plan)
    assert approved.require_total_hours() == Decimal("3.20")
    assert [entry.model_dump() for entry in approved.original_times] == before


def test_repeated_operation_also_only_requires_multi_work_review():
    plan = make_plan(multi=True)
    plan.work_plan.work_items[1].work_item.operation = Operation.BRAKE_REPLACEMENT
    assert codes(plan) == {LabourReviewFlagCode.MULTI_WORK_REVIEW_REQUIRED}
    assert plan.require_total_hours() == Decimal("3.20")


def test_decision_under_previous_business_rules_requires_new_review(monkeypatch):
    monkeypatch.setattr("domain.models.labour_plan.LABOUR_REVIEW_RULES_VERSION", "3A-v1")
    plan = approve(make_plan(multi=True))
    old_decision = plan.review_decision
    monkeypatch.setattr("domain.models.labour_plan.LABOUR_REVIEW_RULES_VERSION", "3A-v2")
    assert plan.review_status == LabourReviewStatus.PENDING
    assert not plan.is_ready_for_final_approval
    assert plan.review_decision == old_decision
    assert approve(plan).is_ready_for_final_approval


@pytest.mark.parametrize("multi", [False, True])
def test_explicit_unchanged_decision_allows_final_approval(multi):
    plan = make_plan(multi=multi)
    before = plan.model_dump(round_trip=True)
    approved = approve(plan)
    assert plan.model_dump(round_trip=True) == before
    assert plan.review_status == LabourReviewStatus.PENDING
    assert approved.review_status == LabourReviewStatus.APPROVED_UNCHANGED
    assert approved.is_ready_for_final_approval
    approved.require_final_approval_ready()
    assert approved.total_hours == plan.total_hours
    assert approved.original_times == plan.original_times
    assert approved.review_flags == plan.review_flags
    assert approved.review_decision.reviewer_id == "test-user-id"
    assert approved.review_decision.reviewed_at.utcoffset() is not None


@pytest.mark.parametrize("change", ["hours", "scope", "operation", "add_work",
                                   "reference", "selection", "unused_original"])
def test_changed_snapshot_invalidates_but_retains_previous_decision(change):
    plan = approve(make_plan(multi=True))
    previous = plan.review_decision
    if change == "hours":
        plan.original_times[0].labour_time.hours = Decimal("2.00")
    elif change == "scope":
        plan.work_plan.work_items[0].work_item.side = Side.BOTH
    elif change == "operation":
        plan.work_plan.work_items[0].work_item.operation = Operation.SHOCK_ABSORBER_REPLACEMENT
    elif change == "add_work":
        plan.work_plan.work_items.append(PlannedWork(
            work_id="new", work_item=WorkItem(operation="BRAKE_REPLACEMENT", axle="REAR"),
        ))
    elif change == "reference":
        plan.original_times[0].labour_time.autodata_reference = "CHANGED-TEST"
    elif change == "selection":
        plan.selected_time_ids.pop()
    else:
        entry = plan.original_times[0].model_copy(deep=True)
        entry.time_id = "alternative"
        plan.original_times.append(entry)
    assert plan.content_id != previous.plan_content_id
    assert plan.review_status == LabourReviewStatus.PENDING
    assert not plan.is_ready_for_final_approval
    assert plan.review_decision == previous
    with pytest.raises(ValueError):
        plan.require_final_approval_ready()


def test_stale_content_id_cannot_approve_changed_plan():
    plan = make_plan()
    old_id = plan.content_id
    plan.original_times[0].labour_time.hours = Decimal("2.00")
    with pytest.raises(ValueError, match="changed"):
        approve_labour_plan_unchanged(plan, old_id, "test-user-id")
    assert plan.review_decision is None


def test_incomplete_plan_cannot_be_approved_even_with_current_id():
    plan = make_plan()
    plan.selected_time_ids.clear()
    with pytest.raises(ValueError, match="review"):
        approve(plan)


def test_adjusted_status_is_reserved_and_not_an_approval():
    approved = approve(make_plan())
    payload = approved.review_decision.model_dump()
    payload["status"] = LabourReviewStatus.APPROVED_ADJUSTED
    with pytest.raises(ValidationError, match="phase 3A"):
        LabourReviewDecision.model_validate(payload)
    # Even a caller bypassing Pydantic validation cannot gain final readiness.
    approved.review_decision = approved.review_decision.model_copy(update={
        "status": LabourReviewStatus.APPROVED_ADJUSTED,
    })
    assert approved.review_status == LabourReviewStatus.PENDING
    assert not approved.is_ready_for_final_approval


def test_approval_round_trip_and_order_independent_content_identity():
    approved = approve(make_plan(multi=True))
    restored = LabourPlan.model_validate_json(approved.model_dump_json(round_trip=True))
    assert restored.is_ready_for_final_approval
    restored.work_plan.work_items.reverse()
    restored.original_times.reverse()
    restored.selected_time_ids.reverse()
    restored.original_times[0].labour_time.hours = Decimal("1.6000")
    assert restored.content_id == approved.content_id
    assert restored.is_ready_for_final_approval


def test_review_cannot_be_automatically_injected_as_derived_state():
    plan = make_plan()
    with pytest.raises(ValidationError):
        LabourPlan.model_validate({**plan.model_dump(round_trip=True),
                                   "review_status": "APPROVED_UNCHANGED"})
    with pytest.raises(ValidationError, match="blank"):
        approve_labour_plan_unchanged(plan, plan.content_id, " ")


def test_single_work_price_remains_144_euros():
    plan = make_plan()
    proposal = build_raw_proposal(
        "TEST-PROPOSAL", plan.work_plan.vehicle,
        plan.work_plan.work_items[0].work_item,
        plan.original_times[0].labour_time,
    )
    assert proposal.labour_cost.total_price == Decimal("144.00")
    assert plan.require_total_hours() == Decimal("1.60")
