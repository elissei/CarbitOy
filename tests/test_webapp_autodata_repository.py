"""Autodata 1B-1 and 1B-2.0: stored lookups, acceptance, removal, history and schema guards.

Repository level only (no HTTP, no real adapter). Fake adapter output is used
as lookup evidence; tests that need non-synthetic evidence relabel it. Every
database is a new synthetic test database under pytest's temporary directory.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_OID, uuid4, uuid5

import pytest
from pydantic import ValidationError

import webapp.autodata_migrations as autodata_migrations
from domain.models.enums import Axle, Side
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from integrations.autodata.fake_adapter import (
    FAKE_ADAPTER_VERSION, FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import (
    Choice, ChoiceIdentity, LabourTimeLookupResult, LabourTimeRequest, LookupOutcome,
    VehicleQuery, WorkQuery,
)
from integrations.autodata.synthetic_reader import SYNTHETIC_ADAPTER_VERSION
from integrations.autodata.time_parser import HOUR_UNITS, TimeRejection
from webapp.autodata_migrations import (
    ADAPTERS, CLOSE_CONFIRMATION, OUTCOMES, SNAPSHOT_KEYS, TIME_REJECTIONS, TIME_UNITS, _adapter, _time_text,
)
from webapp.autodata_records import (
    LOOKUP_CONTRACT, AcceptLookupCommand, canonical_json, command_digest, contains_vin_like,
)
from webapp.autodata_repository import (
    AlreadyAcceptedError, AlreadyRedactedError, AutodataClosedError, AutodataIntegrityError,
    AutodataRepository, ExpiredLookupError, IdentifierInRequestError, IdentifierInSearchTextError,
    InvalidLookupTimeError, LookupNotFoundError, LookupRedactedError, NotAcceptableError,
    RequestRedactedError, StaleLookupError, StaleViewError, SyntheticNotAcceptableError,
)
from webapp.database import IdempotencyConflictError, WorkNotFoundError
from webapp.labour_models import OriginalWorkTimeRecord
from webapp.labour_service import (
    ApproveLabourPlanCommand, LabourPlanService, SaveLabourPlanCommand, build_storage_labour_plan,
)
from webapp.labour_snapshot import UnpriceableLabourTimeError, build_labour_snapshot
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, WorkshopPermission
from workshop_support import TEST_ORGANIZATION, ScopedTestRepository, test_context


T0 = datetime(2026, 10, 22, 12, 0, 0, 123456, tzinfo=timezone.utc)
REGISTRATION = "ABC-123"
CONFIRM = "VEHICLE_OPERATION_VARIANT_TIME"


def choice(*path, source_id=None, details=()):
    return Choice(identity=ChoiceIdentity(source_id=source_id, selection_path=path), details=details)


BRAKES = FakeOperation(choice=choice("Jarrut", "Etujarrupalat, vaihto", source_id="SYN-O-1"),
                       variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,125 h"),))
DISCS = FakeOperation(choice=choice("Jarrut", "Etujarrulevyt, vaihto"), variants=(
    FakeVariant(choice=choice("Ilman ilmastointia"), displayed_time_text="1,5 h"),
    FakeVariant(choice=choice("Ilmastoinnilla"), displayed_time_text="1.9 h"),
))
PETROL = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1", "1.6 bensiini", source_id="SYN-V-1"),
                     operations=(BRAKES, DISCS))


def fake_result(search="etujarrupalat", *, vehicles=(PETROL,), retrieved=T0, synthetic=False, **selections):
    adapter = FakeAutodataLabourAdapter(vehicles, clock=lambda: retrieved)
    result = adapter.lookup(LabourTimeRequest(
        vehicle_query=VehicleQuery(registration=REGISTRATION), work_query=WorkQuery(search_text=search),
        **selections,
    ))
    data = result.model_dump(mode="json", exclude={"hours"})
    data["provenance"]["synthetic"] = synthetic
    return LabourTimeLookupResult.model_validate(data)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    work = ScopedTestRepository(tmp_path / "autodata.db")
    work.initialize()
    clock = Clock(T0 + timedelta(hours=1))
    autodata = AutodataRepository(work, clock=clock)
    staff = test_context("test-employee-stable-id")
    inquiry = work.create_work(registration=REGISTRATION, customer_description="Keksitty Autodata-testi",
                               actor="Testikirjaaja", idempotency_key=str(uuid4()), context=staff)
    return type("Env", (), {"work": work, "autodata": autodata, "clock": clock, "staff": staff,
                            "inquiry": inquiry, "db": tmp_path / "autodata.db"})


def lookup_sha(context, inquiry_id, request_id, version, search, vin):
    body = {"request_id": request_id, "expected_version": version, "work_query": search, "vin": vin}
    return command_digest(LOOKUP_CONTRACT, actor_user_id=context.employee_id, inquiry_id=inquiry_id, body=body)


def store(env, result=None, *, context=None, inquiry_id=None, version=1, request_id=None,
          retrieved=T0, search="etujarrupalat", vin=None, registration=REGISTRATION,
          facts=None, selections=None):
    context = context or env.staff
    inquiry_id = inquiry_id or env.inquiry["id"]
    request_id = request_id or str(uuid4())
    result = result or fake_result(search, retrieved=retrieved)
    sha = lookup_sha(context, inquiry_id, request_id, version, search, vin)
    request = LabourTimeRequest(
        vehicle_query=VehicleQuery(registration=registration, vin=vin, **(facts or {})),
        work_query=WorkQuery(search_text=search), **(selections or {}),
    )
    return env.autodata.store_lookup(
        context, inquiry_id=inquiry_id, request_id=request_id, command_sha256=sha, request=request,
        result=result, source_work_version=version, requested_at=retrieved, retrieved_at=retrieved,
    ), sha


def command(lookup=None, *, version=1, request_id=None, **overrides):
    fields = {"request_id": request_id or str(uuid4()), "expected_version": version, "confirmation": CONFIRM}
    return AcceptLookupCommand.model_validate(fields | overrides)


def accept(env, lookup, cmd=None, *, context=None, inquiry_id=None, **kwargs):
    return env.autodata.accept_lookup(context or env.staff, inquiry_id=inquiry_id or env.inquiry["id"],
                                      lookup_id=lookup["lookup_id"], command=cmd or command(lookup), **kwargs)


def redact(env, lookup, *, context=None, reason_code="IDENTIFIER_IN_SOURCE_TEXT", inquiry_id=None):
    return env.autodata.redact_lookup(context or env.staff, inquiry_id=inquiry_id or env.inquiry["id"],
                                      lookup_id=lookup["lookup_id"], reason_code=reason_code)


def close(env, *, context=None, inquiry_id=None, reason_code="RETENTION", confirmation=CLOSE_CONFIRMATION):
    return env.autodata.close_autodata(context or env.staff, inquiry_id=inquiry_id or env.inquiry["id"],
                                       reason_code=reason_code, confirmation=confirmation)


TABLES = ("autodata_lookups", "autodata_lookup_payloads", "autodata_labour_decisions",
          "autodata_decision_receipts", "autodata_redaction_events", "autodata_inquiry_closures",
          "work_events")


def counts(env):
    with sqlite3.connect(env.db) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


def sql(env, statement, parameters=()):
    with sqlite3.connect(env.db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(statement, parameters)


def inject_failure(env, action, table):
    sql(env, f"CREATE TRIGGER inject_failure BEFORE {action} ON {table} "
             "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")


def labour():
    return {"hours": "1.60", "source": "MANUAL_AUTODATA", "work_description": "Keksitty", "recorded_by": "x",
            "autodata_reference": "", "hourly_rate": "90.00", "total_price": "144.00", "net_price": "114.74",
            "vat_price": "29.26", "vat_rate": "0.255", "currency": "EUR"}


def revoke_membership(env, context):
    sql(env, "UPDATE workshop_memberships SET enabled = 0, version = version + 1 "
             "WHERE auth_store_id = ? AND user_id = ?", (context.auth_store_id, context.employee_id))


def other_inquiry(env, registration="DEF-456"):
    return env.work.create_work(registration=registration, customer_description="Toinen",
                                actor="Testikirjaaja", idempotency_key=str(uuid4()), context=env.staff)


# -- acceptance ---------------------------------------------------------------

EVENT_KEYS = {"hours", "source", "work_description", "recorded_by", "autodata_reference", "hourly_rate",
              "total_price", "net_price", "vat_price", "vat_rate", "currency", "actor_attribution", "autodata"}
EVENT_AUTODATA_KEYS = {"lookup_id", "decision_id", "time_text", "retrieved_at", "adapter_name",
                       "adapter_version", "synthetic", "confirmation"}


def test_acceptance_writes_decision_labour_event_version_and_receipt(env):
    lookup, _ = store(env)
    assert lookup["outcome"] == "FOUND" and lookup["hours"] == "0.125" and lookup["time_text"] == "0,125 h"
    outcome = accept(env, lookup)

    decision = outcome["decision"]
    assert set(decision) == {"decision_id", "inquiry_id", "lookup_id", "resulting_work_version", "decided_at"}
    assert decision["resulting_work_version"] == 2 and outcome["replayed"] is False
    work = env.work.get_work(env.inquiry["id"], context=env.staff)
    assert outcome["work"] == work
    # Same current pricing as the manual route for 0.125 h (golden values).
    assert (work["labour_hours"], work["labour_source"], work["total_price"], work["net_price"],
            work["vat_price"], work["hourly_rate"], work["vat_rate"]) == (
        "0.125", "AUTODATA", "11.25", "8.96", "2.29", "90.00", "0.255")
    assert work["autodata_reference"] == f"lookup:{lookup['lookup_id']}"
    # P2: no source text enters the inquiry or its event.
    assert work["labour_work_description"] == ""
    assert work["labour_recorded_by"] == env.staff.employee_id and work["version"] == 2
    event = work["events"][0]
    assert (event["event_type"], event["work_version"]) == ("AUTODATA_HAKU_HYVAKSYTTY", 2)
    assert set(event["details"]) == EVENT_KEYS
    assert set(event["details"]["autodata"]) == EVENT_AUTODATA_KEYS
    assert event["details"]["autodata"]["decision_id"] == decision["decision_id"]
    assert event["details"]["autodata"]["time_text"] == "0,125 h"
    assert event["details"]["work_description"] == "" and event["details"]["source"] == "AUTODATA"
    history = env.autodata.list_decisions(env.staff, inquiry_id=env.inquiry["id"])
    assert [item["decision_id"] for item in history["items"]] == [decision["decision_id"]]
    assert history["items"][0]["hours"] == "0.125" and history["items"][0]["time_text"] == "0,125 h"
    assert history["items"][0]["labels"]["operation"] == ["Jarrut", "Etujarrupalat, vaihto"]


@pytest.mark.parametrize(("age", "error"), [
    (timedelta(hours=24) - timedelta(microseconds=1), None),
    (timedelta(hours=24), ExpiredLookupError),
    (timedelta(hours=24, microseconds=1), ExpiredLookupError),
    (-timedelta(microseconds=1), InvalidLookupTimeError),
])
def test_acceptance_age_boundary_is_strictly_under_24_hours(env, age, error):
    lookup, _ = store(env)
    env.clock.now = T0 + age
    if error is None:
        assert accept(env, lookup)["decision"]["resulting_work_version"] == 2
        return
    before = counts(env)
    with pytest.raises(error):
        accept(env, lookup)
    assert counts(env) == before
    assert env.work.get_work(env.inquiry["id"], context=env.staff)["version"] == 1


def test_expiry_uses_the_stored_column_not_the_result_provenance(env):
    stale_provenance = fake_result(retrieved=T0 - timedelta(hours=30))
    lookup, _ = store(env, stale_provenance, retrieved=T0)
    assert lookup["result"]["provenance"]["retrieved_at"].startswith("2026-10-21T06:00")
    assert accept(env, lookup)["decision"]["resulting_work_version"] == 2

    fresh_provenance = fake_result(retrieved=T0 + timedelta(hours=1))
    old, _ = store(env, fresh_provenance, version=2, retrieved=T0 - timedelta(hours=25))
    with pytest.raises(ExpiredLookupError):
        accept(env, old, command(version=2))


def test_timestamps_keep_microseconds(env):
    lookup, _ = store(env)
    assert lookup["retrieved_at"] == "2026-10-22T12:00:00.123456+00:00"
    assert lookup["expires_at"] == "2026-10-23T12:00:00.123456+00:00"


def test_clock_is_read_only_after_the_write_lock_is_held(env):
    lookup, _ = store(env)
    observed = []

    def probing_clock():
        other = sqlite3.connect(env.db, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            observed.append("unlocked")
            other.rollback()
        except sqlite3.OperationalError:
            observed.append("locked")
        finally:
            other.close()
        return T0 + timedelta(hours=1)

    env.autodata.clock = probing_clock
    accept(env, lookup)
    assert observed == ["locked"]


def test_lookup_does_not_change_the_inquiry_version(env):
    store(env)
    store(env)
    assert env.work.get_work(env.inquiry["id"], context=env.staff)["version"] == 1


def test_manual_change_after_lookup_makes_it_stale(env):
    lookup, _ = store(env)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour=labour())
    with pytest.raises(StaleLookupError):
        accept(env, lookup)
    with pytest.raises(StaleLookupError):
        accept(env, lookup, command(version=2))


def test_second_lookup_is_stale_after_first_acceptance(env):
    first, _ = store(env)
    second, _ = store(env)
    accept(env, first)
    with pytest.raises(StaleLookupError):
        accept(env, second)


def test_lookup_of_another_inquiry_or_workshop_is_not_found(env):
    other = other_inquiry(env)
    lookup, _ = store(env)
    with pytest.raises(LookupNotFoundError):
        env.autodata.accept_lookup(env.staff, inquiry_id=other["id"], lookup_id=lookup["lookup_id"],
                                   command=command())
    foreign = second_workshop(env)
    with pytest.raises(WorkNotFoundError):
        env.autodata.accept_lookup(foreign, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                                   command=command())
    with pytest.raises(WorkNotFoundError):
        env.autodata.get_lookup(foreign, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"])


@pytest.mark.parametrize("search", ["jarru", "vetokoukku", "levyt"])
def test_only_found_lookups_are_acceptable(env, search):
    lookup, _ = store(env, search=search)
    assert lookup["outcome"] != "FOUND" and lookup["hours"] is None and lookup["time_text"] is None
    with pytest.raises(NotAcceptableError):
        accept(env, lookup)


def test_synthetic_evidence_needs_explicit_test_permission(env):
    lookup, _ = store(env, fake_result(synthetic=True))
    with pytest.raises(SyntheticNotAcceptableError):
        accept(env, lookup)
    assert accept(env, lookup, allow_synthetic=True)["decision"]["resulting_work_version"] == 2


@pytest.mark.parametrize("change", [
    {"confirmation": "VEHICLE"}, {"confirmation": None}, {"confirmation": ""},
    {"confirmation": "vehicle_operation_variant_time"},
    # P6: a closed command; anything else is a validation error (HTTP 422).
    {"hours": "9.9"}, {"expected_result_sha256": "0" * 64},
    {"vehicle_identity": {"selection_path": ["Muu"]}}, {"operation_identity": {"selection_path": ["Muu"]}},
    {"variant_identity": {"selection_path": ["Muu"]}}, {"raw_time_text": "9 h"}, {"lookup_id": str(uuid4())},
])
def test_command_is_closed_and_requires_its_confirmation(change):
    fields = {"request_id": str(uuid4()), "expected_version": 1, "confirmation": CONFIRM} | change
    fields = {key: value for key, value in fields.items() if value is not None}
    with pytest.raises(ValidationError):
        AcceptLookupCommand.model_validate(fields)


def test_invalid_command_writes_nothing(env):
    lookup, _ = store(env)
    before = counts(env)
    with pytest.raises(ValidationError):
        accept(env, lookup, AcceptLookupCommand.model_construct(
            request_id=str(uuid4()), expected_version=1, confirmation="VEHICLE"))
    assert counts(env) == before


def test_retry_returns_the_same_decision_and_version_even_later(env):
    lookup, _ = store(env)
    cmd = command()
    first = accept(env, lookup, cmd)
    after = counts(env)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=2, context=env.staff, labour=labour())
    env.clock.now = T0 + timedelta(hours=25)
    again = accept(env, lookup, cmd)
    assert again["decision"] == first["decision"] and again["replayed"] is True
    assert again["work"]["version"] == 3
    assert counts(env)["autodata_labour_decisions"] == after["autodata_labour_decisions"]


def test_same_key_with_another_valid_inquiry_lookup_or_version_conflicts(env):
    # Codex 3: same user and request_id, valid but different closed fields.
    lookup, _ = store(env)
    other_lookup, _ = store(env)
    same_workshop_inquiry = other_inquiry(env)
    cmd = command()
    accept(env, lookup, cmd)
    with pytest.raises(IdempotencyConflictError):
        accept(env, lookup, command(request_id=cmd.request_id, version=2))
    with pytest.raises(IdempotencyConflictError):
        accept(env, other_lookup, cmd)
    with pytest.raises(IdempotencyConflictError):
        accept(env, lookup, cmd, inquiry_id=same_workshop_inquiry["id"])


def test_scope_is_checked_before_the_receipt(env):
    # Codex 3: an inquiry outside the workshop scope is "not found", never a conflict.
    lookup, _ = store(env)
    cmd = command()
    accept(env, lookup, cmd)
    with pytest.raises(WorkNotFoundError):
        accept(env, lookup, cmd, inquiry_id=str(uuid4()))
    foreign = second_workshop(env)
    foreign_inquiry = env.work.create_work(registration="GHI-789", customer_description="Vieras",
                                           actor="Testikirjaaja", idempotency_key=str(uuid4()),
                                           context=foreign)
    with pytest.raises(WorkNotFoundError):
        accept(env, lookup, cmd, inquiry_id=foreign_inquiry["id"])


def test_receipts_are_per_user(env):
    lookup, _ = store(env)
    cmd = command()
    accept(env, lookup, cmd)
    colleague = test_context("another-test-employee")
    with pytest.raises((AlreadyAcceptedError, StaleLookupError)):
        accept(env, lookup, cmd, context=colleague)


def test_concurrent_acceptances_have_exactly_one_winner(env):
    lookup, _ = store(env)
    outcomes, barrier = [], threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            accept(env, lookup)
            outcomes.append("ok")
        except (AlreadyAcceptedError, StaleLookupError):
            outcomes.append("conflict")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "ok"]
    assert counts(env)["autodata_labour_decisions"] == 1


@pytest.mark.parametrize(("action", "table"), [
    ("UPDATE", "work_inquiries"), ("INSERT", "work_events"), ("INSERT", "autodata_labour_decisions"),
    ("INSERT", "autodata_decision_receipts"),
])
def test_any_failing_write_rolls_back_the_whole_acceptance(env, action, table):
    lookup, _ = store(env)
    inject_failure(env, action, table)
    before, work_before = counts(env), env.work.get_work(env.inquiry["id"], context=env.staff)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        accept(env, lookup)
    assert counts(env) == before
    assert env.work.get_work(env.inquiry["id"], context=env.staff) == work_before


def test_unexpected_pricing_error_rolls_back(env, monkeypatch):
    lookup, _ = store(env)
    monkeypatch.setattr("webapp.labour_snapshot.calculate_labour_cost",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("programming error")))
    before = counts(env)
    with pytest.raises(RuntimeError, match="programming error"):
        accept(env, lookup)
    assert counts(env) == before


def test_reader_and_admin_scopes_cannot_write(env):
    lookup, _ = store(env)
    reader = test_context("trusted-reader", permissions=frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    assert env.autodata.list_lookups(reader, inquiry_id=env.inquiry["id"])["items"]
    with pytest.raises(WorkshopAccessDenied):
        accept(env, lookup, context=reader)
    with pytest.raises(WorkshopAccessDenied):
        store(env, context=reader)
    with pytest.raises(WorkshopAccessDenied):
        redact(env, lookup, context=reader)
    with pytest.raises(WorkshopAccessDenied):
        close(env, context=reader)
    saver = test_context("trusted-reader", permissions=frozenset(
        {WorkshopPermission.READ_LABOUR_PLAN, WorkshopPermission.SAVE_LABOUR_PLAN}))
    with pytest.raises(WorkshopAccessDenied):  # closure requires APPROVE_LABOUR_PLAN (P4)
        close(env, context=saver)
    assert counts(env)["autodata_inquiry_closures"] == 0


# -- receipt trigger bindings (Codex 2) ---------------------------------------

RECEIPT_COLUMNS = ("auth_store_id", "employee_id", "workshop_id", "request_id", "organization_id",
                   "inquiry_id", "lookup_id", "expected_version", "confirmation", "decision_id",
                   "resulting_work_version", "decided_at", "created_at")


def receipt_row(env):
    with sqlite3.connect(env.db) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM autodata_decision_receipts").fetchone())


def insert_receipt(env, row):
    placeholders = ",".join("?" for _ in RECEIPT_COLUMNS)
    sql(env, f"INSERT INTO autodata_decision_receipts ({','.join(RECEIPT_COLUMNS)}) VALUES ({placeholders})",
        tuple(row[column] for column in RECEIPT_COLUMNS))


def test_a_receipt_matching_its_decision_is_accepted_by_the_trigger(env):
    accept(env, store(env)[0])
    insert_receipt(env, receipt_row(env) | {"request_id": str(uuid4())})
    assert counts(env)["autodata_decision_receipts"] == 2


@pytest.mark.parametrize("change", [
    {"employee_id": str(uuid5(NAMESPACE_OID, "another-test-employee"))},
    {"organization_id": str(uuid4())},
    {"confirmation": "VEHICLE"},
    {"decided_at": "2026-10-22T13:00:00.000001+00:00"},
    {"decision_id": str(uuid4())},
    {"lookup_id": str(uuid4())},
    {"inquiry_id": str(uuid4())},
    {"workshop_id": str(uuid4())},
    {"auth_store_id": str(uuid4())},
    {"expected_version": 2, "resulting_work_version": 3},
    {"resulting_work_version": 3},
])
def test_receipt_trigger_rejects_every_wrong_bound_value(env, change):
    accept(env, store(env)[0])
    with pytest.raises(sqlite3.IntegrityError):
        insert_receipt(env, receipt_row(env) | {"request_id": str(uuid4())} | change)
    assert counts(env)["autodata_decision_receipts"] == 1


def test_receipt_survives_without_its_decision_row(env):
    lookup, _ = store(env)
    cmd = command()
    first = accept(env, lookup, cmd)
    close(env)
    assert counts(env)["autodata_labour_decisions"] == 0
    assert receipt_row(env)["decision_id"] == first["decision"]["decision_id"]


# -- lookup storage and request order (Codex 1) -------------------------------

class CountingAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def lookup(self, request):
        self.calls += 1
        return self.result


def lookup_flow(env, adapter, *, request_id, version, search="etujarrupalat", context=None):
    """The 1B-2 service order: stored retry -> new-lookup checks -> adapter -> store."""
    context = context or env.staff
    inquiry_id = env.inquiry["id"]
    sha = lookup_sha(context, inquiry_id, request_id, version, search, None)
    stored = env.autodata.find_stored_lookup(context, inquiry_id=inquiry_id, request_id=request_id,
                                             command_sha256=sha)
    if stored is not None:
        return stored
    preparation = env.autodata.prepare_lookup(context, inquiry_id=inquiry_id, expected_version=version,
                                              search_text=search, vin=None)
    request = LabourTimeRequest(vehicle_query=VehicleQuery(registration=preparation.registration),
                                work_query=WorkQuery(search_text=search))
    result = adapter.lookup(request)
    return env.autodata.store_lookup(context, inquiry_id=inquiry_id, request_id=request_id,
                                     command_sha256=sha, request=request, result=result,
                                     source_work_version=preparation.source_work_version,
                                     requested_at=T0, retrieved_at=T0)


def test_closed_inquiry_answers_closed_even_with_an_old_version_and_no_adapter_call(env):
    adapter = CountingAdapter(fake_result())
    lookup_flow(env, adapter, request_id=str(uuid4()), version=1)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour=labour())
    close(env)
    for version in (1, 2, 99):
        with pytest.raises(AutodataClosedError):
            lookup_flow(env, adapter, request_id=str(uuid4()), version=version)
    assert adapter.calls == 1


def test_request_order_is_authorization_scope_closure_version(env):
    close(env)
    foreign = second_workshop(env)
    with pytest.raises(WorkNotFoundError):  # scope before closure
        env.autodata.prepare_lookup(foreign, inquiry_id=env.inquiry["id"], expected_version=9,
                                    search_text="etujarrupalat", vin=None)
    reader = test_context("trusted-reader", permissions=frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    with pytest.raises(WorkshopAccessDenied):  # authorization before scope and closure
        env.autodata.prepare_lookup(reader, inquiry_id=str(uuid4()), expected_version=9,
                                    search_text="etujarrupalat", vin=None)
    open_inquiry = other_inquiry(env)
    with pytest.raises(StaleViewError):  # version only for an open inquiry
        env.autodata.prepare_lookup(env.staff, inquiry_id=open_inquiry["id"], expected_version=9,
                                    search_text="etujarrupalat", vin=None)


def test_stored_retry_is_recognised_before_the_version_check(env):
    adapter = CountingAdapter(fake_result())
    request_id = str(uuid4())
    first = lookup_flow(env, adapter, request_id=request_id, version=1)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour=labour())
    assert lookup_flow(env, adapter, request_id=request_id, version=1) == first
    assert adapter.calls == 1
    with pytest.raises(StaleViewError):
        lookup_flow(env, adapter, request_id=str(uuid4()), version=1)
    with pytest.raises(StaleLookupError):
        accept(env, first)


def test_redacted_request_retry_is_refused_without_an_adapter_call(env):
    adapter = CountingAdapter(fake_result())
    request_id = str(uuid4())
    first = lookup_flow(env, adapter, request_id=request_id, version=1)
    redact(env, first)
    with pytest.raises(RequestRedactedError):
        lookup_flow(env, adapter, request_id=request_id, version=1)
    assert adapter.calls == 1
    # A new request may look the work up again.
    lookup_flow(env, adapter, request_id=str(uuid4()), version=1)
    assert adapter.calls == 2


def test_stored_lookup_is_recognised_before_any_version_check(env):
    lookup, sha = store(env)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour=labour())
    found = env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                            request_id=lookup["request_id"], command_sha256=sha)
    assert found == lookup
    with pytest.raises(StaleViewError):
        env.autodata.prepare_lookup(env.staff, inquiry_id=env.inquiry["id"], expected_version=1,
                                    search_text="etujarrupalat", vin=None)
    with pytest.raises(StaleLookupError):
        accept(env, lookup)
    with pytest.raises(IdempotencyConflictError):
        env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256="0" * 64)
    assert env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                           request_id=str(uuid4()), command_sha256=sha) is None


def test_lookup_retry_requires_current_rights_and_scope(env):
    lookup, sha = store(env)
    with pytest.raises(WorkNotFoundError):
        env.autodata.find_stored_lookup(second_workshop(env), inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256=sha)
    revoke_membership(env, env.staff)
    with pytest.raises(WorkshopAccessDenied):
        env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256=sha)


def test_storing_the_same_request_again_returns_the_stored_row(env):
    request_id = str(uuid4())
    first, _ = store(env, request_id=request_id)
    again, _ = store(env, request_id=request_id)
    assert again == first and counts(env)["autodata_lookups"] == 1
    with pytest.raises(IdempotencyConflictError):
        store(env, request_id=request_id, search="levyt")


def test_registration_comes_from_the_inquiry_and_inputs_stay_in_the_payload(env):
    with pytest.raises(ValueError, match="registration"):
        store(env, registration="XYZ-999")
    lookup, _ = store(env, vin="WVWZZZ1JZXW000001")
    with sqlite3.connect(env.db) as connection:
        snapshot, = connection.execute("SELECT request_snapshot_json FROM autodata_lookup_payloads "
                                       "WHERE lookup_id = ?", (lookup["lookup_id"],)).fetchone()
        inputs = connection.execute("SELECT registration, vin, search_text FROM autodata_lookup_payloads "
                                    "WHERE lookup_id = ?", (lookup["lookup_id"],)).fetchone()
    assert "ABC" not in snapshot and "WVWZZZ" not in snapshot and "etujarrupalat" not in snapshot
    assert inputs == (REGISTRATION, "WVWZZZ1JZXW000001", "etujarrupalat")


def test_unknown_adapter_is_refused_before_any_write(env):
    data = fake_result().model_dump(mode="json", exclude={"hours"})
    data["provenance"]["adapter_name"] = "browser"
    with pytest.raises(ValueError, match="unknown autodata adapter"):
        store(env, LabourTimeLookupResult.model_validate(data))
    assert counts(env)["autodata_lookups"] == 0


def test_body_time_text_is_normalised_and_the_raw_text_stays_in_the_payload(env):
    odd = FakeOperation(choice=BRAKES.choice,
                        variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,125  H"),))
    vehicle = FakeVehicle(choice=PETROL.choice, operations=(odd,))
    lookup, _ = store(env, fake_result(vehicles=(vehicle,)))
    assert (lookup["time_text"], lookup["raw_time_text"], lookup["hours"]) == ("0,125 h", "0,125  H", "0.125")


def test_a_non_enum_rejection_reason_is_stored_as_site_changed(env):
    unparseable = FakeOperation(choice=BRAKES.choice,
                                variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="1:30"),))
    vehicle = FakeVehicle(choice=PETROL.choice, operations=(unparseable,))
    result = fake_result(vehicles=(vehicle,))
    lookup, _ = store(env, result)
    assert (lookup["outcome"], lookup["rejection_code"]) == ("UNPARSEABLE", "CLOCK_FORMAT")
    data = result.model_dump(mode="json", exclude={"hours"})
    data["rejection_reason"] = "free text from the source"
    other, _ = store(env, LabourTimeLookupResult.model_validate(data))
    assert (other["outcome"], other["rejection_code"], other["result"]["variant"]) == (
        "SITE_CHANGED", None, None)


@pytest.mark.parametrize("search", ["abc 123 jarrut", "jarrut WVWZZZ1JZXW000001"])
def test_identifiers_in_search_text_are_rejected(env, search):
    with pytest.raises(IdentifierInSearchTextError):
        env.autodata.prepare_lookup(env.staff, inquiry_id=env.inquiry["id"], expected_version=1,
                                    search_text=search, vin=None)
    with pytest.raises(IdentifierInSearchTextError):
        store(env, search=search, result=fake_result())
    assert counts(env)["autodata_lookups"] == 0


@pytest.mark.parametrize("search", ["etujarrulevyt ja -palat", "takajarrupalat ja levyt",
                                    "Etujarrulevyt ja -palat, vaihto", "ABC jarrut"])
def test_ordinary_work_names_are_not_mistaken_for_identifiers(env, search):
    preparation = env.autodata.prepare_lookup(env.staff, inquiry_id=env.inquiry["id"], expected_version=1,
                                              search_text=search, vin=None)
    assert preparation.registration == REGISTRATION
    lookup, _ = store(env, search=search, result=fake_result())
    assert lookup["outcome"] == "FOUND"


def test_unpriceable_found_time_raises_and_writes_nothing(env):
    tiny = FakeOperation(choice=choice("Muu", "Pieni aika"),
                         variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,00001 h"),))
    vehicle = FakeVehicle(choice=PETROL.choice, operations=(tiny,))
    lookup, _ = store(env, fake_result("pieni", vehicles=(vehicle,)), search="pieni")
    assert lookup["outcome"] == "FOUND" and lookup["hours"] == "0.00001"
    before = counts(env)
    with pytest.raises(UnpriceableLabourTimeError):
        accept(env, lookup)
    assert counts(env) == before
    assert env.work.get_work(env.inquiry["id"], context=env.staff)["version"] == 1


def test_apply_labour_refuses_to_run_outside_the_callers_transaction(env):
    with env.work._connect() as connection:
        with pytest.raises(RuntimeError, match="caller's transaction"):
            env.work._apply_labour(connection, work_id=env.inquiry["id"], expected_version=1, labour={},
                                   details={}, occurred_at="x", event_type="X")


@pytest.mark.parametrize("table", ["autodata_lookups", "autodata_lookup_payloads"])
def test_a_failing_store_write_stores_nothing(env, table):
    inject_failure(env, "INSERT", table)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store(env)
    assert counts(env)["autodata_lookups"] == counts(env)["autodata_lookup_payloads"] == 0


@pytest.mark.parametrize(("facts", "selections"), [
    ({"make": "ABC-123"}, None),
    ({"model": "Malli abc 123"}, None),
    ({"engine_code": "WVWZZZ1JZXW000001"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "ABC123"))}),
    (None, {"vehicle_selection": ChoiceIdentity(source_id="abc-123", selection_path=("Testimerkki",))}),
])
def test_identifiers_in_vehicle_facts_or_selection_paths_are_rejected(env, facts, selections):
    # Codex P2: an extra guard; since 1B-2.0 these texts would sit in the deletable payload anyway.
    with pytest.raises(IdentifierInRequestError):
        store(env, result=fake_result(), facts=facts, selections=selections)
    assert counts(env)["autodata_lookups"] == 0


def short_registration_inquiry(env):
    return env.work.create_work(registration="AB-1", customer_description="Lyhyt rekisteri",
                                actor="Testikirjaaja", idempotency_key=str(uuid4()), context=env.staff)


@pytest.mark.parametrize(("facts", "selections"), [
    ({"make": "AB-1"}, None),
    ({"model": "Malli ab 1"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "AB1"))}),
    (None, {"vehicle_selection": ChoiceIdentity(source_id="ab-1", selection_path=("Testimerkki",))}),
])
def test_known_short_registration_is_rejected_from_the_snapshot(env, facts, selections):
    inquiry = short_registration_inquiry(env)
    with pytest.raises(IdentifierInRequestError):
        store(env, fake_result(), inquiry_id=inquiry["id"], registration="AB-1", facts=facts,
              selections=selections)
    assert counts(env)["autodata_lookups"] == 0


@pytest.mark.parametrize(("facts", "selections"), [
    ({"make": "LAB1 Motors", "model": "ABS 1 Plus"}, None),
    ({"model": "AB12"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(source_id="SYN-AB-10", selection_path=("Abarth 1",))}),
])
def test_ordinary_values_survive_the_short_registration_rule(env, facts, selections):
    inquiry = short_registration_inquiry(env)
    lookup, _ = store(env, fake_result(), inquiry_id=inquiry["id"], registration="AB-1", facts=facts,
                      selections=selections)
    assert lookup["outcome"] == "FOUND"


def test_source_label_echoing_a_short_registration_is_not_stored(env):
    inquiry = short_registration_inquiry(env)
    leaky = FakeVehicle(choice=choice("Testimerkki", "Tunnistettu ab-1"), operations=(BRAKES,))
    lookup, _ = store(env, fake_result(vehicles=(leaky,)), inquiry_id=inquiry["id"], registration="AB-1")
    assert lookup["outcome"] == "SITE_CHANGED" and lookup["result"]["vehicle"] is None


@pytest.mark.parametrize(("facts", "selections"), [
    ({"engine_code": "WVW ZZZ1JZ XW000001"}, None),
    ({"model": "wvw-zzz1jz-xw000001"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "WVW ZZZ1JZXW 000001"))}),
    # r4: five and more parts.
    ({"engine_code": "WVW ZZZ 1JZ XW 000001"}, None),
    ({"model": "W V W Z Z Z 1 J Z X W 0 0 0 0 0 1"}, None),
    ({"fuel": "Malli WVW-ZZZ-1JZ-XW-000-001 bensiini"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "WVW ZZZ 1JZ XW 000 001"))}),
    (None, {"vehicle_selection": ChoiceIdentity(source_id="WV WZ ZZ 1J ZX W0 00001",
                                                selection_path=("Testimerkki",))}),
    # r5: prefix, suffix and both, inside one token (plan 2.6 contiguous VIN).
    (None, {"vehicle_selection": ChoiceIdentity(source_id="VINWVWZZZ1JZXW000001",
                                                selection_path=("Testimerkki",))}),
    ({"engine_code": "WVWZZZ1JZXW000001FI"}, None),
    ({"model": "MalliWVWZZZ1JZXW000001Testi"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "vinWVWZZZ1JZXW000001"))}),
    (None, {"operation_selection": ChoiceIdentity(selection_path=("Jarrut", "WVWZZZ1JZXW000001x")),
            "vehicle_selection": PETROL.choice.identity}),
    # r5: split VIN with a glued prefix or suffix.
    ({"fuel": "VINWVW ZZZ 1JZ XW 000001FI"}, None),
    ({"make": "malliWVW-ZZZ-1JZ-XW-000001"}, None),
    # r5: contiguous VIN without a numeric ending (no four-digit requirement here).
    ({"engine_code": "WVWZZZ1JZXWABCDEF"}, None),
])
def test_spaced_vin_without_a_vin_field_is_rejected_from_the_snapshot(env, facts, selections):
    with pytest.raises(IdentifierInRequestError):
        store(env, fake_result(), facts=facts, selections=selections)
    assert counts(env)["autodata_lookups"] == 0


@pytest.mark.parametrize("facts", [
    {"model": "1.6 bensiini 2015-2018", "engine_code": "SYN16A", "fuel": "etujarrulevyt ja -palat"},
    # r4: unbounded part runs must still leave ordinary multi-part texts alone.
    {"model": "Testimalli 1 2.0 TDI 110 kW 2015 2018", "fuel": "takajarrupalat ja levyt"},
    {"model": "Etujarrulevyt ja -palat, vaihto 2 kpl 1234"},
    # r5: why the split rule needs a four-digit ending and the contiguous rule a digit.
    {"model": "Etujarrupalat 2 kpl", "fuel": "etujarrupalasarjat"},
])
def test_ordinary_vehicle_texts_are_not_vin_findings(env, facts):
    lookup, _ = store(env, fake_result(), facts=facts)
    assert lookup["outcome"] == "FOUND"


@pytest.mark.parametrize("text", [
    # Digit in the same token, but separated from the 17 letters by I (not a VIN character).
    "1IABCDEFGHJKLMNPRST",
    "ABCDEFGHJKLMNPRSTI1",
    # Digit in another token: neither rule's window contains it.
    "ABCDEFGHJKLMNPRST 1",
    "1 ABCDEFGHJKLMNPRST",
])
def test_a_digit_outside_the_vin_window_does_not_satisfy_the_digit_rule(env, text):
    assert not contains_vin_like(text)
    lookup, _ = store(env, fake_result(), facts={"engine_code": text})
    assert lookup["outcome"] == "FOUND"


def test_the_same_letters_with_a_digit_inside_the_window_are_rejected(env):
    assert contains_vin_like("ABCDEFGHJKLMNPR1T")
    with pytest.raises(IdentifierInRequestError):
        store(env, fake_result(), facts={"engine_code": "ABCDEFGHJKLMNPR1T"})


def test_source_labels_echoing_the_registration_are_not_stored(env):
    leaky = FakeVehicle(choice=choice("Testimerkki", "ABC 123 tunnistettu"), operations=(BRAKES,))
    lookup, _ = store(env, fake_result(vehicles=(leaky,)))
    assert lookup["outcome"] == "SITE_CHANGED"
    assert lookup["result"]["candidates"] == [] and lookup["result"]["vehicle"] is None
    assert "ABC" not in canonical_json(lookup["result"])


def test_oversize_result_is_stored_as_site_changed(env):
    many = tuple(FakeVehicle(choice=choice("Testimerkki", "Malli " + str(index) + " " + "x" * 190),
                             operations=(BRAKES,)) for index in range(400))
    lookup, _ = store(env, fake_result(vehicles=many))
    assert lookup["outcome"] == "SITE_CHANGED" and lookup["result"]["candidates"] == []


# -- copy chain: external text and its digests (plan 7.1, 13.6) ---------------

CANARY = "Kanaria Virtanen"
CANARY_FIELDS = ("vehicle_label", "operation_label", "variant_label", "source_id", "details",
                 "raw_time_text", "make", "search_text", "vin")


def canary_lookup(env, field):
    """Store a FOUND lookup whose only canary sits in one external (S) or staff (A) field."""
    vehicle_path, source_id = ["Testimerkki A", "Testimalli 1"], "SYN-V-1"
    operation_path, details = ["Jarrut", "Etujarrupalat, vaihto"], ()
    variant_label, time_text, search, facts, vin, canary = "Vakio", "0,125 h", "etujarrupalat", None, None, CANARY
    if field == "vehicle_label":
        vehicle_path[1] = CANARY
    elif field == "operation_label":
        operation_path[1] = f"Etujarrupalat {CANARY}"
    elif field == "variant_label":
        variant_label = CANARY
    elif field == "source_id":
        source_id = CANARY
    elif field == "details":
        details = (CANARY,)
    elif field == "raw_time_text":
        time_text = canary = "0,125  H"
    elif field == "make":
        facts = {"make": CANARY}
    elif field == "search_text":
        operation_path[1] = "Etujarrupalat kanariahaku"
        search = canary = "kanariahaku"
    elif field == "vin":
        vin = canary = "WVWZZZ1JZXW999999"
    operation = FakeOperation(choice=choice(*operation_path, details=details), variants=(
        FakeVariant(choice=choice(variant_label), displayed_time_text=time_text),))
    vehicle = FakeVehicle(choice=choice(*vehicle_path, source_id=source_id), operations=(operation,))
    lookup, _ = store(env, fake_result(search, vehicles=(vehicle,)), search=search, facts=facts, vin=vin)
    assert lookup["outcome"] == "FOUND"
    return lookup, canary


def find_everywhere(db, needles, *, skip=()):
    """{(table, column)} whose value contains any needle, case-insensitively, in every table."""
    hits = set()
    needles = [needle.lower() for needle in needles]
    with sqlite3.connect(db) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            if table in skip:
                continue
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for row in connection.execute(f"SELECT * FROM {table}"):
                for column, value in zip(columns, row):
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", "replace")
                    if isinstance(value, str) and any(needle in value.lower() for needle in needles):
                        hits.add((table, column))
    return hits


def payload_digests(env, lookup):
    with sqlite3.connect(env.db) as connection:
        return list(connection.execute(
            "SELECT result_sha256, command_sha256, snapshot_sha256 FROM autodata_lookup_payloads "
            "WHERE lookup_id = ?", (lookup["lookup_id"],)).fetchone())


def strip_payload_views(value):
    """A read model without the fields that are views of the payload."""
    if isinstance(value, dict):
        return {key: strip_payload_views(item) for key, item in value.items()
                if key not in ("labels", "result", "raw_time_text")}
    if isinstance(value, list):
        return [strip_payload_views(item) for item in value]
    return value


def labour_plan_from_inquiry(env, work, *, description=None):
    """A 4B plan whose original time comes ONLY from the inquiry's closed fields (K6 subset)."""
    works = [PlannedWork(work_id="work-0", work_item=WorkItem(
        operation=Operation.BRAKE_REPLACEMENT, axle=Axle.FRONT, side=Side.BOTH))]
    work_plan = WorkPlan(vehicle=Vehicle(registration=REGISTRATION, make="Test", model="Test"), work_items=works)
    originals = (OriginalWorkTimeRecord(
        work_item_id="work-0", operation_identifier=Operation.BRAKE_REPLACEMENT.value, axle="FRONT",
        side="BOTH", original_hours=Decimal(work["labour_hours"]), source="AUTODATA",
        work_description=description, recorded_by=work["labour_recorded_by"],
        autodata_reference=work["autodata_reference"]),)
    return work_plan, originals


def run_labour_plan(env, work, *, description=None):
    service = LabourPlanService(env.work)
    work_plan, originals = labour_plan_from_inquiry(env, work, description=description)
    preview = build_storage_labour_plan(work_plan, originals)
    saved = service.save_plan(env.staff, SaveLabourPlanCommand(
        inquiry_id=work["id"], expected_version=work["version"], expected_content_id=preview.content_id,
        expected_rules_version=preview.review_rules_version, idempotency_key=str(uuid4()),
        work_plan=work_plan, original_times=originals))
    service.approve_plan(env.staff, ApproveLabourPlanCommand(
        inquiry_id=work["id"], expected_version=saved.work_version, expected_content_id=saved.content_id,
        expected_rules_version=saved.rules_version, expected_revision_id=saved.revision_id,
        idempotency_key=str(uuid4()), status="APPROVED_UNCHANGED"))
    return saved


@pytest.mark.parametrize("field", CANARY_FIELDS)
def test_external_text_and_its_digests_never_leave_the_payload(env, field):
    lookup, canary = canary_lookup(env, field)
    payload_only = {("autodata_lookup_payloads", column) for column in (
        "registration", "vin", "search_text", "request_snapshot_json", "result_json", "vehicle_identity_json",
        "operation_identity_json", "variant_identity_json", "raw_time_text")}
    digests = payload_digests(env, lookup) + [sha256(canary.encode("utf-8")).hexdigest()]
    # 1. Storage: the canary is in the payload (proves storage) and nowhere else.
    hits = find_everywhere(env.db, [canary])
    assert hits and hits <= payload_only
    assert find_everywhere(env.db, digests, skip=("autodata_lookup_payloads",)) == set()

    # 2. Acceptance: inquiry, events, decision and receipt carry no canary and no digest.
    accept(env, lookup)
    assert find_everywhere(env.db, [canary]) <= payload_only
    assert find_everywhere(env.db, digests, skip=("autodata_lookup_payloads",)) == set()

    # 3. Reads: payload texts appear only in the payload views of the Autodata reads.
    work = env.work.get_work(env.inquiry["id"], context=env.staff)
    assert canary.lower() not in canonical_json(work).lower()
    assert canary.lower() not in canonical_json(env.work.list_works(context=env.staff)).lower()
    for read in (env.autodata.list_lookups(env.staff, inquiry_id=env.inquiry["id"]),
                 env.autodata.get_lookup(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"]),
                 env.autodata.list_decisions(env.staff, inquiry_id=env.inquiry["id"])):
        assert canary.lower() not in canonical_json(strip_payload_views(read)).lower()

    # 4. The fields that app.js prefills into the manual form carry no source text.
    assert (work["labour_hours"], work["labour_work_description"], work["autodata_reference"]) == (
        "0.125", "", f"lookup:{lookup['lookup_id']}")

    # 5. Bounded 4B path from the inquiry's closed fields only.
    saved = run_labour_plan(env, work)
    assert find_everywhere(env.db, [canary]) <= payload_only
    assert find_everywhere(env.db, digests, skip=("autodata_lookup_payloads",)) == set()
    work_plan, _ = labour_plan_from_inquiry(env, work)
    independent = (OriginalWorkTimeRecord(
        work_item_id="work-0", operation_identifier=Operation.BRAKE_REPLACEMENT.value, axle="FRONT",
        side="BOTH", original_hours=Decimal("0.125"), source="AUTODATA",
        recorded_by=env.staff.employee_id, autodata_reference=f"lookup:{lookup['lookup_id']}"),)
    assert build_storage_labour_plan(work_plan, independent).content_id == saved.content_id

    # 7. Redaction and closure: no logical row anywhere keeps the canary or a digest.
    redact(env, lookup)
    assert find_everywhere(env.db, [canary] + digests) == set()
    close(env)
    assert find_everywhere(env.db, [canary] + digests) == set()


def test_staff_text_typed_into_the_4b_plan_is_outside_this_removal(env):
    # Control (plan 7.1.6): the scan is sensitive; staff-typed text (U) stays in 4B by design.
    lookup, _ = store(env)
    accept(env, lookup)
    work = env.work.get_work(env.inquiry["id"], context=env.staff)
    run_labour_plan(env, work, description=f"Käsin kirjoitettu {CANARY}")
    redact(env, lookup)
    close(env)
    hits = find_everywhere(env.db, [CANARY])
    assert {"labour_plan_revision_items", "labour_plan_revisions", "labour_plan_requests"} <= {
        table for table, _ in hits}
    assert not any(table.startswith("autodata_") for table, _ in hits)


def test_the_digest_search_finds_a_planted_copy(tmp_path):
    # Positive control for the digest search: case-insensitive, nested JSON, non-payload table.
    digest_value = sha256(b"control").hexdigest()
    db = tmp_path / "control.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE work_events (details_json TEXT)")
        connection.execute("INSERT INTO work_events VALUES (?)",
                           (canonical_json({"nested": {"value": digest_value.upper()}}),))
    assert find_everywhere(db, [digest_value]) == {("work_events", "details_json")}


# -- redaction ----------------------------------------------------------------

def test_redaction_deletes_the_payload_and_keeps_body_decision_and_receipt(env):
    lookup, sha = store(env, vin="WVWZZZ1JZXW000001")
    cmd = command()
    first = accept(env, lookup, cmd)
    before = counts(env)
    outcome = redact(env, lookup, reason_code="DATA_SUBJECT_REQUEST")
    assert set(outcome) == {"redaction_id", "lookup_id", "redacted_at"}
    after = counts(env)
    assert after["autodata_lookup_payloads"] == before["autodata_lookup_payloads"] - 1
    assert after["autodata_redaction_events"] == 1
    for table in ("autodata_lookups", "autodata_labour_decisions", "autodata_decision_receipts", "work_events"):
        assert after[table] == before[table]
    stored = env.autodata.get_lookup(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"])
    assert stored["redacted"] is True and stored["result"] is None and stored["raw_time_text"] is None
    assert (stored["hours"], stored["time_text"], stored["outcome"]) == ("0.125", "0,125 h", "FOUND")
    decision = env.autodata.list_decisions(env.staff, inquiry_id=env.inquiry["id"])["items"][0]
    assert decision["labels"] is None and decision["redacted"] is True and decision["hours"] == "0.125"
    # P1 retained view and the retry guarantee.
    assert accept(env, lookup, cmd)["decision"] == first["decision"]
    with pytest.raises(RequestRedactedError):
        env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256=sha)
    with pytest.raises(AlreadyRedactedError):
        redact(env, lookup)


def test_new_acceptance_of_a_redacted_lookup_is_refused(env):
    lookup, _ = store(env)
    redact(env, lookup)
    before = counts(env)
    with pytest.raises(LookupRedactedError):
        accept(env, lookup)
    assert counts(env) == before
    status = env.autodata.list_lookups(env.staff, inquiry_id=env.inquiry["id"])["items"][0]
    assert (status["acceptability"], status["redacted"], status["labels"]) == ("REDACTED", True, None)


def test_redaction_requires_a_reason_code_scope_and_an_open_inquiry(env):
    lookup, _ = store(env)
    with pytest.raises(ValueError, match="reason"):
        redact(env, lookup, reason_code="Keksitty vapaa teksti")
    with pytest.raises(WorkNotFoundError):
        redact(env, lookup, context=second_workshop(env))
    with pytest.raises(LookupNotFoundError):
        redact(env, lookup, inquiry_id=other_inquiry(env)["id"])
    close(env)
    with pytest.raises(AutodataClosedError):
        redact(env, lookup)


def test_payload_cannot_be_deleted_or_regained_without_the_procedures(env):
    lookup, _ = store(env)
    with pytest.raises(sqlite3.IntegrityError, match="logged redaction or a closure"):
        sql(env, "DELETE FROM autodata_lookup_payloads")
    with pytest.raises(sqlite3.IntegrityError, match="never updated"):
        sql(env, "UPDATE autodata_lookup_payloads SET search_text = 'muu'")
    with sqlite3.connect(env.db) as connection:
        payload = connection.execute("SELECT * FROM autodata_lookup_payloads").fetchone()
    redact(env, lookup)
    with pytest.raises(sqlite3.IntegrityError, match="cannot regain"):
        sql(env, f"INSERT INTO autodata_lookup_payloads VALUES ({','.join('?' * len(payload))})", payload)


def test_redaction_event_requires_the_payload(env):
    lookup, _ = store(env)
    redact(env, lookup)
    with pytest.raises(sqlite3.IntegrityError, match="requires the payload"):
        sql(env, "INSERT INTO autodata_redaction_events VALUES (?,?,?,?,?,?,?,?,?)", (
            str(uuid4()), lookup["lookup_id"], TEST_ORGANIZATION, env.inquiry["id"], env.staff.workshop_id,
            env.staff.employee_id, "{}", "2026-10-22T14:00:00.000000+00:00", "RETENTION"))


@pytest.mark.parametrize(("action", "table"), [("INSERT", "autodata_redaction_events"),
                                               ("DELETE", "autodata_lookup_payloads")])
def test_a_failing_redaction_step_rolls_back(env, action, table):
    lookup, _ = store(env)
    inject_failure(env, action, table)
    before = counts(env)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        redact(env, lookup)
    assert counts(env) == before


# -- closure ------------------------------------------------------------------

def test_closure_deletes_atomically_keeps_receipts_and_blocks_later_evidence(env):
    open_lookup, _ = store(env)
    accepted, _ = store(env)
    cmd = command()
    first = accept(env, accepted, cmd)
    redact(env, open_lookup)
    outcome = close(env, reason_code="DATA_SUBJECT_REQUEST")
    assert outcome["row_counts"] == {
        "autodata_labour_decisions": 1, "autodata_redaction_events": 1, "autodata_lookup_payloads": 1,
        "autodata_lookups": 2, "autodata_decision_receipts": 1}
    left = counts(env)
    assert (left["autodata_lookups"], left["autodata_lookup_payloads"], left["autodata_labour_decisions"],
            left["autodata_redaction_events"]) == (0, 0, 0, 0)
    assert (left["autodata_decision_receipts"], left["autodata_inquiry_closures"]) == (1, 1)
    # P7: the successful acceptance still replays after closure.
    assert accept(env, accepted, cmd)["decision"] == first["decision"]
    with pytest.raises(AutodataClosedError):
        accept(env, accepted)
    with pytest.raises(AutodataClosedError):
        store(env, version=2)
    with pytest.raises(AutodataClosedError):
        env.autodata.prepare_lookup(env.staff, inquiry_id=env.inquiry["id"], expected_version=2,
                                    search_text="etujarrupalat", vin=None)
    with pytest.raises(AutodataClosedError):
        close(env)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sql(env, "DELETE FROM autodata_inquiry_closures")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sql(env, "UPDATE autodata_inquiry_closures SET reason_code = 'RETENTION'")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        sql(env, "DELETE FROM autodata_decision_receipts")


def test_closure_requires_confirmation_reason_code_and_scope(env):
    with pytest.raises(ValueError, match="confirmation"):
        close(env, confirmation="CLOSE")
    with pytest.raises(ValueError, match="reason"):
        close(env, reason_code="Keksitty vapaa teksti")
    with pytest.raises(WorkNotFoundError):
        close(env, context=second_workshop(env))
    assert counts(env)["autodata_inquiry_closures"] == 0


def test_closure_during_the_adapter_call_stores_nothing(env):
    request_id = str(uuid4())
    result = fake_result()
    close(env)
    with pytest.raises(AutodataClosedError):
        store(env, result, request_id=request_id)
    assert counts(env)["autodata_lookups"] == 0


@pytest.mark.parametrize(("action", "table"), [
    ("INSERT", "autodata_inquiry_closures"), ("DELETE", "autodata_labour_decisions"),
    ("DELETE", "autodata_redaction_events"), ("DELETE", "autodata_lookup_payloads"),
    ("DELETE", "autodata_lookups"),
])
def test_a_failing_closure_step_rolls_back(env, action, table):
    first, _ = store(env)
    accept(env, store(env)[0])
    redact(env, first)
    inject_failure(env, action, table)
    before = counts(env)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        close(env)
    assert counts(env) == before


# -- residual checks (Codex P3) -------------------------------------------------

def keep_rows_on_delete(env, table):
    """A test-database trigger that silently skips every DELETE on the table."""
    sql(env, f"CREATE TRIGGER keep_rows BEFORE DELETE ON {table} BEGIN SELECT RAISE(IGNORE); END")


def test_redaction_residual_check_rolls_back_everything(env):
    lookup, _ = store(env)
    accept(env, lookup)
    keep_rows_on_delete(env, "autodata_lookup_payloads")
    before, work_before = all_rows(env.db), env.work.get_work(env.inquiry["id"], context=env.staff)
    with pytest.raises(AutodataIntegrityError):
        redact(env, lookup)
    # No redaction event, no partial deletion: every row as it was.
    assert all_rows(env.db) == before
    assert counts(env)["autodata_redaction_events"] == 0
    assert env.work.get_work(env.inquiry["id"], context=env.staff) == work_before


@pytest.mark.parametrize("table", ["autodata_labour_decisions", "autodata_redaction_events",
                                   "autodata_lookup_payloads", "autodata_lookups"])
def test_closure_residual_check_rolls_back_everything(env, table):
    redacted, _ = store(env)
    accept(env, store(env)[0])
    redact(env, redacted)
    keep_rows_on_delete(env, table)
    before, work_before = all_rows(env.db), env.work.get_work(env.inquiry["id"], context=env.staff)
    assert all(before[name] for name in ("autodata_labour_decisions", "autodata_redaction_events",
                                          "autodata_lookup_payloads", "autodata_lookups",
                                          "autodata_decision_receipts"))
    with pytest.raises(AutodataIntegrityError):
        close(env)
    # No closure marker and no partial deletion in any table.
    assert all_rows(env.db) == before
    assert counts(env)["autodata_inquiry_closures"] == 0
    assert env.work.get_work(env.inquiry["id"], context=env.staff) == work_before


@pytest.mark.parametrize("table", ["autodata_lookups", "autodata_lookup_payloads",
                                   "autodata_labour_decisions", "autodata_redaction_events"])
def test_evidence_cannot_be_deleted_without_closure(env, table):
    lookup, _ = store(env)
    accept(env, store(env)[0])
    redact(env, lookup)
    with pytest.raises(sqlite3.IntegrityError, match="closure"):
        sql(env, f"DELETE FROM {table}")


@pytest.mark.parametrize("statement", [
    "UPDATE autodata_lookups SET hours = '9'",
    "UPDATE autodata_lookups SET time_text = '9 h'",
    "UPDATE autodata_lookup_payloads SET registration = 'MUU-1'",
    "UPDATE autodata_labour_decisions SET hours = '9'",
    "UPDATE autodata_decision_receipts SET expected_version = 5",
    "DELETE FROM autodata_decision_receipts",
])
def test_evidence_is_immutable(env, statement):
    accept(env, store(env)[0])
    with pytest.raises(sqlite3.IntegrityError):
        sql(env, statement)


# -- races (Codex 4 and plan 7.5) ---------------------------------------------

def test_acceptance_then_closure_keeps_a_replayable_receipt_behind_current_rights(env):
    lookup, _ = store(env)
    cmd = command()
    first = accept(env, lookup, cmd)
    close(env)
    assert counts(env)["autodata_decision_receipts"] == 1
    assert accept(env, lookup, cmd)["decision"] == first["decision"]
    reader = test_context("trusted-reader", permissions=frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    with pytest.raises(WorkshopAccessDenied):
        accept(env, lookup, cmd, context=reader)
    with pytest.raises(WorkNotFoundError):
        accept(env, lookup, cmd, context=second_workshop(env))
    revoke_membership(env, env.staff)
    with pytest.raises(WorkshopAccessDenied):
        accept(env, lookup, cmd)


def test_closure_then_acceptance_creates_nothing(env):
    lookup, _ = store(env)
    close(env)
    before, work_before = counts(env), env.work.get_work(env.inquiry["id"], context=env.staff)
    with pytest.raises(AutodataClosedError):
        accept(env, lookup)
    assert counts(env) == before
    work = env.work.get_work(env.inquiry["id"], context=env.staff)
    assert work == work_before and work["version"] == 1 and work["labour_hours"] is None


def run_together(*actions):
    outcomes, barrier = {}, threading.Barrier(len(actions))

    def run(name, action):
        barrier.wait()
        try:
            outcomes[name] = action()
        except Exception as error:  # noqa: BLE001 - the race test inspects every outcome.
            outcomes[name] = error

    threads = [threading.Thread(target=run, args=item) for item in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_concurrent_acceptance_and_closure_keep_the_invariants(env):
    lookup, _ = store(env)
    cmd = command()
    outcomes = run_together(("accept", lambda: accept(env, lookup, cmd)), ("close", lambda: close(env)))
    assert isinstance(outcomes["close"], dict)
    left, work = counts(env), env.work.get_work(env.inquiry["id"], context=env.staff)
    assert left["autodata_lookups"] == left["autodata_labour_decisions"] == 0
    if isinstance(outcomes["accept"], dict):
        assert left["autodata_decision_receipts"] == 1 and work["version"] == 2
        assert accept(env, lookup, cmd)["decision"] == outcomes["accept"]["decision"]
    else:
        assert isinstance(outcomes["accept"], AutodataClosedError)
        assert left["autodata_decision_receipts"] == 0 and work["version"] == 1


def test_concurrent_redaction_and_acceptance_keep_the_invariants(env):
    lookup, _ = store(env)
    outcomes = run_together(("accept", lambda: accept(env, lookup)), ("redact", lambda: redact(env, lookup)))
    assert isinstance(outcomes["redact"], dict)
    left = counts(env)
    assert left["autodata_lookup_payloads"] == 0 and left["autodata_redaction_events"] == 1
    if isinstance(outcomes["accept"], dict):
        assert left["autodata_labour_decisions"] == 1
    else:
        assert isinstance(outcomes["accept"], LookupRedactedError) and left["autodata_labour_decisions"] == 0


def test_concurrent_redaction_and_closure_keep_the_invariants(env):
    lookup, _ = store(env)
    outcomes = run_together(("redact", lambda: redact(env, lookup)), ("close", lambda: close(env)))
    assert isinstance(outcomes["close"], dict)
    assert isinstance(outcomes["redact"], (dict, AutodataClosedError))
    left = counts(env)
    assert (left["autodata_lookups"], left["autodata_lookup_payloads"], left["autodata_redaction_events"]) == (
        0, 0, 0)


def test_concurrent_closures_close_once(env):
    store(env)
    outcomes = run_together(("one", lambda: close(env)), ("two", lambda: close(env)))
    assert sorted(type(value).__name__ for value in outcomes.values()) == ["AutodataClosedError", "dict"]
    assert counts(env)["autodata_inquiry_closures"] == 1


# -- history and paging -------------------------------------------------------

def test_lookup_listing_pages_without_overlap_and_reports_acceptability(env):
    accepted, _ = store(env, retrieved=T0)
    for _ in range(3):
        store(env, retrieved=T0)  # Same timestamp: the lookup ID breaks ties.
    store(env, search="jarru", retrieved=T0 - timedelta(hours=1))
    store(env, fake_result(synthetic=True), retrieved=T0 - timedelta(hours=2))
    expired, _ = store(env, retrieved=T0 - timedelta(hours=30))
    accept(env, accepted)

    seen, cursor = [], None
    while True:
        page = env.autodata.list_lookups(env.staff, inquiry_id=env.inquiry["id"], limit=2, before=cursor)
        seen += page["items"]
        cursor = page["next"]
        if cursor is None:
            break
    assert len(seen) == 7 and len({item["lookup_id"] for item in seen}) == 7
    status = {item["lookup_id"]: item["acceptability"] for item in seen}
    assert status[accepted["lookup_id"]] == "ACCEPTED"
    assert status[expired["lookup_id"]] == "STALE_LOOKUP"  # version moved before age matters
    assert sorted(status.values()) == sorted(
        ["ACCEPTED", "STALE_LOOKUP", "STALE_LOOKUP", "STALE_LOOKUP", "NOT_FOUND_OUTCOME", "SYNTHETIC",
         "STALE_LOOKUP"])
    assert all("candidates" not in item for item in seen)
    with pytest.raises(ValueError):
        env.autodata.list_lookups(env.staff, inquiry_id=env.inquiry["id"], limit=21)


def test_listing_marks_acceptable_and_expired_lookups(env):
    fresh, _ = store(env, retrieved=T0)
    old, _ = store(env, retrieved=T0 - timedelta(hours=24))
    status = {item["lookup_id"]: item["acceptability"]
              for item in env.autodata.list_lookups(env.staff, inquiry_id=env.inquiry["id"])["items"]}
    assert status == {fresh["lookup_id"]: "ACCEPTABLE", old["lookup_id"]: "EXPIRED"}


def test_missing_payload_without_redaction_is_an_integrity_error(env, monkeypatch):
    lookup, sha = store(env)
    monkeypatch.setattr(AutodataRepository, "_payload", staticmethod(lambda connection, lookup_id: None))
    before = counts(env)
    with pytest.raises(AutodataIntegrityError):
        accept(env, lookup)
    with pytest.raises(AutodataIntegrityError):
        env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256=sha)
    assert counts(env) == before


# -- schema guard -------------------------------------------------------------

# Every TEXT column of the v2 Autodata tables: PAYLOAD (deletable open text or
# digest), CLOSED (grammar or value set checked in the schema), SCOPE
# (server identifiers), STAFF (staff attribution, plan K5).
COLUMN_CLASSES = {
    "autodata_lookups": {
        "lookup_id": "CLOSED", "organization_id": "SCOPE", "workshop_id": "SCOPE", "inquiry_id": "SCOPE",
        "auth_store_id": "SCOPE", "requested_by": "SCOPE", "requested_by_attribution_json": "STAFF",
        "request_id": "CLOSED", "registration_source": "CLOSED", "requested_at": "CLOSED",
        "retrieved_at": "CLOSED", "outcome": "CLOSED", "level": "CLOSED", "hours": "CLOSED",
        "time_text": "CLOSED", "rejection_code": "CLOSED", "adapter_name": "CLOSED", "adapter_version": "CLOSED",
    },
    "autodata_lookup_payloads": {
        "lookup_id": "SCOPE", "inquiry_id": "SCOPE", "workshop_id": "SCOPE", "registration": "PAYLOAD",
        "vin": "PAYLOAD", "search_text": "PAYLOAD", "request_snapshot_json": "PAYLOAD", "result_json": "PAYLOAD",
        "vehicle_identity_json": "PAYLOAD", "operation_identity_json": "PAYLOAD",
        "variant_identity_json": "PAYLOAD", "raw_time_text": "PAYLOAD", "result_sha256": "PAYLOAD",
        "command_sha256": "PAYLOAD", "snapshot_sha256": "PAYLOAD",
    },
    "autodata_labour_decisions": {
        "decision_id": "CLOSED", "lookup_id": "SCOPE", "organization_id": "SCOPE", "workshop_id": "SCOPE",
        "inquiry_id": "SCOPE", "decision": "CLOSED", "hours": "CLOSED", "time_text": "CLOSED",
        "confirmation": "CLOSED", "lookup_retrieved_at": "CLOSED", "decided_at": "CLOSED",
        "auth_store_id": "SCOPE", "decided_by": "SCOPE", "decided_by_attribution_json": "STAFF",
        "labour_snapshot_json": "CLOSED",
    },
    "autodata_decision_receipts": {
        "auth_store_id": "SCOPE", "employee_id": "SCOPE", "workshop_id": "SCOPE", "request_id": "CLOSED",
        "organization_id": "SCOPE", "inquiry_id": "SCOPE", "lookup_id": "CLOSED", "confirmation": "CLOSED",
        "decision_id": "CLOSED", "decided_at": "CLOSED", "created_at": "CLOSED",
    },
    "autodata_redaction_events": {
        "redaction_id": "CLOSED", "lookup_id": "SCOPE", "organization_id": "SCOPE", "inquiry_id": "SCOPE",
        "workshop_id": "SCOPE", "redacted_by": "SCOPE", "redacted_by_attribution_json": "STAFF",
        "redacted_at": "CLOSED", "reason_code": "CLOSED",
    },
    "autodata_inquiry_closures": {
        "inquiry_id": "SCOPE", "organization_id": "SCOPE", "workshop_id": "SCOPE", "closed_by": "SCOPE",
        "closed_by_attribution_json": "STAFF", "closed_at": "CLOSED", "reason_code": "CLOSED",
        "row_counts_json": "CLOSED",
    },
}


def test_every_autodata_text_column_is_classified(env):
    with sqlite3.connect(env.db) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'autodata\\_%' ESCAPE '\\' "
            "AND name <> 'autodata_schema_migrations'")}
        assert tables == set(COLUMN_CLASSES)
        for table in tables:
            text_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                            if row[2].upper() == "TEXT"}
            assert text_columns == set(COLUMN_CLASSES[table]), table
    # Receipts hold no open text at all.
    assert "PAYLOAD" not in COLUMN_CLASSES["autodata_decision_receipts"].values()
    assert "STAFF" not in COLUMN_CLASSES["autodata_decision_receipts"].values()


def check_table(expression):
    connection = sqlite3.connect(":memory:")
    connection.execute(f"CREATE TABLE t (x TEXT, y TEXT, CHECK ({expression}))")
    return connection


@pytest.mark.parametrize(("text", "valid"), [
    ("0,125 h", True), ("1 h", True), ("1.5 hours", True), ("2 tuntia", True), ("10 tunti", True),
    ("tunti 1", False), ("1 hhh", False), ("1  h", False), ("1 H", False), ("1,2,3 h", False),
    ("1. h", False), (",5 h", False), ("1h", False), ("1 h ", False), (" 1 h", False), ("-1 h", False),
    ("1,5 min", False), ("", False),
])
def test_body_time_text_grammar(text, valid):
    connection = check_table(_time_text("x"))
    if valid:
        connection.execute("INSERT INTO t (x) VALUES (?)", (text,))
    else:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO t (x) VALUES (?)", (text,))


@pytest.mark.parametrize(("name", "version", "valid"), [
    ("fake", "fake-1", True), ("synthetic-page", "synthetic-1", True), ("fake", "synthetic-1", False),
    ("browser", "1", False), ("Fake", "fake-1", False),
])
def test_adapter_vocabulary_is_closed(name, version, valid):
    connection = check_table(_adapter("x", "y"))
    if valid:
        connection.execute("INSERT INTO t VALUES (?, ?)", (name, version))
    else:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO t VALUES (?, ?)", (name, version))


def _drop(key):
    return lambda snapshot: {name: value for name, value in snapshot.items() if name != key}


def _set(key, value):
    return lambda snapshot: snapshot | {key: value}


SNAPSHOT_CASES = {
    # Positive control: the server-built snapshot itself.
    "valid": None,
    # A required key is missing.
    "missing_work_description": _drop("work_description"),
    "missing_autodata_reference": _drop("autodata_reference"),
    "missing_currency": _drop("currency"),
    "missing_total_price": _drop("total_price"),
    # A required value is JSON null.
    "null_work_description": _set("work_description", None),
    "null_autodata_reference": _set("autodata_reference", None),
    "null_vat_price": _set("vat_price", None),
    # A wrong type.
    "number_hours": _set("hours", 0.125),
    "number_total_price": _set("total_price", 11.25),
    "array_work_description": _set("work_description", []),
    "object_currency": _set("currency", {"code": "EUR"}),
    "boolean_source": _set("source", True),
    # An extra free-text field.
    "extra_free_text": _set("operation_label", "Jarrut / Etujarrupalat, vaihto"),
    "extra_null": _set("note", None),
    # A wrong agreed value.
    "nonempty_description": _set("work_description", "Jarrut / Etujarrupalat"),
    "blank_description": _set("work_description", " "),
    "other_reference": _set("autodata_reference", "lookup:x"),
    "manual_source": _set("source", "MANUAL_AUTODATA"),
    "other_hours": _set("hours", "9"),
    "other_recorder": _set("recorded_by", "someone-else"),
    # Not a canonical JSON object.
    "array_document": lambda snapshot: [snapshot],
    "json_null_document": lambda snapshot: None,
}


@pytest.mark.parametrize("case", list(SNAPSHOT_CASES))
def test_decision_snapshot_is_a_closed_object(env, case):
    # Codex P2: a NULL CHECK result passes in SQLite, so every rule must be NULL-safe.
    lookup, _ = store(env)
    accept(env, lookup)
    with sqlite3.connect(env.db) as connection:
        create, = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'autodata_labour_decisions'"
                                     ).fetchone()
        row = list(connection.execute("SELECT * FROM autodata_labour_decisions").fetchone())
        columns = [item[1] for item in connection.execute("PRAGMA table_info(autodata_labour_decisions)")]
    index = columns.index("labour_snapshot_json")
    assert set(json.loads(row[index])) == set(SNAPSHOT_KEYS)
    if SNAPSHOT_CASES[case] is not None:
        row[index] = canonical_json(SNAPSHOT_CASES[case](json.loads(row[index])))
    # The table's own CHECK constraints, without its triggers and references: a
    # rejection can only come from a constraint, and the name proves which one.
    isolated = sqlite3.connect(":memory:")
    isolated.execute(create)
    insert = f"INSERT INTO autodata_labour_decisions VALUES ({','.join('?' * len(row))})"
    if case == "valid":
        isolated.execute(insert, row)
        assert isolated.execute("SELECT count(*) FROM autodata_labour_decisions").fetchone() == (1,)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: decision_snapshot_closed"):
            isolated.execute(insert, row)


@pytest.mark.parametrize("text", ["{", "", "{}", '{"work_description":""}'])
def test_decision_snapshot_rejects_malformed_or_partial_json(env, text):
    lookup, _ = store(env)
    accept(env, lookup)
    with sqlite3.connect(env.db) as connection:
        create, = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'autodata_labour_decisions'"
                                     ).fetchone()
        row = list(connection.execute("SELECT * FROM autodata_labour_decisions").fetchone())
        columns = [item[1] for item in connection.execute("PRAGMA table_info(autodata_labour_decisions)")]
    row[columns.index("labour_snapshot_json")] = text
    isolated = sqlite3.connect(":memory:")
    isolated.execute(create)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: decision_snapshot_closed"):
        isolated.execute(f"INSERT INTO autodata_labour_decisions VALUES ({','.join('?' * len(row))})", row)


def test_migration_literals_match_the_current_contracts():
    assert set(TIME_REJECTIONS) == {reason.value for reason in TimeRejection}
    assert set(TIME_UNITS) == set(HOUR_UNITS)
    assert set(OUTCOMES) == {outcome.value for outcome in LookupOutcome}
    assert set(ADAPTERS) == {("fake", FAKE_ADAPTER_VERSION), ("synthetic-page", SYNTHETIC_ADAPTER_VERSION)}
    snapshot = build_labour_snapshot(LabourTime(hours=Decimal("0.125"), source=LabourTimeSource.AUTODATA))
    assert SNAPSHOT_KEYS == tuple(sorted(snapshot))


# -- migration ----------------------------------------------------------------

def ledger(db):
    with sqlite3.connect(db) as connection:
        return {row[0] for row in connection.execute("SELECT version FROM autodata_schema_migrations")}


def schema_dump(db):
    with sqlite3.connect(db) as connection:
        return sorted(tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"))


def all_rows(db):
    with sqlite3.connect(db) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(map(tuple, connection.execute(f"SELECT * FROM {table}"))) for table in tables}


def test_new_database_applies_both_versions(env):
    assert ledger(env.db) == {1, 2}
    env.work.initialize()
    assert ledger(env.db) == {1, 2}


def test_existing_database_gains_autodata_tables_without_data_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("webapp.database.apply_autodata_migrations", lambda connection: None)
    work = ScopedTestRepository(tmp_path / "old.db")
    work.initialize()
    staff = test_context("test-employee-stable-id")
    inquiry = work.create_work(registration="OLD-1", customer_description="Vanha", actor="Testikirjaaja",
                               idempotency_key=str(uuid4()), context=staff)
    monkeypatch.undo()
    work.initialize()
    work.initialize()
    assert work.get_work(inquiry["id"], context=staff) == inquiry
    assert ledger(tmp_path / "old.db") == {1, 2}
    with sqlite3.connect(tmp_path / "old.db") as connection:
        connection.execute("INSERT INTO autodata_schema_migrations VALUES (3, 'x')")
    with pytest.raises(RuntimeError, match="unknown autodata schema version"):
        work.initialize()


@pytest.fixture
def v1_database(tmp_path, monkeypatch):
    """A synthetic test database stopped at autodata migration 1."""
    monkeypatch.setattr(autodata_migrations, "MIGRATIONS", autodata_migrations.MIGRATIONS[:1])
    work = ScopedTestRepository(tmp_path / "v1.db")
    work.initialize()
    staff = test_context("test-employee-stable-id")
    inquiry = work.create_work(registration="OLD-1", customer_description="Vanha", actor="Testikirjaaja",
                               idempotency_key=str(uuid4()), context=staff)
    assert ledger(tmp_path / "v1.db") == {1}
    monkeypatch.undo()
    return work, staff, inquiry, tmp_path / "v1.db"


def insert_v1_lookup(db, staff, inquiry_id):
    lookup_id = str(uuid4())
    moment = "2026-10-22T12:00:00.000000+00:00"
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO autodata_lookups (lookup_id, organization_id, workshop_id, inquiry_id, auth_store_id, "
            "requested_by, requested_by_attribution_json, request_id, command_sha256, snapshot_json, "
            "snapshot_sha256, registration_source, source_work_version, requested_at, retrieved_at, outcome, "
            "result_json, result_sha256, adapter_name, adapter_version, synthetic) "
            "VALUES (?,?,?,?,?,?,'{}',?,?,'{}',?, 'INQUIRY', 1, ?, ?, 'WORK_NOT_FOUND', '{}', ?, 'fake', 'fake-1', 1)",
            (lookup_id, staff.organization_id, staff.workshop_id, inquiry_id, staff.auth_store_id,
             staff.employee_id, str(uuid4()), "a" * 64, "b" * 64, moment, moment, "c" * 64))
        connection.execute("INSERT INTO autodata_lookup_vehicle_inputs (lookup_id, inquiry_id, workshop_id, "
                           "registration, vin, search_text) VALUES (?,?,?, 'OLD-1', NULL, 'jarrut')",
                           (lookup_id, inquiry_id, staff.workshop_id))


@pytest.mark.parametrize("evidence", ["lookup", "event_only"])
def test_migration_2_stops_without_changes_on_version_1_evidence(v1_database, evidence):
    work, staff, inquiry, db = v1_database
    if evidence == "lookup":
        insert_v1_lookup(db, staff, inquiry["id"])
    else:
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO work_events (work_id, occurred_at, event_type, actor, details_json, "
                               "work_version) VALUES (?, 'x', 'AUTODATA_HAKU_HYVAKSYTTY', 'x', '{}', 2)",
                               (inquiry["id"],))
    schema, rows = schema_dump(db), all_rows(db)
    with pytest.raises(RuntimeError, match="migration 2 stopped"):
        work.initialize()
    assert schema_dump(db) == schema and all_rows(db) == rows and ledger(db) == {1}


def test_empty_version_1_database_migrates_to_version_2(v1_database):
    work, staff, inquiry, db = v1_database
    work.initialize()
    assert ledger(db) == {1, 2}
    assert work.get_work(inquiry["id"], context=staff) == inquiry
    with sqlite3.connect(db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "autodata_lookup_payloads" in tables and "autodata_lookup_vehicle_inputs" not in tables


def test_a_failure_inside_migration_2_rolls_everything_back(v1_database, monkeypatch):
    work, staff, inquiry, db = v1_database
    version, statements = autodata_migrations.MIGRATIONS[1]
    broken = statements[:3] + ("INSERT INTO autodata_no_such_table VALUES (1)",) + statements[3:]
    monkeypatch.setattr(autodata_migrations, "MIGRATIONS",
                        (autodata_migrations.MIGRATIONS[0], (version, broken)))
    schema, rows = schema_dump(db), all_rows(db)
    with pytest.raises(sqlite3.OperationalError):
        work.initialize()
    assert schema_dump(db) == schema and all_rows(db) == rows and ledger(db) == {1}


# -- helpers -------------------------------------------------------------------

def second_workshop(env):
    workshop = str(uuid5(NAMESPACE_OID, "carbit.tests.second-workshop"))
    employee = str(uuid5(NAMESPACE_OID, "second-workshop-employee"))
    store_id = env.staff.auth_store_id
    with sqlite3.connect(env.db) as connection:
        connection.execute("INSERT OR IGNORE INTO workshops VALUES (?,?,'Fictional second workshop','ACTIVE')",
                           (workshop, TEST_ORGANIZATION))
        connection.execute("INSERT OR IGNORE INTO workshop_principals VALUES (?,?)", (store_id, employee))
        connection.execute("INSERT OR IGNORE INTO workshop_memberships VALUES (?,?,?,1,1)",
                           (store_id, employee, workshop))
    return TrustedWorkshopContext(employee, frozenset(WorkshopPermission), auth_store_id=store_id,
                                  organization_id=TEST_ORGANIZATION, workshop_id=workshop,
                                  membership_version=1)
