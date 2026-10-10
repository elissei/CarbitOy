import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from threading import Barrier
from uuid import uuid4

import pytest
from http_auth_support import AuthenticatedTestClient as TestClient
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.labour_plan import LabourReviewStatus
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from webapp.database import IdempotencyConflictError, VersionConflictError, WorkRepository
from webapp.labour_models import OriginalWorkTimeRecord
from webapp.labour_service import (
    ApproveLabourPlanCommand, LabourPlanService, SaveLabourPlanCommand,
    StaleLabourPlanError, StoredLabourPlanError, TrustedWorkshopContext,
    WorkshopPermission, build_storage_labour_plan,
)
from http_auth_support import create_test_app as create_app


# Explicit test context only. Production has no authentication/context issuer yet.
TEST_CONTEXT = TrustedWorkshopContext("test-employee-stable-id", frozenset(WorkshopPermission))


@pytest.fixture
def environment(tmp_path):
    repository = WorkRepository(tmp_path / "service.db")
    repository.initialize()
    inquiry = repository.create_work(registration="TEST-123", customer_description="Fictional works",
                                     actor="Original recorder", idempotency_key=str(uuid4()))
    return repository, LabourPlanService(repository), inquiry["id"]


def save_command(inquiry_id, hours=("1.60", "2.00"), version=1, **changes):
    works = [PlannedWork(work_id=f"work-{i}", work_item=WorkItem(
        operation=(Operation.BRAKE_REPLACEMENT if i == 0 else Operation.SHOCK_ABSORBER_REPLACEMENT),
        axle=Axle.FRONT, side=Side.BOTH,
    )) for i, _ in enumerate(hours)]
    work_plan = WorkPlan(vehicle=Vehicle(registration="TEST-123", make="Test", model="Test"), work_items=works)
    originals = tuple(OriginalWorkTimeRecord(
        work_item_id=work.work_id, operation_identifier=work.work_item.operation.value,
        axle=work.work_item.axle.value, side=work.work_item.side.value,
        original_hours=value, source="MANUAL_AUTODATA" if value is not None else None,
        work_description=f"Fictional original operation {i}", recorded_by="Original recorder",
        autodata_reference=f"TEST-AUTODATA-{i}",
    ) for i, (work, value) in enumerate(zip(works, hours)))
    preview = build_storage_labour_plan(work_plan, originals)
    values = dict(inquiry_id=inquiry_id, expected_version=version,
                  expected_content_id=preview.content_id, expected_rules_version=preview.review_rules_version,
                  idempotency_key=str(uuid4()), work_plan=work_plan, original_times=originals)
    return SaveLabourPlanCommand(**(values | changes))


def approval(result, final=None, reason=None, **changes):
    values = dict(inquiry_id=result.inquiry_id, expected_version=result.work_version,
                  expected_content_id=result.content_id, expected_rules_version=result.rules_version,
                  expected_revision_id=result.revision_id, idempotency_key=str(uuid4()),
                  status="APPROVED_UNCHANGED" if final is None else "APPROVED_ADJUSTED",
                  final_billable_hours=final, reason=reason)
    return ApproveLabourPlanCommand(**(values | changes))


def approved_hours(service, result, **changes):
    arguments = dict(inquiry_id=result.inquiry_id, expected_version=result.work_version,
                     expected_revision_id=result.revision_id, expected_content_id=result.content_id,
                     expected_rules_version=result.rules_version)
    return service.require_approved_final_hours(TEST_CONTEXT, **(arguments | changes))


def snapshot(repository):
    with repository._connect() as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables}


def test_save_preserves_sources_and_sums_without_pricing_or_implicit_approval(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id)
    before = command.model_dump(round_trip=True)
    result = service.save_plan(TEST_CONTEXT, command)
    assert command.model_dump(round_trip=True) == before
    assert result.original_total_hours == Decimal("3.60")
    assert result.human_review_required
    assert result.review_status == LabourReviewStatus.PENDING
    assert result.final_billable_hours is None
    assert result.revision == 1 and result.work_version == 2
    stored = service.get_active_plan(TEST_CONTEXT, inquiry_id)
    assert stored.plan.content_id == result.content_id
    assert stored.plan.require_total_hours() == Decimal("3.60")
    assert stored.plan.original_times[0].labour_time.autodata_reference == "TEST-AUTODATA-0"
    assert stored.plan.original_times[0].labour_time.recorded_by == "Original recorder"
    assert not stored.plan.is_ready_for_final_approval
    with pytest.raises(ValueError, match="approved"):
        approved_hours(service, result)
    assert repository.get_work(inquiry_id)["labour_hours"] is None
    assert repository.get_work(inquiry_id)["total_price"] is None


def test_unchanged_approval_uses_domain_and_trusted_server_identity(environment, monkeypatch):
    repository, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    import webapp.labour_service as module
    original = module.approve_labour_plan_unchanged
    called = []

    def observe(*args):
        called.append(args[2])
        return original(*args)

    monkeypatch.setattr(module, "approve_labour_plan_unchanged", observe)
    outcome = service.approve_plan(TEST_CONTEXT, approval(result))
    assert called == [TEST_CONTEXT.employee_id]
    assert outcome.review_status == LabourReviewStatus.APPROVED_UNCHANGED
    assert outcome.final_billable_hours == Decimal("3.60")
    assert approved_hours(service, outcome) == Decimal("3.60")
    stored = service.get_active_plan(TEST_CONTEXT, inquiry_id)
    assert stored.plan.review_decision.final_hours is None
    assert stored.plan.is_ready_for_final_approval
    history = service.decision_history(TEST_CONTEXT, inquiry_id)
    assert len(history) == 1 and history[0].reviewer_id == TEST_CONTEXT.employee_id
    assert history[0].reviewed_at.utcoffset().total_seconds() == 0
    event = repository.get_work(inquiry_id)["events"][0]
    assert event["actor"] == TEST_CONTEXT.employee_id
    assert event["occurred_at"] == history[0].reviewed_at.isoformat()
    assert event["work_version"] == outcome.work_version
    assert event["details"]["decision_id"] == outcome.decision_id


@pytest.mark.parametrize("hours", ["2.75", "4.80", "0.00"])
def test_adjustment_preserves_originals_and_supports_zero(environment, hours, monkeypatch):
    _, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    original_times = service.get_active_plan(TEST_CONTEXT, inquiry_id).plan.original_times
    import webapp.labour_service as module
    original = module.approve_labour_plan_adjusted
    called = []

    def observe(*args):
        called.append(args[3])
        return original(*args)

    monkeypatch.setattr(module, "approve_labour_plan_adjusted", observe)
    outcome = service.approve_plan(TEST_CONTEXT, approval(result, final=hours, reason="Workshop decision"))
    assert called == [Decimal(hours)]
    assert outcome.review_status == LabourReviewStatus.APPROVED_ADJUSTED
    assert outcome.final_billable_hours == Decimal(hours)
    assert outcome.original_total_hours == Decimal("3.60")
    assert approved_hours(service, outcome) == Decimal(hours)
    restored = LabourPlanService(service.repository).get_active_plan(TEST_CONTEXT, inquiry_id)
    assert restored.plan.original_times == original_times
    assert restored.plan.require_approved_final_hours() == Decimal(hours)
    assert restored.plan.review_decision.reason == "Workshop decision"


@pytest.mark.parametrize("changes", [
    {"reason": None}, {"reason": " "}, {"final_billable_hours": "-0.01"},
    {"final_billable_hours": True}, {"final_billable_hours": 1.6},
    {"final_billable_hours": "NaN"}, {"final_billable_hours": "Infinity"},
])
def test_invalid_adjustment_is_rejected_without_writes(environment, changes):
    repository, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    before = snapshot(repository)
    payload = approval(result, final="0.00", reason="Reason").model_dump(round_trip=True) | changes
    with pytest.raises(ValidationError):
        service.approve_plan(TEST_CONTEXT, ApproveLabourPlanCommand.model_validate(payload))
    assert snapshot(repository) == before


def test_incomplete_plan_is_saved_but_cannot_be_approved(environment):
    repository, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, hours=("1.60", None)))
    assert result.coverage_status == "REVIEW_REQUIRED" and result.original_total_hours is None
    assert len(service.get_active_plan(TEST_CONTEXT, inquiry_id).revision.items) == 2
    before = snapshot(repository)
    with pytest.raises(ValueError, match="review"):
        service.approve_plan(TEST_CONTEXT, approval(result))
    assert snapshot(repository) == before


def test_pair_is_one_both_work_not_an_automatic_sum_of_separate_sides(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id, hours=("1.60", "1.60"))
    for i, work in enumerate(command.work_plan.work_items):
        work.work_item.operation = Operation.SHOCK_ABSORBER_REPLACEMENT
        work.work_item.side = Side.LEFT if i == 0 else Side.RIGHT
    originals = tuple(item.model_copy(update={
        "operation_identifier": "SHOCK_ABSORBER_REPLACEMENT", "side": "LEFT" if i == 0 else "RIGHT",
    }) for i, item in enumerate(command.original_times))
    preview = build_storage_labour_plan(command.work_plan, originals)
    command = command.model_copy(update={"original_times": originals, "expected_content_id": preview.content_id})
    result = service.save_plan(TEST_CONTEXT, command)
    assert result.coverage_status == "REVIEW_REQUIRED" and result.original_total_hours is None
    with pytest.raises(ValueError):
        service.approve_plan(TEST_CONTEXT, approval(result, final="2.55", reason="Not a substitute for original pair time"))
    pair = save_command(inquiry_id, hours=("2.55",), version=2)
    pair.work_plan.work_items[0].work_item.operation = Operation.SHOCK_ABSORBER_REPLACEMENT
    pair_items = (pair.original_times[0].model_copy(update={"operation_identifier": "SHOCK_ABSORBER_REPLACEMENT"}),)
    preview = build_storage_labour_plan(pair.work_plan, pair_items)
    pair = pair.model_copy(update={"original_times": pair_items, "expected_content_id": preview.content_id})
    result = service.save_plan(TEST_CONTEXT, pair)
    assert result.original_total_hours == Decimal("2.55")
    assert not result.human_review_required


@pytest.mark.parametrize("changes", [
    {"expected_content_id": "f" * 64}, {"expected_rules_version": "old-rules"},
    {"expected_revision_id": "old-revision"},
])
def test_stale_review_request_cannot_write(environment, changes):
    repository, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    before = snapshot(repository)
    with pytest.raises(StaleLabourPlanError):
        service.approve_plan(TEST_CONTEXT, approval(result, **changes))
    assert snapshot(repository) == before


@pytest.mark.parametrize("changes", [
    {"expected_content_id": "f" * 64}, {"expected_rules_version": "old-rules"},
])
def test_stale_plan_creation_cannot_write(environment, changes):
    repository, service, inquiry_id = environment
    before = snapshot(repository)
    with pytest.raises(StaleLabourPlanError):
        service.save_plan(TEST_CONTEXT, save_command(inquiry_id, **changes))
    assert snapshot(repository) == before


def test_rules_change_requires_new_revision_and_preserves_decisions(environment, monkeypatch):
    _, service, inquiry_id = environment
    result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    outcome = service.approve_plan(TEST_CONTEXT, approval(result))
    monkeypatch.setattr("domain.models.labour_plan.LABOUR_REVIEW_RULES_VERSION", "test-next-rules")
    with pytest.raises(StaleLabourPlanError):
        approved_hours(service, outcome)
    with pytest.raises(StaleLabourPlanError):
        service.approve_plan(TEST_CONTEXT, approval(outcome))
    assert service.decision_history(TEST_CONTEXT, inquiry_id)[0].rules_version == "3B-v1"
    new_result = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, version=outcome.work_version))
    assert new_result.content_id != outcome.content_id
    assert new_result.review_status == LabourReviewStatus.PENDING
    renewed = service.approve_plan(TEST_CONTEXT, approval(new_result))
    assert approved_hours(service, renewed) == Decimal("3.60")
    assert len(service.decision_history(TEST_CONTEXT, inquiry_id)) == 2


def test_new_revision_invalidates_approval_and_old_retry_is_only_historical(environment):
    repository, service, inquiry_id = environment
    first = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    request = approval(first, final="0.00", reason="No charge")
    outcome = service.approve_plan(TEST_CONTEXT, request)
    newer = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, hours=("1.70", "2.00"), version=outcome.work_version))
    before = snapshot(repository)
    assert service.approve_plan(TEST_CONTEXT, request) == outcome
    assert snapshot(repository) == before
    with pytest.raises(ValueError, match="approved"):
        approved_hours(service, newer)
    with pytest.raises(StaleLabourPlanError):
        service.approve_plan(TEST_CONTEXT, approval(outcome, expected_version=newer.work_version))
    current = service.get_active_plan(TEST_CONTEXT, inquiry_id)
    assert current.plan.review_decision is None
    assert current.plan.review_history[0].final_hours == Decimal("0.00")
    renewed = service.approve_plan(TEST_CONTEXT, approval(newer))
    assert approved_hours(service, renewed) == Decimal("3.70")
    history = service.decision_history(TEST_CONTEXT, inquiry_id)
    assert [record.original_total_hours for record in history] == [Decimal("3.60"), Decimal("3.70")]


def test_even_identical_new_revision_requires_a_new_explicit_decision(environment):
    _, service, inquiry_id = environment
    first = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    outcome = service.approve_plan(TEST_CONTEXT, approval(first))
    newer = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, version=outcome.work_version))
    assert newer.content_id == first.content_id and newer.revision_id != first.revision_id
    with pytest.raises(StaleLabourPlanError):
        service.approve_plan(TEST_CONTEXT, approval(outcome, expected_version=newer.work_version))
    with pytest.raises(ValueError, match="approved"):
        approved_hours(service, newer)


def test_multiple_decisions_preserve_history_and_only_latest_time_applies(environment):
    _, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    first = service.approve_plan(TEST_CONTEXT, approval(saved))
    second = service.approve_plan(TEST_CONTEXT, approval(first, final="0.00", reason="No charge"))
    third = service.approve_plan(TEST_CONTEXT, approval(second, final="4.50", reason="Additional effort"))
    plan = service.get_active_plan(TEST_CONTEXT, inquiry_id).plan
    assert [item.final_hours for item in plan.review_history] == [None, Decimal("0.00")]
    assert approved_hours(service, third) == Decimal("4.50")
    assert len(service.decision_history(TEST_CONTEXT, inquiry_id)) == 3


@pytest.mark.parametrize("operation", ["save", "approve"])
def test_idempotent_retries_survive_restart_and_conflicting_payload_is_rejected(environment, operation):
    repository, service, inquiry_id = environment
    if operation == "save":
        command = save_command(inquiry_id)
        method = service.save_plan
    else:
        saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
        command = approval(saved)
        method = service.approve_plan
    result = method(TEST_CONTEXT, command)
    before = snapshot(repository)
    restored = LabourPlanService(WorkRepository(repository.database_path))
    repeat = restored.save_plan if operation == "save" else restored.approve_plan
    assert repeat(TEST_CONTEXT, command) == result
    assert snapshot(repository) == before
    with pytest.raises(IdempotencyConflictError):
        repeat(TEST_CONTEXT, command.model_copy(update={"expected_version": command.expected_version + 1}))
    assert snapshot(repository) == before


@pytest.mark.parametrize("operation", ["save", "approve"])
@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_requests_produce_one_atomic_change(environment, operation, same_request):
    repository, service, inquiry_id = environment
    if operation == "save":
        first = save_command(inquiry_id)
    else:
        saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
        first = approval(saved)
    second = first if same_request else first.model_copy(update={"idempotency_key": str(uuid4())})
    barrier = Barrier(2)

    def run(command):
        other = LabourPlanService(WorkRepository(repository.database_path))
        barrier.wait(timeout=5)
        try:
            return (other.save_plan if operation == "save" else other.approve_plan)(TEST_CONTEXT, command)
        except VersionConflictError:
            return "version-conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, (first, second)))
    if same_request:
        assert outcomes[0] == outcomes[1]
    else:
        assert outcomes.count("version-conflict") == 1
    assert repository.get_work(inquiry_id)["version"] == first.expected_version + 1
    with repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM labour_plan_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM labour_review_decisions").fetchone()[0] == (1 if operation == "approve" else 0)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("operation", ["save", "approve"])
@pytest.mark.parametrize("failure_step", ["event", "idempotency"])
def test_service_transaction_rolls_back_all_writes_on_failure(environment, monkeypatch, operation, failure_step):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    reviewed = service.approve_plan(TEST_CONTEXT, approval(saved))
    command = (save_command(inquiry_id, hours=("1.70", "2.00"), version=reviewed.work_version)
               if operation == "save" else approval(reviewed, final="0.00", reason="No charge"))
    before = snapshot(repository)

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated final write failure")

    monkeypatch.setattr(service, "_append_event" if failure_step == "event" else "_remember", fail)
    with pytest.raises(sqlite3.IntegrityError):
        (service.save_plan if operation == "save" else service.approve_plan)(TEST_CONTEXT, command)
    assert snapshot(repository) == before


@pytest.mark.parametrize("field, value", [
    ("reviewer_id", "browser-user"), ("role", "admin"), ("timestamp", "2026-10-01T00:00:00Z"),
    ("reviewed_at", "2026-10-01T00:00:00Z"),
])
def test_review_commands_reject_client_identity_role_and_timestamp(environment, field, value):
    _, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    payload = approval(saved).model_dump(round_trip=True) | {field: value}
    with pytest.raises(ValidationError):
        ApproveLabourPlanCommand.model_validate(payload)


def test_only_trusted_authorized_context_can_write_or_replay(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id)
    saved = service.save_plan(TEST_CONTEXT, command)
    request = approval(saved)
    service.approve_plan(TEST_CONTEXT, request)
    before = snapshot(repository)
    for untrusted in ({"employee_id": "browser-user", "role": "admin"}, "browser-user",
                      TrustedWorkshopContext(TEST_CONTEXT.employee_id, frozenset({WorkshopPermission.READ_LABOUR_PLAN}))):
        with pytest.raises(PermissionError):
            service.approve_plan(untrusted, request)
        with pytest.raises(PermissionError):
            service.save_plan(untrusted, command)
    assert snapshot(repository) == before


def test_idempotency_is_scoped_to_trusted_employee(environment):
    _, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    request = approval(saved)
    first = service.approve_plan(TEST_CONTEXT, request)
    other = TrustedWorkshopContext("another-test-employee", frozenset(WorkshopPermission))
    with pytest.raises(VersionConflictError):
        service.approve_plan(other, request)
    second = service.approve_plan(other, request.model_copy(update={"expected_version": first.work_version}))
    assert second.decision_id != first.decision_id
    assert service.decision_history(TEST_CONTEXT, inquiry_id)[-1].reviewer_id == other.employee_id


def test_approval_is_not_exposed_over_http_and_legacy_pricing_still_works(environment):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, hours=("1.60",)))
    assert not saved.human_review_required
    with TestClient(create_app(repository.database_path)) as client:
        assert client.get("/openapi.json").status_code == 404
        paths = set(client.app.openapi()["paths"])
        assert paths == {"/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour"}
        assert client.post(f"/api/works/{inquiry_id}/approve", json={"reviewer_id": "admin"}).status_code == 404
        response = client.put(f"/api/works/{inquiry_id}/labour", json={
            "version": saved.work_version, "hours": "1.60", "work_description": "Fictional original work",
            "recorded_by": "Legacy recorder", "autodata_reference": "TEST-LEGACY",
        })
        assert response.status_code == 200
        assert response.json()["total_price"] == "144.00"
    with pytest.raises(VersionConflictError):
        service.approve_plan(TEST_CONTEXT, approval(saved))


def test_exact_original_sum_and_zero_survive_low_decimal_precision(environment):
    _, service, inquiry_id = environment
    command = save_command(inquiry_id, hours=("12345678901234567890.123456789", "0.000000001"))
    with localcontext() as context:
        context.prec = 3
        saved = service.save_plan(TEST_CONTEXT, command)
        assert saved.original_total_hours == Decimal("12345678901234567890.123456790")
        reviewed = service.approve_plan(TEST_CONTEXT, approval(saved, final="0.00", reason="No charge"))
        assert approved_hours(service, reviewed) == Decimal("0.00")
        assert reviewed.original_total_hours == saved.original_total_hours


def test_saved_snapshot_contains_real_domain_content_and_no_fabricated_approval(environment):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    with repository._connect() as connection:
        row = connection.execute("SELECT snapshot_json FROM labour_plan_revisions WHERE revision_id=?", (saved.revision_id,)).fetchone()
    source = json.loads(row[0])
    assert source["format"] == "web4b-v1"
    assert source["plan"]["review_decision"] is None
    assert source["plan"]["review_history"] == []
    assert len(source["plan"]["work_plan"]["work_items"]) == 2
    assert source["plan"]["original_times"][0]["labour_time"]["hours"] == "1.60"


def test_inconsistent_original_operation_is_rejected_without_writes(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id)
    items = (command.original_times[0].model_copy(update={"side": "LEFT"}), command.original_times[1])
    before = snapshot(repository)
    with pytest.raises(ValueError, match="scope"):
        service.save_plan(TEST_CONTEXT, command.model_copy(update={"original_times": items}))
    assert snapshot(repository) == before


@pytest.mark.parametrize("field, value", [
    ("content_id", "f" * 64), ("rules_version", "old-rules"),
])
def test_stored_identity_is_verified_before_using_a_revision(environment, monkeypatch, field, value):
    repository, service, inquiry_id = environment
    insert = service._insert_revision

    def inconsistent_writer(connection, record, plan):
        # Simulate a legacy/incorrect writer without disabling any DB protections.
        insert(connection, record.model_copy(update={field: value}), plan)

    monkeypatch.setattr(service, "_insert_revision", inconsistent_writer)
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    before = snapshot(repository)
    with pytest.raises(StaleLabourPlanError):
        service.approve_plan(TEST_CONTEXT, approval(saved))
    with pytest.raises(StaleLabourPlanError):
        approved_hours(service, saved)
    assert snapshot(repository) == before


def test_legacy_snapshot_is_not_silently_treated_as_an_approved_plan(environment):
    repository, service, inquiry_id = environment
    # Write a genuine pre-4B revision via the old storage model contract.
    command = save_command(inquiry_id)
    from webapp.labour_models import LabourPlanRevisionRecord
    from datetime import datetime, timezone
    record = LabourPlanRevisionRecord(
        revision_id="legacy-plan", inquiry_id=inquiry_id, revision=1,
        content_id=command.expected_content_id, rules_version=command.expected_rules_version,
        coverage_status="READY", original_total_hours="3.60", items=command.original_times,
        created_at=datetime.now(timezone.utc),
    )
    with repository._connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for position, item in enumerate(record.items, 1):
            connection.execute("INSERT INTO inquiry_work_items VALUES (?, ?, ?)",
                               (inquiry_id, item.work_item_id, record.created_at.isoformat()))
            connection.execute("INSERT INTO labour_plan_revision_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (record.revision_id, inquiry_id, item.work_item_id, position,
                                item.operation_identifier, item.axle, item.side, str(item.original_hours),
                                item.source.value, item.work_description, item.autodata_reference, item.recorded_by))
        connection.execute("INSERT INTO labour_plan_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (record.revision_id, inquiry_id, 1, record.content_id, record.rules_version,
                            "READY", "3.60", 2, record.model_dump_json(), record.created_at.isoformat()))
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, NULL)", (inquiry_id, record.revision_id))
    before = snapshot(repository)
    with pytest.raises(StoredLabourPlanError, match="new revision"):
        service.get_active_plan(TEST_CONTEXT, inquiry_id)
    assert snapshot(repository) == before
    fresh = service.save_plan(TEST_CONTEXT, command)
    assert fresh.revision == 2 and fresh.review_status == LabourReviewStatus.PENDING
    assert len(service.decision_history(TEST_CONTEXT, inquiry_id)) == 0


def test_revision_and_inquiry_versions_are_independent(environment):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    first = service.approve_plan(TEST_CONTEXT, approval(saved))
    second = service.approve_plan(TEST_CONTEXT, approval(first, final="0.00", reason="No charge"))
    newer = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, version=second.work_version))
    assert newer.revision == 2 and newer.work_version == 5
    assert repository.get_work(inquiry_id)["version"] == 5


def test_inquiry_identity_and_missing_work_records_are_rejected(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id)
    before = snapshot(repository)
    missing = command.model_copy(update={"original_times": command.original_times[:1]})
    with pytest.raises(ValueError, match="each work"):
        service.save_plan(TEST_CONTEXT, missing)
    command.work_plan.vehicle.registration = "OTHER-123"
    preview = build_storage_labour_plan(command.work_plan, command.original_times)
    other_vehicle = command.model_copy(update={"expected_content_id": preview.content_id})
    with pytest.raises(ValueError, match="vehicle"):
        service.save_plan(TEST_CONTEXT, other_vehicle)
    assert snapshot(repository) == before


def test_originals_do_not_gain_automatic_overlap_reductions(environment):
    _, service, inquiry_id = environment
    command = save_command(inquiry_id)
    command.work_plan.work_items[1].work_item.operation = Operation.BRAKE_REPLACEMENT
    items = (command.original_times[0], command.original_times[1].model_copy(update={
        "operation_identifier": "BRAKE_REPLACEMENT",
    }))
    preview = build_storage_labour_plan(command.work_plan, items)
    command = command.model_copy(update={"original_times": items, "expected_content_id": preview.content_id})
    saved = service.save_plan(TEST_CONTEXT, command)
    assert saved.original_total_hours == Decimal("3.60")
    assert saved.human_review_required
    assert [flag.code.value for flag in service.get_active_plan(TEST_CONTEXT, inquiry_id).plan.review_flags] == ["MULTI_WORK_REVIEW_REQUIRED"]


def test_save_and_approval_idempotency_are_separate_operations(environment):
    repository, service, inquiry_id = environment
    command = save_command(inquiry_id)
    saved = service.save_plan(TEST_CONTEXT, command)
    service.approve_plan(TEST_CONTEXT, approval(saved, idempotency_key=command.idempotency_key))
    with repository._connect() as connection:
        assert [row[0] for row in connection.execute(
            "SELECT operation FROM labour_plan_requests ORDER BY rowid"
        )] == ["SAVE_PLAN", "APPROVE_LABOUR"]


@pytest.mark.parametrize("location", ["current", "history"])
def test_approval_in_revision_json_does_not_grant_trusted_approval(environment, monkeypatch, location):
    repository, service, inquiry_id = environment
    insert = service._insert_revision
    from services.labour_planning import approve_labour_plan_unchanged

    def forged_source_writer(connection, record, plan):
        forged = approve_labour_plan_unchanged(plan, plan.content_id, "untrusted-source-reviewer")
        if location == "history":
            forged.review_history = (forged.review_decision,)
            forged.review_decision = None
        insert(connection, record, forged)

    monkeypatch.setattr(service, "_insert_revision", forged_source_writer)
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    before = snapshot(repository)
    with pytest.raises(StoredLabourPlanError, match="must not contain"):
        service.approve_plan(TEST_CONTEXT, approval(saved))
    with pytest.raises(StoredLabourPlanError):
        approved_hours(service, saved)
    assert snapshot(repository) == before
    assert service.decision_history(TEST_CONTEXT, inquiry_id) == ()


def test_active_pointer_cannot_reinstate_an_older_decision(environment):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    first = service.approve_plan(TEST_CONTEXT, approval(saved))
    second = service.approve_plan(TEST_CONTEXT, approval(first, final="0.00", reason="No charge"))
    with repository._connect() as connection, connection:
        connection.execute("UPDATE inquiry_labour_plan_state SET active_decision_id=? WHERE inquiry_id=?",
                           (first.decision_id, inquiry_id))
    with pytest.raises(StaleLabourPlanError, match="latest decision"):
        approved_hours(service, second)
    assert len(service.decision_history(TEST_CONTEXT, inquiry_id)) == 2


def test_active_pointer_cannot_reinstate_an_older_revision(environment):
    repository, service, inquiry_id = environment
    saved = service.save_plan(TEST_CONTEXT, save_command(inquiry_id))
    first = service.approve_plan(TEST_CONTEXT, approval(saved))
    newer = service.save_plan(TEST_CONTEXT, save_command(inquiry_id, hours=("1.70", "2.00"), version=first.work_version))
    with repository._connect() as connection, connection:
        connection.execute("UPDATE inquiry_labour_plan_state SET active_revision_id=?, active_decision_id=? WHERE inquiry_id=?",
                           (first.revision_id, first.decision_id, inquiry_id))
    with pytest.raises(StaleLabourPlanError, match="latest revision"):
        approved_hours(service, first, expected_version=newer.work_version)
    assert len(service.decision_history(TEST_CONTEXT, inquiry_id)) == 1
