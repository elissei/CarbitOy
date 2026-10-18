import json
from datetime import datetime, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from uuid import uuid4

import pytest
from http_auth_support import AuthenticatedTestClient as TestClient
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.labour_cost import LabourCost
from domain.models.labour_time import LabourTime
from domain.models.part_sale_price import PartSalePrice
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from services.labour_pricing import calculate_labour_cost, split_inclusive_vat
from webapp.approved_labour_pricing import (
    ApprovedLabourPriceSnapshot, ApprovedLabourPricingRequest, ApprovedLabourPricingService,
)
from webapp.database import VersionConflictError
from workshop_support import ScopedTestRepository as WorkRepository
from webapp.labour_models import OriginalWorkTimeRecord
from webapp.labour_service import (
    ApproveLabourPlanCommand, LabourPlanService, SaveLabourPlanCommand,
    StaleLabourPlanError, TrustedWorkshopContext, WorkshopPermission, build_storage_labour_plan,
)
from http_auth_support import create_test_app as create_app


# Test-only identity; production authentication/context issuance is not supplied.
from workshop_support import test_context
CONTEXT = test_context("pricing-test-employee")


@pytest.fixture
def environment(tmp_path):
    repository = WorkRepository(tmp_path / "pricing.db")
    repository.initialize()
    inquiry = repository.create_work(registration="TEST-123", customer_description="Test works",
                                     actor="Recorder", idempotency_key=str(uuid4()))
    plans = LabourPlanService(repository)
    return repository, plans, ApprovedLabourPricingService(plans), inquiry["id"]


def save_plan(plans, inquiry_id, hours=("1.60", "2.00"), version=1):
    works = [PlannedWork(work_id=f"work-{i}", work_item=WorkItem(
        operation=Operation.BRAKE_REPLACEMENT if i == 0 else Operation.SHOCK_ABSORBER_REPLACEMENT,
        axle=Axle.FRONT, side=Side.BOTH,
    )) for i in range(len(hours))]
    work_plan = WorkPlan(vehicle=Vehicle(registration="TEST-123", make="Test", model="Test"), work_items=works)
    originals = tuple(OriginalWorkTimeRecord(
        work_item_id=work.work_id, operation_identifier=work.work_item.operation.value,
        axle="FRONT", side="BOTH", original_hours=value,
        source="MANUAL_AUTODATA" if value is not None else None,
        work_description=f"Original operation {i}", autodata_reference=f"TEST-REF-{i}", recorded_by="Recorder",
    ) for i, (work, value) in enumerate(zip(works, hours)))
    preview = build_storage_labour_plan(work_plan, originals)
    return plans.save_plan(CONTEXT, SaveLabourPlanCommand(
        inquiry_id=inquiry_id, expected_version=version, expected_content_id=preview.content_id,
        expected_rules_version=preview.review_rules_version, idempotency_key=str(uuid4()),
        work_plan=work_plan, original_times=originals,
    ))


def approval(result, final=None):
    return ApproveLabourPlanCommand(
        inquiry_id=result.inquiry_id, expected_version=result.work_version,
        expected_revision_id=result.revision_id, expected_content_id=result.content_id,
        expected_rules_version=result.rules_version, idempotency_key=str(uuid4()),
        status="APPROVED_UNCHANGED" if final is None else "APPROVED_ADJUSTED",
        final_billable_hours=final, reason=None if final is None else "Documented workshop decision",
    )


def request(result, **changes):
    return ApprovedLabourPricingRequest(**(dict(
        inquiry_id=result.inquiry_id, expected_version=result.work_version,
        expected_revision_id=result.revision_id, expected_content_id=result.content_id,
        expected_rules_version=result.rules_version,
    ) | changes))


def database_snapshot(repository):
    with repository._connect() as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables}


@pytest.mark.parametrize("final,gross,net,vat", [
    (None, "324.00", "258.17", "65.83"),
    ("2.75", "247.50", "197.21", "50.29"),
    ("4.80", "432.00", "344.22", "87.78"),
    ("0.00", "0.00", "0.00", "0.00"),
])
def test_approved_final_hours_price_with_unchanged_originals(environment, final, gross, net, vat):
    repository, plans, pricing, inquiry_id = environment
    saved = save_plan(plans, inquiry_id)
    originals = plans.get_active_plan(CONTEXT, inquiry_id).revision.items
    approved = plans.approve_plan(CONTEXT, approval(saved, final))
    before = database_snapshot(repository)
    result = pricing.price_approved_labour(CONTEXT, request(approved))
    assert result.final_billable_hours == Decimal(final if final is not None else "3.60")
    assert result.original_total_hours == Decimal("3.60")
    assert (result.total_price, result.net_price, result.vat_price) == tuple(map(Decimal, (gross, net, vat)))
    assert result.net_price + result.vat_price == result.total_price
    assert result.hourly_rate == Decimal("90.00") and result.vat_rate == Decimal("0.255")
    assert result.decision_id == approved.decision_id
    assert result.revision_id == approved.revision_id and result.work_version == approved.work_version
    assert result.content_id == approved.content_id and result.rules_version == approved.rules_version
    assert result.created_at.utcoffset().total_seconds() == 0
    assert plans.get_active_plan(CONTEXT, inquiry_id).revision.items == originals
    assert originals[0].original_hours == Decimal("1.60")
    assert originals[0].source.value == "MANUAL_AUTODATA"
    assert originals[0].recorded_by == "Recorder" and originals[0].autodata_reference == "TEST-REF-0"
    assert database_snapshot(repository) == before
    assert pricing.require_current_snapshot(CONTEXT, result) == result


def test_zero_labour_keeps_parts_and_old_positive_models_separate(environment):
    _, plans, pricing, inquiry_id = environment
    saved = save_plan(plans, inquiry_id)
    approved = plans.approve_plan(CONTEXT, approval(saved, "0.00"))
    parts = PartSalePrice(purchase_price=Decimal("100.00"), sale_price=Decimal("150.00"), currency="EUR")
    original_parts = parts.model_dump()
    result = pricing.price_approved_labour(CONTEXT, request(approved))
    assert result.total_price + parts.sale_price == Decimal("150.00")
    assert parts.model_dump() == original_parts
    assert json.loads(result.model_dump_json())["total_price"] == "0.00"
    with pytest.raises(ValidationError):
        LabourTime(hours="0.00", source="AUTODATA")
    with pytest.raises(ValidationError):
        LabourCost(hours="0.00", hourly_rate="90.00", total_price="0.00", currency="EUR")


@pytest.mark.parametrize("hours", [("1.60",), ("1.60", "2.00"), ("1.60", None)])
def test_missing_or_incomplete_approval_cannot_price(environment, hours):
    repository, plans, pricing, inquiry_id = environment
    saved = save_plan(plans, inquiry_id, hours=hours)
    before = database_snapshot(repository)
    with pytest.raises(ValueError, match="approved"):
        pricing.price_approved_labour(CONTEXT, request(saved))
    assert database_snapshot(repository) == before
    assert plans.decision_history(CONTEXT, inquiry_id) == ()


def test_no_plan_cannot_price(environment):
    repository, _, pricing, inquiry_id = environment
    before = database_snapshot(repository)
    with pytest.raises(ValueError):
        pricing.price_approved_labour(CONTEXT, ApprovedLabourPricingRequest(
            inquiry_id=inquiry_id, expected_version=1, expected_revision_id="missing",
            expected_content_id="0" * 64, expected_rules_version="3B-v1",
        ))
    assert database_snapshot(repository) == before


@pytest.mark.parametrize("changes,error", [
    ({"expected_version": 1}, VersionConflictError),
    ({"expected_revision_id": "different"}, StaleLabourPlanError),
    ({"expected_content_id": "f" * 64}, StaleLabourPlanError),
    ({"expected_rules_version": "old-rules"}, StaleLabourPlanError),
])
def test_stale_request_cannot_price(environment, changes, error):
    repository, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    before = database_snapshot(repository)
    with pytest.raises(error):
        pricing.price_approved_labour(CONTEXT, request(approved, **changes))
    assert database_snapshot(repository) == before


@pytest.mark.parametrize("hours", [("1.60", "2.00"), ("1.70", "2.00")])
def test_new_revision_invalidates_historical_price_even_when_content_same(environment, hours):
    _, plans, pricing, inquiry_id = environment
    command = approval(save_plan(plans, inquiry_id), "0.00")
    approved = plans.approve_plan(CONTEXT, command)
    old = pricing.price_approved_labour(CONTEXT, request(approved))
    history = old.model_dump_json()
    newer = save_plan(plans, inquiry_id, hours=hours, version=approved.work_version)
    # Historical idempotency responses are not current authority.
    assert plans.approve_plan(CONTEXT, command) == approved
    with pytest.raises(VersionConflictError):
        pricing.price_approved_labour(CONTEXT, request(approved))
    with pytest.raises(VersionConflictError):
        pricing.require_current_snapshot(CONTEXT, old)
    with pytest.raises(ValueError, match="approved"):
        pricing.price_approved_labour(CONTEXT, request(newer))
    assert old.model_dump_json() == history
    assert len(plans.decision_history(CONTEXT, inquiry_id)) == 1
    renewed = plans.approve_plan(CONTEXT, approval(newer))
    assert pricing.price_approved_labour(CONTEXT, request(renewed)).decision_id != old.decision_id


def test_rules_change_invalidates_price_until_new_revision_and_review(environment, monkeypatch):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    old = pricing.price_approved_labour(CONTEXT, request(approved))
    monkeypatch.setattr("domain.models.labour_plan.LABOUR_REVIEW_RULES_VERSION", "test-next-rules")
    with pytest.raises(StaleLabourPlanError):
        pricing.price_approved_labour(CONTEXT, request(approved))
    with pytest.raises(StaleLabourPlanError):
        pricing.require_current_snapshot(CONTEXT, old)
    newer = save_plan(plans, inquiry_id, version=approved.work_version)
    renewed = plans.approve_plan(CONTEXT, approval(newer))
    result = pricing.price_approved_labour(CONTEXT, request(renewed))
    assert result.rules_version == "test-next-rules" and result.content_id != old.content_id
    assert len(plans.decision_history(CONTEXT, inquiry_id)) == 2


def test_new_decision_invalidates_old_price_on_same_revision(environment):
    _, plans, pricing, inquiry_id = environment
    first = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    old = pricing.price_approved_labour(CONTEXT, request(first))
    second = plans.approve_plan(CONTEXT, approval(first, "0.00"))
    with pytest.raises(VersionConflictError):
        pricing.require_current_snapshot(CONTEXT, old)
    current = pricing.price_approved_labour(CONTEXT, request(second))
    assert current.revision_id == old.revision_id and current.content_id == old.content_id
    assert current.decision_id != old.decision_id and current.total_price == Decimal("0.00")


def test_service_gate_is_authoritative_and_concurrent_change_rejects_mixed_snapshot(environment, monkeypatch):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    gate = plans.require_approved_final_hours
    calls = []

    def observe(context, **arguments):
        calls.append(arguments)
        return gate(context, **arguments)

    monkeypatch.setattr(plans, "require_approved_final_hours", observe)
    assert pricing.price_approved_labour(CONTEXT, request(approved)).final_billable_hours == Decimal("3.60")
    assert calls == [request(approved).model_dump()]

    def concurrent_change(context, **arguments):
        plans.approve_plan(CONTEXT, approval(approved, "0.00"))
        return gate(context, **arguments)

    monkeypatch.setattr(plans, "require_approved_final_hours", concurrent_change)
    with pytest.raises(VersionConflictError):
        pricing.price_approved_labour(CONTEXT, request(approved))


@pytest.mark.parametrize("hours,gross,net,vat", [
    ("1.60", "144.00", "114.74", "29.26"),
    ("1.3355", "120.20", "95.78", "24.42"),
    ("0.0005", "0.05", "0.04", "0.01"),
    ("0.00001", "0.00", "0.00", "0.00"),
    ("-0.00", "0.00", "0.00", "0.00"),
    ("1.334999999999999999999999999999999999999999", "120.15", "95.74", "24.41"),
    ("123456789012345678901234567890.12345", "11111111011111111101111111110111.11",
     "8853474909251881355467020804869.41", "2257636101859229745644090305241.70"),
])
def test_decimal_precision_and_rounding_are_independent_of_caller(environment, hours, gross, net, vat):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id), hours))
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_DOWN
        # Existing review arithmetic also needs to sum original hours. Its
        # totals here are exact; only monetary division is inexact.
        context.traps[Inexact] = True
        result = pricing.price_approved_labour(CONTEXT, request(approved))
        assert context.prec == 4 and context.rounding == ROUND_DOWN
        assert context.traps[Inexact]
    assert result.final_billable_hours == Decimal(hours)
    assert (result.total_price, result.net_price, result.vat_price) == tuple(map(Decimal, (gross, net, vat)))
    assert json.loads(result.model_dump_json())["final_billable_hours"] == ("0.00" if hours == "-0.00" else hours)


def test_total_is_rounded_once_after_approved_sum(environment):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id, hours=("0.0005", "0.0005"))))
    result = pricing.price_approved_labour(CONTEXT, request(approved))
    assert result.final_billable_hours == Decimal("0.0010")
    assert result.total_price == Decimal("0.09")  # Not two separately rounded 0.05 prices.


def test_single_work_matches_legacy_price_and_vat(environment):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id, hours=("1.60",))))
    result = pricing.price_approved_labour(CONTEXT, request(approved))
    legacy = calculate_labour_cost(LabourTime(hours="1.60", source="AUTODATA"))
    assert result.total_price == legacy.total_price == Decimal("144.00")
    assert (result.net_price, result.vat_price) == split_inclusive_vat(legacy.total_price)


@pytest.mark.parametrize("field,value", [
    ("final_billable_hours", "0.00"), ("hourly_rate", "1.00"),
    ("reviewer_id", "browser"), ("role", "admin"), ("timestamp", "2020-01-01T00:00:00Z"),
])
def test_request_forbids_client_hours_money_and_identity(environment, field, value):
    _, plans, _, inquiry_id = environment
    saved = save_plan(plans, inquiry_id)
    with pytest.raises(ValidationError):
        request(saved, **{field: value})


def test_pricing_requires_trusted_read_permission_without_approval_permission(environment):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    reader = test_context("trusted-reader", frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    assert pricing.price_approved_labour(reader, request(approved)).total_price == Decimal("324.00")
    for untrusted in ({"employee_id": "browser", "permissions": ["READ_LABOUR_PLAN"]},
                      TrustedWorkshopContext("no-permission", frozenset())):
        with pytest.raises(PermissionError):
            pricing.price_approved_labour(untrusted, request(approved))


def test_snapshot_is_immutable_exact_json_and_not_proof_of_approval(environment):
    _, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id), "0.00"))
    result = pricing.price_approved_labour(CONTEXT, request(approved))
    assert ApprovedLabourPriceSnapshot.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.final_billable_hours = Decimal("3.60")
    # A plausible externally modified snapshot must still match current storage.
    for changes in ({"decision_id": "forged"}, {"pricing_rules_version": "old-policy"},
                    {"final_billable_hours": Decimal("1.00")},
                    {"total_price": Decimal("90.00"), "net_price": Decimal("71.71"), "vat_price": Decimal("18.29")}):
        with pytest.raises(StaleLabourPlanError):
            pricing.require_current_snapshot(CONTEXT, result.model_copy(update=changes))
    with pytest.raises(ValidationError):
        ApprovedLabourPriceSnapshot.model_validate(result.model_dump() | {"total_price": 0.0})
    with pytest.raises(ValidationError):
        ApprovedLabourPriceSnapshot.model_validate(result.model_dump() | {"created_at": datetime(2020, 1, 1)})
    assert result.created_at.tzinfo == timezone.utc


def test_pricing_failure_writes_nothing(environment, monkeypatch):
    repository, plans, pricing, inquiry_id = environment
    approved = plans.approve_plan(CONTEXT, approval(save_plan(plans, inquiry_id)))
    before = database_snapshot(repository)

    def fail(*args):
        raise ArithmeticError("test calculation failure")

    monkeypatch.setattr("webapp.approved_labour_pricing.split_inclusive_vat", fail)
    with pytest.raises(ArithmeticError):
        pricing.price_approved_labour(CONTEXT, request(approved))
    assert database_snapshot(repository) == before
    assert plans.require_approved_final_hours(CONTEXT, **request(approved).model_dump()) == Decimal("3.60")


def test_http_stays_legacy_and_cannot_be_used_as_final_approval(environment):
    repository, plans, pricing, inquiry_id = environment
    app = create_app(repository.database_path)
    with TestClient(app) as client:
        assert set(app.openapi()["paths"]) == {"/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour"}
        response = client.put(f"/api/works/{inquiry_id}/labour", json={
            "version": 1, "hours": "1.60", "work_description": "Original single work",
            "recorded_by": "Recorder", "autodata_reference": "TEST-LEGACY",
        })
        assert response.status_code == 200
        work = response.json()
        assert Decimal(work["total_price"]) == Decimal("144.00")
        saved = save_plan(plans, inquiry_id, hours=("1.60",), version=work["version"])
        with pytest.raises(ValueError, match="approved"):
            pricing.price_approved_labour(CONTEXT, request(saved))
        approved = plans.approve_plan(CONTEXT, approval(saved, "0.00"))
        result = pricing.price_approved_labour(CONTEXT, request(approved))
        # The preliminary legacy price is not overwritten by final pricing.
        assert result.total_price == Decimal("0.00")
        assert Decimal(client.get(f"/api/works/{inquiry_id}").json()["total_price"]) == Decimal("144.00")
