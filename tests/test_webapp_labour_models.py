from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from webapp.labour_models import (
    ActiveLabourPlanRecord, LabourPlanRequestRecord, LabourPlanRevisionRecord,
    LabourReviewDecisionRecord, OriginalWorkTimeRecord, decimal_storage_text,
)


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def original(work_id="shocks", hours="1.60"):
    return OriginalWorkTimeRecord(
        work_item_id=work_id, operation_identifier="SHOCK_ABSORBER_REPLACEMENT",
        axle="FRONT", side="BOTH", original_hours=hours,
        source="MANUAL_AUTODATA", work_description="Original Autodata operation",
        autodata_reference="TEST-REFERENCE", recorded_by="Recorder",
    )


def revision(**changes):
    values = dict(
        revision_id="revision-1", inquiry_id="inquiry-1", revision=1,
        content_id="a" * 64, rules_version="3B-v1", coverage_status="READY",
        original_total_hours="1.60", items=(original(),), created_at=NOW,
    )
    return LabourPlanRevisionRecord(**(values | changes))


def decision(**changes):
    values = dict(
        decision_id="decision-1", inquiry_id="inquiry-1", revision_id="revision-1",
        content_id="a" * 64, rules_version="3B-v1", status="APPROVED_ADJUSTED",
        original_total_hours="1.60", final_billable_hours="0.00",
        reason="Explicit no-charge decision", reviewer_id="employee-stable-id",
        reviewed_at=NOW, event_id=1,
    )
    return LabourReviewDecisionRecord(**(values | changes))


def test_original_sum_and_final_zero_are_separate_exact_decimal_values():
    plan = revision()
    outcome = decision()

    assert plan.original_total_hours == Decimal("1.60")
    assert plan.items[0].original_hours == Decimal("1.60")
    assert outcome.final_billable_hours == Decimal("0.00")
    assert outcome.model_dump(mode="json")["final_billable_hours"] == "0.00"
    assert LabourReviewDecisionRecord.model_validate_json(outcome.model_dump_json()) == outcome


def test_revision_validates_unchanged_multi_work_sum():
    plan = revision(items=(original("left"), original("right", "2.00")),
                    original_total_hours="3.60")
    assert plan.original_total_hours == Decimal("3.60")
    with pytest.raises(ValidationError, match="unchanged"):
        revision(items=plan.items, original_total_hours="3.00")


def test_revision_rejects_duplicate_work_ids():
    with pytest.raises(ValidationError, match="unique"):
        revision(items=(original(), original()), original_total_hours="3.20")


def test_original_sum_validation_does_not_round_with_decimal_context():
    items = (original("first", "12345678901234567890.123456789"),
             original("second", "0.000000001"))
    with localcontext() as context:
        context.prec = 3
        plan = revision(items=items, original_total_hours="12345678901234567890.123456790")
        assert plan.original_total_hours == Decimal("12345678901234567890.123456790")
        with pytest.raises(ValidationError, match="unchanged"):
            revision(items=items, original_total_hours="12300000000000000000")


def test_incomplete_revision_does_not_claim_an_original_total():
    unfinished = OriginalWorkTimeRecord(work_item_id="missing", operation_identifier="BRAKE_REPLACEMENT")
    plan = revision(items=(original(), unfinished), coverage_status="REVIEW_REQUIRED",
                    original_total_hours=None)
    assert plan.original_total_hours is None
    with pytest.raises(ValidationError):
        revision(items=(unfinished,))
    with pytest.raises(ValidationError):
        revision(items=(unfinished,), coverage_status="REVIEW_REQUIRED")


@pytest.mark.parametrize("hours", [True, False, 1.6, "NaN", "Infinity", "-0.01"])
def test_decision_rejects_inexact_or_invalid_final_hours(hours):
    with pytest.raises(ValidationError):
        decision(final_billable_hours=hours)


def test_original_hours_remain_positive_even_when_final_zero_is_allowed():
    with pytest.raises(ValidationError):
        original(hours="0.00")


def test_changed_decision_requires_nonblank_reason_and_stable_reviewer():
    for changes in ({"reason": None}, {"reason": " "}, {"reviewer_id": " "}):
        with pytest.raises(ValidationError):
            decision(**changes)


def test_unchanged_decision_requires_original_final_time():
    assert decision(status="APPROVED_UNCHANGED", final_billable_hours="1.60", reason=None)
    with pytest.raises(ValidationError, match="unchanged"):
        decision(status="APPROVED_UNCHANGED", final_billable_hours="0.00")


def test_snapshot_models_are_frozen_and_require_aware_timestamps():
    plan = revision()
    with pytest.raises(ValidationError):
        plan.revision = 2
    with pytest.raises(ValidationError):
        revision(created_at=datetime(2026, 10, 9))
    with pytest.raises(ValidationError):
        decision(reviewed_at=datetime(2026, 10, 9))


def test_content_identity_rules_and_revision_are_required():
    for changes in ({"content_id": "not-a-hash"}, {"rules_version": " "},
                    {"revision": 0}, {"revision": True}):
        with pytest.raises(ValidationError):
            revision(**changes)


@pytest.mark.parametrize("hours, expected", [
    ("1E-7", "0.0000001"), ("1E+3", "1000"), ("-0.00", "0.00"),
    ("12345678901234567890.123456789", "12345678901234567890.123456789"),
])
def test_decimal_storage_and_json_preserve_exact_fixed_point_text(hours, expected):
    outcome = decision(final_billable_hours=hours)
    assert decimal_storage_text(outcome.final_billable_hours) == expected
    assert outcome.model_dump(mode="json")["final_billable_hours"] == expected
    assert outcome.model_dump()["final_billable_hours"] == Decimal(hours)


def test_timestamps_normalize_to_utc():
    timestamp = NOW.astimezone(timezone(timedelta(hours=3)))
    assert decision(reviewed_at=timestamp).reviewed_at == NOW
    assert revision(created_at=timestamp).created_at.tzinfo == timezone.utc


def test_active_state_can_have_an_unapproved_revision():
    state = ActiveLabourPlanRecord(inquiry_id="inquiry-1", active_revision_id="revision-1")
    assert state.active_decision_id is None


def test_idempotency_snapshot_requires_valid_operation_result():
    values = dict(
        inquiry_id="inquiry-1", actor_id="employee-stable-id", operation="SAVE_PLAN",
        idempotency_key="retry-key", request_json='{"intent":"save"}',
        response_json='{"saved":true}', revision_id="revision-1",
        result_work_version=7, created_at=NOW,
    )
    assert LabourPlanRequestRecord(**values).decision_id is None
    assert LabourPlanRequestRecord(**(values | {
        "operation": "APPROVE_LABOUR", "decision_id": "decision-1",
    }))
    for changes in (
        {"request_json": "[]"}, {"response_json": "invalid-json"},
        {"actor_id": " "}, {"operation": "APPROVE_LABOUR"},
        {"decision_id": "decision-1"}, {"result_work_version": True},
    ):
        with pytest.raises(ValidationError):
            LabourPlanRequestRecord(**(values | changes))
