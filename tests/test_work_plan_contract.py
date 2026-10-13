"""WD-2 contracts/adapters independent of web persistence modules."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.work_plan_adapters import (
    IncompleteWorkPlanError, missing_work_data, to_domain_labour_plan, to_domain_work_plan,
)
from app.work_plan_contract import (
    LabourApprovalRecord, PartNeed, SourceRevision, TimeData, VehicleData,
    WorkData, WorkPlanSnapshot, WorkReviewRecord,
)
from domain.models.enums import Axle, Side
from domain.models.labour_plan import LabourPlanIssueCode, LabourReviewStatus
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.part_requirement import RequirementUnit
from domain.models.work_item import Operation
from services.labour_planning import approve_labour_plan_adjusted, approve_labour_plan_unchanged
from services.labour_pricing import calculate_labour_cost, split_inclusive_vat


def snapshot(**changes):
    values = dict(
        organization_id="org-1", workshop_id="shop-1", inquiry_id="inquiry-1",
        plan_id="plan-1", revision_id="revision-1", revision=1,
        vehicle=VehicleData(registration="TEST-123", make="Test", model="Test"),
    )
    return WorkPlanSnapshot(**(values | changes))


def work(work_id="brakes", display_order=0, **changes):
    return WorkData(**(dict(
        work_id=work_id, display_order=display_order,
        operation=Operation.BRAKE_REPLACEMENT, axle=Axle.FRONT, side=Side.BOTH,
    ) | changes))


def time(time_id="pair-time", covered_work_ids=("brakes",), **changes):
    return TimeData(**(dict(
        time_id=time_id, covered_work_ids=covered_work_ids, hours="1.60",
        source=LabourTimeSource.MANUAL_AUTODATA, side=Side.BOTH,
        work_description="Full pair replacement", recorded_by="employee-1",
        autodata_reference="TEST-REF",
    ) | changes))


def check(plan, work_id):
    return WorkReviewRecord(
        review_id=f"review-{work_id}", plan_revision=plan.revision_ref, work_id=work_id,
        reviewer_id="employee-1", reviewed_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )


def with_changes(plan, **changes):
    return WorkPlanSnapshot.model_validate(plan.model_dump(round_trip=True) | changes)


def test_unidentified_inquiry_round_trips_without_fake_domain_work():
    plan = snapshot(vehicle=VehicleData(), customer_description="Noise from vehicle")
    assert WorkPlanSnapshot.model_validate_json(plan.model_dump_json()) == plan
    assert plan.works == () and not plan.all_works_checked
    assert {item.path for item in missing_work_data(plan)} == {
        "vehicle.registration", "vehicle.make", "vehicle.model", "works",
    }
    with pytest.raises(IncompleteWorkPlanError) as error:
        to_domain_work_plan(plan)
    assert "works" in {item.path for item in error.value.missing}


def test_multiple_works_keep_work_local_parts_and_display_order():
    brakes = work(part_needs=(PartNeed(
        requirement_id="pads", canonical_product_type="brake_pad", axle=Axle.FRONT,
        required_quantity=1, required_unit=RequirementUnit.SET,
    ),))
    shocks = work("shocks", 3, operation=Operation.SHOCK_ABSORBER_REPLACEMENT,
                  part_needs=(PartNeed(
                      requirement_id="shock-pair", canonical_product_type="shock_absorber",
                      axle=Axle.FRONT, required_quantity=2, required_unit=RequirementUnit.PCS,
                  ),))
    plan = snapshot(works=(shocks, brakes))
    domain = to_domain_work_plan(plan)
    assert [item.work_id for item in domain.work_items] == ["brakes", "shocks"]
    assert [item.work_item.part_requirements[0].required_quantity for item in domain.work_items] == [1, 2]
    domain.work_items[0].work_item.part_requirements[0].required_quantity = 99
    assert brakes.part_needs[0].required_quantity == 1


def test_ids_survive_reorder_delete_insert_revision_and_json():
    original = snapshot(works=(work(), work("shocks", 1)))
    revised = with_changes(original, revision=2, revision_id="revision-2",
                           previous_revision_id=original.revision_id,
                           works=(work("shocks", 5), work("new-work", 0)))
    restored = WorkPlanSnapshot.model_validate_json(revised.model_dump_json())
    assert restored.plan_id == original.plan_id
    assert [item.work_id for item in to_domain_work_plan(restored).work_items] == ["new-work", "shocks"]
    assert restored.revision_ref != original.revision_ref


@pytest.mark.parametrize("unit,quantity", [(RequirementUnit.PAIR, 1), (RequirementUnit.SET, 1), (RequirementUnit.PCS, 2)])
def test_full_pair_or_set_time_is_used_once(unit, quantity):
    plan = snapshot(works=(work(part_needs=(PartNeed(
        requirement_id="parts", canonical_product_type="test", axle=Axle.FRONT,
        required_quantity=quantity, required_unit=unit,
    ),)),), original_times=(time(),), selected_time_ids=("pair-time",))
    domain = to_domain_labour_plan(plan)
    assert domain.require_total_hours() == Decimal("1.60")
    assert domain.review_status == LabourReviewStatus.PENDING
    assert domain.approved_final_hours is None


def test_multiple_work_times_sum_without_overlap_deductions():
    plan = snapshot(works=(work(), work("shocks", 1, operation=Operation.SHOCK_ABSORBER_REPLACEMENT)),
                    original_times=(time(), time("shock-time", ("shocks",), hours="2.30")),
                    selected_time_ids=("pair-time", "shock-time"))
    domain = to_domain_labour_plan(plan)
    assert domain.require_total_hours() == Decimal("3.90")
    assert len(domain.review_flags) == 1
    assert domain.review_status == LabourReviewStatus.PENDING


def test_explicit_combined_time_covers_split_sides_once():
    plan = snapshot(works=(work("left", 0, side=Side.LEFT), work("right", 1, side=Side.RIGHT)),
                    original_times=(time("combined", ("left", "right"), side=Side.UNKNOWN),),
                    selected_time_ids=("combined",))
    assert to_domain_labour_plan(plan).require_total_hours() == Decimal("1.60")


def test_separate_side_times_do_not_guess_a_pair_total():
    plan = snapshot(works=(work("left", 0, side=Side.LEFT), work("right", 1, side=Side.RIGHT)),
                    original_times=(time("left-time", ("left",), side=Side.LEFT),
                                    time("right-time", ("right",), side=Side.RIGHT)),
                    selected_time_ids=("left-time", "right-time"))
    domain = to_domain_labour_plan(plan)
    assert domain.total_hours is None
    assert LabourPlanIssueCode.MISSING_PAIR_TIME in {issue.code for issue in domain.issues}


def test_missing_times_remain_domain_coverage_issues():
    domain = to_domain_labour_plan(snapshot(works=(work(),)))
    assert domain.total_hours is None and domain.approved_final_hours is None
    assert domain.issues[0].code == LabourPlanIssueCode.MISSING_PAIR_TIME


def test_times_are_never_selected_implicitly():
    domain = to_domain_labour_plan(snapshot(works=(work(),), original_times=(time(),)))
    assert domain.selected_time_ids == [] and domain.total_hours is None


def test_wrong_side_and_overlapping_selection_retain_domain_guards():
    wrong_side = snapshot(works=(work(),), original_times=(time(side=Side.LEFT),),
                          selected_time_ids=("pair-time",))
    assert LabourPlanIssueCode.INCOMPLETE_SCOPE in {
        issue.code for issue in to_domain_labour_plan(wrong_side).issues
    }
    overlapping = snapshot(works=(work(),), original_times=(time(), time("alternative")),
                           selected_time_ids=("pair-time", "alternative"))
    domain = to_domain_labour_plan(overlapping)
    assert domain.total_hours is None
    assert LabourPlanIssueCode.OVERLAPPING_COVERAGE in {issue.code for issue in domain.issues}


@pytest.mark.parametrize("changes,path", [
    ({"operation": None}, "works[brakes].operation"),
    ({"axle": None}, "works[brakes].axle"),
    ({"side": None}, "works[brakes].side"),
    ({"part_needs": (PartNeed(requirement_id="unknown"),)}, "works[brakes].part_needs[unknown].required_unit"),
])
def test_incomplete_identified_work_reports_missing_fields(changes, path):
    plan = snapshot(works=(work(**changes),))
    assert path in {item.path for item in missing_work_data(plan)}
    with pytest.raises(IncompleteWorkPlanError):
        to_domain_work_plan(plan)


@pytest.mark.parametrize("field", ["hours", "source", "work_description", "recorded_by"])
def test_incomplete_time_is_preserved_and_reported(field):
    plan = snapshot(works=(work(),), original_times=(time(**{field: None}),))
    restored = WorkPlanSnapshot.model_validate_json(plan.model_dump_json())
    with pytest.raises(IncompleteWorkPlanError) as error:
        to_domain_labour_plan(restored)
    assert f"original_times[pair-time].{field}" in {item.path for item in error.value.missing}


def test_work_checks_are_individual_and_do_not_approve_labour():
    original = snapshot(works=(work(), work("shocks", 1)))
    one = with_changes(original, work_reviews=(check(original, "brakes"),))
    assert one.unchecked_work_ids == ("shocks",) and not one.all_works_checked
    both = with_changes(one, work_reviews=one.work_reviews + (check(one, "shocks"),))
    assert both.all_works_checked
    domain = to_domain_labour_plan(both)
    assert domain.review_decision is None and domain.review_status == LabourReviewStatus.PENDING
    assert both.historical_labour_approval is None
    with pytest.raises(ValidationError):
        WorkReviewRecord.model_validate(check(original, "brakes").model_dump() | {"customer_approved": True})


def test_labour_approval_and_work_checks_are_separate_records():
    original = snapshot(works=(work(),), original_times=(time(),), selected_time_ids=("pair-time",))
    domain = to_domain_labour_plan(original)
    approved = approve_labour_plan_unchanged(domain, domain.content_id, "employee-1")
    link = LabourApprovalRecord(
        decision_id="decision-1", labour_revision_id="labour-revision-1",
        plan_revision=original.revision_ref, decision=approved.review_decision,
    )
    linked = with_changes(original, historical_labour_approval=link)
    assert not linked.all_works_checked
    assert linked.historical_labour_approval.decision.plan_content_id == domain.content_id
    assert linked.content_id == original.content_id
    assert to_domain_labour_plan(linked).approved_final_hours is None
    checked = with_changes(linked, work_reviews=(check(linked, "brakes"),))
    assert checked.all_works_checked
    assert to_domain_labour_plan(checked).review_status == LabourReviewStatus.PENDING
    revised = with_changes(checked, revision=2, revision_id="revision-2", previous_revision_id="revision-1")
    assert revised.historical_labour_approval.plan_revision != revised.revision_ref
    assert not revised.all_works_checked


@pytest.mark.parametrize("changes", [
    {"customer_description": "changed"}, {"workshop_id": "another-shop"},
    {"inquiry_id": "another-inquiry"}, {"vehicle": VehicleData(registration="NEW", make="Test", model="Test")},
    {"works": (work(description="changed"),)}, {"works": (work(display_order=7),)},
    {"original_times": (time(hours="1.61"),)}, {"selected_time_ids": ()},
    {"source_revision": SourceRevision(kind="INQUIRY", source_id="inquiry-1", revision=2)},
])
def test_any_changed_snapshot_makes_old_work_check_stale(changes):
    original = snapshot(works=(work(),), original_times=(time(),), selected_time_ids=("pair-time",))
    checked = with_changes(original, work_reviews=(check(original, "brakes"),))
    assert checked.all_works_checked
    changed = with_changes(checked, **changes)
    assert changed.content_id != checked.content_id and not changed.all_works_checked
    assert changed.work_reviews == checked.work_reviews


def test_decimal_equivalence_and_container_order_do_not_change_identity():
    original = snapshot(works=(work(), work("shocks", 1)), original_times=(time(),))
    reordered = with_changes(original, works=tuple(reversed(original.works)),
                             original_times=(time(hours="1.6000"),))
    assert reordered.content_id == original.content_id


@pytest.mark.parametrize("changes", [
    {"works": (work(), work("brakes", 1))}, {"works": (work(), work("other", 0))},
    {"works": (work(),), "original_times": (time(), time())},
    {"works": (work(),), "original_times": (time(covered_work_ids=("unknown",)),)},
    {"selected_time_ids": ("missing",)},
    {"works": (work(),), "original_times": (time(),), "selected_time_ids": ("pair-time", "pair-time")},
    {"schema_version": 1}, {"schema_version": 2.0}, {"revision": True}, {"revision": 2},
    {"previous_revision_id": "revision-1"}, {"plan_id": " "}, {"customer_approval": True},
])
def test_invalid_identity_scope_revision_or_foreign_fields_are_rejected(changes):
    with pytest.raises(ValidationError):
        snapshot(**changes)


@pytest.mark.parametrize("value", [True, 1.6, "NaN", "Infinity", "0", "-1"])
def test_invalid_or_inexact_hours_are_rejected(value):
    with pytest.raises(ValidationError):
        time(hours=value)


def test_adapter_revalidates_unvalidated_copies_and_records_are_frozen():
    original = snapshot(works=(work(),))
    with pytest.raises(ValidationError):
        original.works[0].work_id = "different"
    with pytest.raises(ValidationError):
        to_domain_work_plan(original.model_copy(update={"works": (work(), work())}))


def test_review_requires_aware_timestamp():
    original = snapshot(works=(work(),))
    with pytest.raises(ValidationError):
        WorkReviewRecord.model_validate(check(original, "brakes").model_dump() | {
            "reviewed_at": datetime(2026, 10, 12),
        })


def test_legacy_pricing_stays_unchanged():
    original = LabourTime(hours="1.60", source=LabourTimeSource.AUTODATA)
    price = calculate_labour_cost(original)
    assert price.total_price == Decimal("144.00")
    assert split_inclusive_vat(price.total_price) == (Decimal("114.74"), Decimal("29.26"))


@pytest.mark.parametrize("changes", [
    {"reviewer_id": ""}, {"reviewer_id": " "}, {"status": "APPROVED"},
    {"reviewed_at": datetime(2026, 10, 13)},
    {"work_id": ""}, {"work_id": "unknown-work"}, {"work_id": 123},
])
def test_copied_invalid_work_check_does_not_count(changes):
    original = snapshot(works=(work(), work("shocks", 1)))
    invalid = check(original, "brakes").model_copy(update=changes)
    copied = original.model_copy(update={
        "work_reviews": (invalid, check(original, "shocks")),
    })
    assert copied.unchecked_work_ids == ("brakes",)
    assert not copied.all_works_checked
    assert copied.work_reviews[0] is invalid
    assert original.work_reviews == ()


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"content_id": "invalid"},
    {"revision_id": "old-revision"}, {"workshop_id": "another-shop"},
    {"inquiry_id": "another-inquiry"}, {"plan_id": "another-plan"},
])
def test_copied_invalid_or_foreign_revision_ref_does_not_count(changes):
    original = snapshot(works=(work(),))
    invalid_ref = original.revision_ref.model_copy(update=changes)
    invalid = check(original, "brakes").model_copy(update={"plan_revision": invalid_ref})
    copied = original.model_copy(update={"work_reviews": (invalid,)})
    assert copied.unchecked_work_ids == ("brakes",)
    assert not copied.all_works_checked


def test_copied_check_for_previous_revision_does_not_count():
    original = snapshot(works=(work(),))
    revised = with_changes(original, revision=2, revision_id="revision-2",
                           previous_revision_id="revision-1")
    old_check = check(revised, "brakes").model_copy(update={"plan_revision": original.revision_ref})
    copied = revised.model_copy(update={"work_reviews": (old_check,)})
    assert not copied.all_works_checked
    assert copied.unchecked_work_ids == ("brakes",)
    assert with_changes(revised, work_reviews=(check(revised, "brakes"),)).all_works_checked


@pytest.mark.parametrize("quantity", [0, -1, -99, True, False, 1.0, "1"])
def test_part_need_rejects_nonpositive_or_noninteger_quantity(quantity):
    with pytest.raises(ValidationError):
        PartNeed(requirement_id="parts", required_quantity=quantity)


@pytest.mark.parametrize("quantity", [None, 1, 2])
def test_part_need_accepts_unknown_or_positive_quantity(quantity):
    need = PartNeed(requirement_id="parts", required_quantity=quantity)
    assert PartNeed.model_validate_json(need.model_dump_json()).required_quantity == quantity


@pytest.mark.parametrize("quantity", [0, -1, True, False])
def test_adapter_rejects_copied_invalid_part_quantity(quantity):
    need = PartNeed(requirement_id="parts", canonical_product_type="test", axle=Axle.FRONT,
                    required_quantity=1, required_unit=RequirementUnit.SET)
    copied_need = need.model_copy(update={"required_quantity": quantity})
    copied_work = work().model_copy(update={"part_needs": (copied_need,)})
    copied = snapshot(works=(work(),)).model_copy(update={"works": (copied_work,)})
    with pytest.raises(ValidationError):
        to_domain_work_plan(copied)


@pytest.mark.parametrize("approval_kind", ["unchanged", "adjusted", "zero-adjusted"])
@pytest.mark.parametrize("stale", [False, True])
def test_historical_approved_status_never_grants_current_domain_approval(approval_kind, stale):
    original = snapshot(works=(work(),), original_times=(time(),), selected_time_ids=("pair-time",))
    domain = to_domain_labour_plan(original)
    if approval_kind == "unchanged":
        approved = approve_labour_plan_unchanged(domain, domain.content_id, "employee-1")
    else:
        hours = Decimal("0") if approval_kind == "zero-adjusted" else Decimal("1.25")
        approved = approve_labour_plan_adjusted(domain, domain.content_id, "employee-1", hours, "Test decision")
    record = LabourApprovalRecord(decision_id="decision-1", labour_revision_id="labour-revision-1",
                                  plan_revision=original.revision_ref, decision=approved.review_decision)
    current = original
    if stale:
        current = with_changes(original, revision=2, revision_id="revision-2", previous_revision_id="revision-1")
    current = with_changes(current, historical_labour_approval=record,
                           work_reviews=(check(current, "brakes"),))
    assert current.all_works_checked
    assert (record.plan_revision != current.revision_ref) == stale
    assert record.decision.status in (LabourReviewStatus.APPROVED_UNCHANGED, LabourReviewStatus.APPROVED_ADJUSTED)
    projected = to_domain_labour_plan(current)
    # Even a matching domain content hash and current work checks grant no approval.
    assert projected.content_id == approved.content_id
    assert projected.review_decision is None
    assert projected.review_status == LabourReviewStatus.PENDING
    assert projected.approved_final_hours is None
    assert not projected.is_ready_for_final_approval
    with pytest.raises(ValueError, match="no current approved final hours"):
        projected.require_approved_final_hours()
    with pytest.raises(ValueError, match="explicit current human review"):
        projected.require_final_approval_ready()


def test_old_wd2_approval_key_is_read_only_as_historical_data():
    original = snapshot(works=(work(),), original_times=(time(),), selected_time_ids=("pair-time",))
    domain = to_domain_labour_plan(original)
    approved = approve_labour_plan_unchanged(domain, domain.content_id, "employee-1")
    record = LabourApprovalRecord(decision_id="decision-1", labour_revision_id="labour-revision-1",
                                  plan_revision=original.revision_ref, decision=approved.review_decision)
    old_payload = original.model_dump(round_trip=True, exclude={"historical_labour_approval"})
    old_payload["labour_approval"] = record.model_dump()
    restored = WorkPlanSnapshot.model_validate(old_payload)
    assert restored.historical_labour_approval == record
    assert restored.content_id == original.content_id
    assert not hasattr(restored, "labour_approval")
    serialized = restored.model_dump(mode="json")
    assert "labour_approval" not in serialized
    assert serialized["historical_labour_approval"]["decision_id"] == "decision-1"
    assert WorkPlanSnapshot.model_validate_json(restored.model_dump_json()) == restored
    with pytest.raises(ValueError, match="no current approved final hours"):
        to_domain_labour_plan(restored).require_approved_final_hours()
    # Ambiguous input is rejected, never resolved by silently preferring a status.
    with pytest.raises(ValidationError):
        WorkPlanSnapshot.model_validate(old_payload | {"historical_labour_approval": None})
