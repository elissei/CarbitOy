from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.labour_plan import (
    LabourPlan,
    LabourPlanIssueCode,
    LabourPlanStatus,
    LabourTimeEntry,
)
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from services.labour_planning import build_labour_plan
from services.proposal_builder import build_raw_proposal


def work(work_id, side=Side.UNKNOWN, operation=Operation.BRAKE_REPLACEMENT,
         axle=Axle.FRONT):
    return PlannedWork(
        work_id=work_id,
        work_item=WorkItem(operation=operation, axle=axle, side=side),
    )


def work_plan(*works):
    return WorkPlan(
        vehicle=Vehicle(registration="ABC-123", make="Toyota", model="Corolla"),
        work_items=list(works),
    )


def original(time_id, hours, work_ids, side=Side.UNKNOWN,
             source=LabourTimeSource.MANUAL_AUTODATA):
    return LabourTimeEntry(
        time_id=time_id,
        labour_time=LabourTime(
            hours=Decimal(hours),
            source=source,
            work_description="Original Autodata operation",
            recorded_by="Workshop mechanic",
            autodata_reference=f"AUTODATA-{time_id}",
        ),
        covered_work_ids=work_ids,
        side=side,
    )


@pytest.fixture
def pair_inputs():
    return (
        work_plan(work("shocks", Side.BOTH, Operation.SHOCK_ABSORBER_REPLACEMENT)),
        [
            original("left", "1.60", ["shocks"], Side.LEFT),
            original("right", "1.60", ["shocks"], Side.RIGHT),
            original("pair", "2.55", ["shocks"], Side.BOTH),
        ],
    )


def issue_codes(plan):
    return {issue.code for issue in plan.issues}


def assert_review(plan, code):
    assert plan.status == LabourPlanStatus.REVIEW_REQUIRED
    assert not plan.is_billable
    assert plan.total_hours is None
    assert code in issue_codes(plan)
    with pytest.raises(ValueError, match="review"):
        plan.require_total_hours()


def test_pair_time_replaces_individual_side_times(pair_inputs):
    works, entries = pair_inputs

    plan = build_labour_plan(works, entries)

    assert plan.selected_time_ids == ["pair"]
    assert plan.require_total_hours() == Decimal("2.55")
    assert plan.total_hours != Decimal("3.20")
    assert plan.status == LabourPlanStatus.READY
    assert plan.issues == []


def test_missing_pair_time_requires_review_not_sum_of_sides(pair_inputs):
    works, entries = pair_inputs

    plan = build_labour_plan(works, entries[:2])

    assert plan.selected_time_ids == []
    assert_review(plan, LabourPlanIssueCode.MISSING_PAIR_TIME)


def split_pair_inputs(axle=Axle.FRONT):
    return (
        work_plan(
            work("left-work", Side.LEFT, Operation.SHOCK_ABSORBER_REPLACEMENT, axle),
            work("right-work", Side.RIGHT, Operation.SHOCK_ABSORBER_REPLACEMENT, axle),
        ),
        [original("left", "1.60", ["left-work"], Side.LEFT),
         original("right", "1.60", ["right-work"], Side.RIGHT)],
    )


def test_confirmed_pair_as_two_work_ids_uses_own_pair_time():
    works, entries = split_pair_inputs()
    entries.append(original("pair", "2.55", ["left-work", "right-work"]))

    plan = build_labour_plan(works, entries)

    assert plan.selected_time_ids == ["pair"]
    assert plan.require_total_hours() == Decimal("2.55")


@pytest.mark.parametrize("selection", [None, ["left", "right"]])
def test_split_confirmed_pair_without_own_time_cannot_sum_sides(selection):
    works, entries = split_pair_inputs()

    plan = build_labour_plan(works, entries, selection)

    assert_review(plan, LabourPlanIssueCode.MISSING_PAIR_TIME)


def test_unconfirmed_split_pair_does_not_infer_workshop_practice():
    works, entries = split_pair_inputs(Axle.REAR)

    plan = build_labour_plan(works, entries)

    assert plan.work_plan.confirmed_split_pairs == []
    assert plan.selected_time_ids == []
    assert_review(plan, LabourPlanIssueCode.MISSING_PAIR_TIME)


def test_unconfirmed_split_pair_can_use_explicit_full_group_time():
    works, entries = split_pair_inputs(Axle.REAR)
    entries.append(original("explicit", "2.55", ["left-work", "right-work"]))

    plan = build_labour_plan(works, entries)
    chosen = build_labour_plan(works, entries, ["explicit"])

    assert_review(plan, LabourPlanIssueCode.MISSING_PAIR_TIME)
    assert chosen.require_total_hours() == Decimal("2.55")


def test_split_pair_covered_by_explicit_larger_combination_is_counted_once():
    works, entries = split_pair_inputs()
    works.work_items.append(work("brakes"))
    entries.append(original("all", "3.00", ["left-work", "right-work", "brakes"]))

    plan = build_labour_plan(works, entries, ["all"])

    assert plan.selected_time_ids == ["all"]
    assert plan.require_total_hours() == Decimal("3.00")


def test_multiple_split_pair_times_require_explicit_choice():
    works, entries = split_pair_inputs()
    entries.extend([
        original("pair", "2.55", ["left-work", "right-work"]),
        original("other-pair", "2.70", ["right-work", "left-work"]),
    ])

    plan = build_labour_plan(works, entries)
    chosen = build_labour_plan(works, entries, ["pair"])

    assert_review(plan, LabourPlanIssueCode.AMBIGUOUS_TIME)
    assert chosen.require_total_hours() == Decimal("2.55")


def test_unspecified_time_scope_is_not_assumed_to_cover_pair(pair_inputs):
    works, _ = pair_inputs

    plan = build_labour_plan(works, [original("unspecified", "3.20", ["shocks"])])

    assert_review(plan, LabourPlanIssueCode.MISSING_PAIR_TIME)


def test_explicit_side_sum_cannot_make_a_pair_billable(pair_inputs):
    works, entries = pair_inputs

    plan = build_labour_plan(works, entries[:2], ["left", "right"])

    assert_review(plan, LabourPlanIssueCode.INCOMPLETE_SCOPE)


def test_independent_operation_times_are_added_without_overlap_deduction():
    works = work_plan(
        work("brakes"),
        work("shocks", Side.LEFT, Operation.SHOCK_ABSORBER_REPLACEMENT),
    )
    entries = [
        original("brake-time", "0.80", ["brakes"]),
        original("shock-time", "1.60", ["shocks"], Side.LEFT),
    ]

    plan = build_labour_plan(works, entries)

    assert plan.selected_time_ids == ["brake-time", "shock-time"]
    assert plan.require_total_hours() == Decimal("2.40")


def test_total_keeps_decimal_hours_without_rounding():
    plan = build_labour_plan(
        work_plan(work("first"), work("second")),
        [original("one", "1.3355", ["first"]),
         original("two", "1.3355", ["second"])],
    )

    assert plan.require_total_hours() == Decimal("2.6710")


def test_explicit_combined_time_replaces_covered_individual_times():
    works = work_plan(work("first"), work("second"))
    entries = [
        original("one", "1.00", ["first"]),
        original("two", "2.00", ["second"]),
        original("combined", "2.40", ["first", "second"]),
    ]

    plan = build_labour_plan(works, entries, ["combined"])

    assert plan.selected_time_ids == ["combined"]
    assert plan.require_total_hours() == Decimal("2.40")
    assert len(plan.original_times) == 3


def test_available_combined_time_is_not_selected_without_explicit_choice():
    works = work_plan(work("first"), work("second"))
    entries = [
        original("one", "1.00", ["first"]),
        original("two", "2.00", ["second"]),
        original("combined", "2.40", ["first", "second"]),
    ]

    plan = build_labour_plan(works, entries)

    assert plan.selected_time_ids == ["one", "two"]
    assert plan.require_total_hours() == Decimal("3.00")


def test_only_combined_time_requires_explicit_choice():
    works = work_plan(work("first"), work("second"))
    entries = [original("combined", "2.40", ["first", "second"])]

    plan = build_labour_plan(works, entries)
    chosen = build_labour_plan(works, entries, ["combined"])

    assert plan.selected_time_ids == []
    assert_review(plan, LabourPlanIssueCode.MISSING_TIME)
    assert chosen.require_total_hours() == Decimal("2.40")


def test_partial_combination_adds_uncovered_work_separately():
    plan = build_labour_plan(
        work_plan(work("first"), work("second"), work("third")),
        [original("one", "1.00", ["first"]),
         original("two", "2.00", ["second"]),
         original("three", "0.75", ["third"]),
         original("combined", "2.40", ["first", "second"])],
        ["combined"],
    )

    assert plan.selected_time_ids == ["combined", "three"]
    assert plan.require_total_hours() == Decimal("3.15")


def test_multiple_disjoint_combined_groups_can_be_selected():
    plan = build_labour_plan(
        work_plan(*(work(key) for key in ["a", "b", "c", "d"])),
        [original("ab", "2.00", ["a", "b"]),
         original("cd", "3.00", ["c", "d"])],
        ["ab", "cd"],
    )

    assert plan.require_total_hours() == Decimal("5.00")


def test_combined_record_explicitly_covers_full_pair_scope(pair_inputs):
    works, entries = pair_inputs
    works.work_items.append(work("brakes"))
    entries.append(original("combined", "3.00", ["shocks", "brakes"]))

    plan = build_labour_plan(works, entries, ["combined"])

    assert plan.require_total_hours() == Decimal("3.00")
    assert plan.selected_time_ids == ["combined"]


def test_combined_and_individual_coverage_cannot_be_billed_twice():
    plan = build_labour_plan(
        work_plan(work("first"), work("second")),
        [original("one", "1.00", ["first"]),
         original("combined", "2.40", ["first", "second"])],
        ["combined", "one"],
    )

    assert_review(plan, LabourPlanIssueCode.OVERLAPPING_COVERAGE)
    issue = next(item for item in plan.issues
                 if item.code == LabourPlanIssueCode.OVERLAPPING_COVERAGE)
    assert issue.work_ids == ["first"]
    assert issue.time_ids == ["combined", "one"]


def test_overlapping_combined_groups_require_review():
    plan = build_labour_plan(
        work_plan(work("a"), work("b"), work("c")),
        [original("ab", "2.00", ["a", "b"]),
         original("bc", "2.00", ["b", "c"])],
        ["ab", "bc"],
    )

    assert_review(plan, LabourPlanIssueCode.OVERLAPPING_COVERAGE)


def test_repeated_selected_time_is_not_billed_twice():
    plan = build_labour_plan(
        work_plan(work("a")), [original("one", "1.00", ["a"])],
        ["one", "one"],
    )

    assert_review(plan, LabourPlanIssueCode.DUPLICATE_SELECTION)


def test_missing_required_time_blocks_total_even_if_other_work_is_priced():
    plan = build_labour_plan(
        work_plan(work("a"), work("b")), [original("one", "1.00", ["a"])],
    )

    assert plan.selected_time_ids == ["one"]
    assert_review(plan, LabourPlanIssueCode.MISSING_TIME)


def test_empty_originals_are_incomplete_not_zero_hours():
    plan = build_labour_plan(work_plan(work("a")), [])

    assert_review(plan, LabourPlanIssueCode.MISSING_TIME)


def test_unknown_selected_time_requires_review():
    plan = build_labour_plan(
        work_plan(work("a")), [original("one", "1.00", ["a"])], ["missing"],
    )

    assert_review(plan, LabourPlanIssueCode.UNKNOWN_TIME)


def test_unknown_work_in_original_coverage_requires_review():
    plan = build_labour_plan(
        work_plan(work("a")),
        [original("invalid-combination", "1.00", ["a", "unknown"])],
        ["invalid-combination"],
    )

    assert_review(plan, LabourPlanIssueCode.UNKNOWN_WORK)


def test_multiple_matching_times_require_explicit_choice():
    works = work_plan(work("a"))
    entries = [original("one", "1.00", ["a"]),
               original("alternative", "1.20", ["a"])]

    plan = build_labour_plan(works, entries)
    chosen = build_labour_plan(works, entries, ["alternative"])

    assert_review(plan, LabourPlanIssueCode.AMBIGUOUS_TIME)
    assert chosen.require_total_hours() == Decimal("1.20")


def test_multiple_pair_times_require_explicit_choice(pair_inputs):
    works, entries = pair_inputs
    entries.append(original("pair-alternative", "2.70", ["shocks"], Side.BOTH))

    plan = build_labour_plan(works, entries)

    assert_review(plan, LabourPlanIssueCode.AMBIGUOUS_TIME)


@pytest.mark.parametrize("side", [Side.LEFT, Side.RIGHT, Side.NOT_APPLICABLE,
                                  Side.UNKNOWN])
def test_single_work_time_matches_explicit_work_scope(side):
    plan = build_labour_plan(
        work_plan(work("a", side)), [original("one", "1.60", ["a"], side)],
    )

    assert plan.require_total_hours() == Decimal("1.60")


def test_wrong_side_time_is_not_selected():
    plan = build_labour_plan(
        work_plan(work("a", Side.RIGHT)),
        [original("left", "1.60", ["a"], Side.LEFT)],
    )

    assert_review(plan, LabourPlanIssueCode.MISSING_TIME)


def test_workshop_pair_practice_does_not_expand_single_side_work():
    plan = build_labour_plan(
        work_plan(work("shocks", Side.LEFT, Operation.SHOCK_ABSORBER_REPLACEMENT)),
        [original("left", "1.60", ["shocks"], Side.LEFT),
         original("pair", "2.55", ["shocks"], Side.BOTH)],
    )

    assert plan.selected_time_ids == ["left"]
    assert plan.require_total_hours() == Decimal("1.60")


def test_unconfirmed_rear_pair_practice_is_not_inferred():
    works = work_plan(work("shocks", Side.UNKNOWN,
                           Operation.SHOCK_ABSORBER_REPLACEMENT, Axle.REAR))

    plan = build_labour_plan(works, [original("one", "1.60", ["shocks"])])

    assert plan.work_plan.work_items[0].work_item.side == Side.UNKNOWN
    assert plan.require_total_hours() == Decimal("1.60")


def test_original_times_provenance_and_input_objects_are_preserved(pair_inputs):
    works, entries = pair_inputs
    original_work = works.model_dump()
    original_entries = [entry.model_dump() for entry in entries]
    selection = ["pair"]

    plan = build_labour_plan(works, entries, selection)

    assert works.model_dump() == original_work
    assert [entry.model_dump() for entry in entries] == original_entries
    assert [entry.model_dump() for entry in plan.original_times] == original_entries
    pair = plan.original_times[2].labour_time
    assert pair.source == LabourTimeSource.MANUAL_AUTODATA
    assert pair.recorded_by == "Workshop mechanic"
    assert pair.autodata_reference == "AUTODATA-pair"
    assert selection == ["pair"]

    plan.original_times[0].labour_time.hours = Decimal("9.00")
    plan.work_plan.vehicle.registration = "OTHER"
    plan.selected_time_ids.clear()
    assert [entry.model_dump() for entry in entries] == original_entries
    assert works.model_dump() == original_work
    assert selection == ["pair"]


def test_source_autodata_can_be_stored_without_manual_provenance():
    entry = LabourTimeEntry(
        time_id="original", covered_work_ids=["a"],
        labour_time=LabourTime(hours=Decimal("1.60"), source="AUTODATA"),
    )

    plan = build_labour_plan(work_plan(work("a")), [entry])

    assert plan.original_times[0].labour_time.source == LabourTimeSource.AUTODATA
    assert plan.original_times[0].labour_time.recorded_by is None
    assert plan.require_total_hours() == Decimal("1.60")


@pytest.mark.parametrize("ids", [[], ["a", "a"], [" "]])
def test_original_entry_requires_unique_nonblank_coverage(ids):
    with pytest.raises(ValidationError):
        original("one", "1.00", ids)


@pytest.mark.parametrize("time_id", ["", " "])
def test_original_entry_requires_nonblank_id(time_id):
    with pytest.raises(ValidationError):
        original(time_id, "1.00", ["a"])


def test_duplicate_original_time_ids_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        build_labour_plan(
            work_plan(work("a")),
            [original("one", "1.00", ["a"]), original("one", "1.20", ["a"])],
        )


def test_combined_entry_cannot_claim_only_one_side_of_covered_works():
    with pytest.raises(ValidationError, match="scopes"):
        original("combined", "2.00", ["a", "b"], Side.LEFT)


def test_labour_plan_serializes_status_and_can_restore_original_data(pair_inputs):
    plan = build_labour_plan(*pair_inputs)

    payload = plan.model_dump(mode="json")
    restored = LabourPlan.model_validate_json(plan.model_dump_json(round_trip=True))

    assert payload["status"] == "READY"
    assert payload["total_hours"] == "2.55"
    assert restored.selected_time_ids == ["pair"]
    assert restored.require_total_hours() == Decimal("2.55")


def test_derived_state_cannot_be_overridden_by_caller(pair_inputs):
    plan = build_labour_plan(*pair_inputs)

    with pytest.raises(ValidationError):
        LabourPlan.model_validate({**plan.model_dump(round_trip=True),
                                   "status": "READY", "total_hours": "999"})


def test_state_is_rechecked_after_selection_changes(pair_inputs):
    plan = build_labour_plan(*pair_inputs)
    assert plan.is_billable

    plan.selected_time_ids.append("left")

    assert_review(plan, LabourPlanIssueCode.INCOMPLETE_SCOPE)


def test_existing_single_work_proposal_builder_remains_compatible():
    works = work_plan(work("brakes"))
    entry = original("brake-time", "1.60", ["brakes"])
    plan = build_labour_plan(works, [entry])

    proposal = build_raw_proposal(
        "PROPOSAL-001", works.vehicle, works.work_items[0].work_item,
        entry.labour_time,
    )

    assert plan.require_total_hours() == proposal.labour_time.hours
    assert proposal.labour_cost.total_price == Decimal("144.00")
    assert proposal.labour_cost.hours == Decimal("1.60")
