"""Autodata 1B-1: stored lookups, acceptance, history and schema guards.

Repository level only (no HTTP, no adapter calls). Fake adapter output is used
as lookup evidence; tests that need non-synthetic evidence relabel it.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_OID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from integrations.autodata.fake_adapter import (
    FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import (
    Choice, ChoiceIdentity, LabourTimeLookupResult, LabourTimeRequest, LookupOutcome,
    VehicleQuery, WorkQuery,
)
from webapp.autodata_records import (
    LOOKUP_CONTRACT, AcceptLookupCommand, canonical_json, command_digest, contains_vin_like,
)
from webapp.autodata_repository import (
    AlreadyAcceptedError, AutodataClosedError, AutodataRepository, ExpiredLookupError,
    IdentifierInRequestError, IdentifierInSearchTextError, InvalidLookupTimeError,
    LookupNotFoundError, NotAcceptableError,
    SelectionMismatchError, StaleLookupError, StaleViewError, SyntheticNotAcceptableError,
)
from webapp.database import IdempotencyConflictError, WorkNotFoundError
from webapp.labour_snapshot import UnpriceableLabourTimeError
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, WorkshopPermission
from workshop_support import TEST_ORGANIZATION, ScopedTestRepository, test_context


T0 = datetime(2026, 10, 22, 12, 0, 0, 123456, tzinfo=timezone.utc)
REGISTRATION = "ABC-123"


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


def store(env, result=None, *, context=None, inquiry_id=None, version=1, request_id=None,
          retrieved=T0, search="etujarrupalat", vin=None, registration=REGISTRATION,
          facts=None, selections=None):
    context = context or env.staff
    inquiry_id = inquiry_id or env.inquiry["id"]
    request_id = request_id or str(uuid4())
    result = result or fake_result(search, retrieved=retrieved)
    body = {"request_id": request_id, "expected_version": version, "work_query": search, "vin": vin}
    sha = command_digest(LOOKUP_CONTRACT, actor_user_id=context.employee_id,
                         inquiry_id=inquiry_id, body=body)
    request = LabourTimeRequest(
        vehicle_query=VehicleQuery(registration=registration, vin=vin, **(facts or {})),
        work_query=WorkQuery(search_text=search), **(selections or {}),
    )
    return env.autodata.store_lookup(
        context, inquiry_id=inquiry_id, request_id=request_id, command_sha256=sha, request=request,
        result=result, source_work_version=version, requested_at=retrieved, retrieved_at=retrieved,
    ), sha


def command(lookup, *, version=1, request_id=None, **overrides):
    result = lookup["result"]
    fields = {
        "request_id": request_id or str(uuid4()), "expected_version": version,
        "expected_result_sha256": lookup["result_sha256"],
        "vehicle_identity": result["vehicle"]["choice"]["identity"],
        "operation_identity": result["operation"]["choice"]["identity"],
        "variant_identity": result["variant"]["choice"]["identity"],
        "confirmation": "VEHICLE_OPERATION_VARIANT_TIME",
    }
    return AcceptLookupCommand.model_validate(fields | overrides)


def accept(env, lookup, cmd=None, *, context=None, **kwargs):
    return env.autodata.accept_lookup(context or env.staff, inquiry_id=env.inquiry["id"],
                                      lookup_id=lookup["lookup_id"], command=cmd or command(lookup), **kwargs)


def counts(env):
    with sqlite3.connect(env.db) as connection:
        tables = ("autodata_lookups", "autodata_lookup_vehicle_inputs", "autodata_labour_decisions",
                  "autodata_decision_receipts", "work_events")
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def sql(env, statement, parameters=()):
    with sqlite3.connect(env.db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(statement, parameters)


# -- acceptance ---------------------------------------------------------------

def test_acceptance_writes_decision_labour_event_version_and_receipt(env):
    lookup, _ = store(env)
    assert lookup["outcome"] == "FOUND" and lookup["hours"] == "0.125"
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
    assert work["labour_work_description"] == "Jarrut / Etujarrupalat, vaihto / Vakio"
    assert work["labour_recorded_by"] == env.staff.employee_id and work["version"] == 2
    event = work["events"][0]
    assert (event["event_type"], event["work_version"]) == ("AUTODATA_HAKU_HYVAKSYTTY", 2)
    assert event["details"]["autodata"]["decision_id"] == decision["decision_id"]
    assert event["details"]["autodata"]["raw_time_text"] == "0,125 h"
    assert event["details"]["source"] == "AUTODATA"
    history = env.autodata.list_decisions(env.staff, inquiry_id=env.inquiry["id"])
    assert [item["decision_id"] for item in history["items"]] == [decision["decision_id"]]
    assert history["items"][0]["hours"] == "0.125"


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
        accept(env, old, command(old, version=2))


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
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour={
        "hours": "1.60", "source": "MANUAL_AUTODATA", "work_description": "Keksitty", "recorded_by": "x",
        "autodata_reference": "", "hourly_rate": "90.00", "total_price": "144.00", "net_price": "114.74",
        "vat_price": "29.26", "vat_rate": "0.255", "currency": "EUR"})
    with pytest.raises(StaleLookupError):
        accept(env, lookup)
    with pytest.raises(StaleLookupError):
        accept(env, lookup, command(lookup, version=2))


def test_second_lookup_is_stale_after_first_acceptance(env):
    first, _ = store(env)
    second, _ = store(env)
    accept(env, first)
    with pytest.raises(StaleLookupError):
        accept(env, second)


@pytest.mark.parametrize("field", ["vehicle_identity", "operation_identity", "variant_identity",
                                   "expected_result_sha256"])
def test_every_bound_selection_must_match(env, field):
    lookup, _ = store(env)
    replacement = "0" * 64 if field == "expected_result_sha256" else {"selection_path": ["Muu"]}
    before = counts(env)
    with pytest.raises(SelectionMismatchError):
        accept(env, lookup, command(lookup, **{field: replacement}))
    assert counts(env) == before


def test_lookup_of_another_inquiry_or_workshop_is_not_found(env):
    other = env.work.create_work(registration="DEF-456", customer_description="Toinen",
                                 actor="Testikirjaaja", idempotency_key=str(uuid4()), context=env.staff)
    lookup, _ = store(env)
    with pytest.raises(LookupNotFoundError):
        env.autodata.accept_lookup(env.staff, inquiry_id=other["id"], lookup_id=lookup["lookup_id"],
                                   command=command(lookup))
    foreign = second_workshop(env)
    with pytest.raises(WorkNotFoundError):
        env.autodata.accept_lookup(foreign, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                                   command=command(lookup))
    with pytest.raises(WorkNotFoundError):
        env.autodata.get_lookup(foreign, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"])


@pytest.mark.parametrize("search", ["jarru", "vetokoukku", "levyt"])
def test_only_found_lookups_are_acceptable(env, search):
    lookup, _ = store(env, search=search)
    assert lookup["outcome"] != "FOUND" and lookup["hours"] is None
    identity = {"selection_path": ["X"]}
    cmd = AcceptLookupCommand(request_id=str(uuid4()), expected_version=1,
                              expected_result_sha256=lookup["result_sha256"], vehicle_identity=identity,
                              operation_identity=identity, variant_identity=identity,
                              confirmation="VEHICLE_OPERATION_VARIANT_TIME")
    with pytest.raises(NotAcceptableError):
        accept(env, lookup, cmd)


def test_synthetic_evidence_needs_explicit_test_permission(env):
    lookup, _ = store(env, fake_result(synthetic=True))
    with pytest.raises(SyntheticNotAcceptableError):
        accept(env, lookup)
    assert accept(env, lookup, allow_synthetic=True)["decision"]["resulting_work_version"] == 2


@pytest.mark.parametrize("change", [{"confirmation": "VEHICLE"}, {"confirmation": None}, {"hours": "9.9"}])
def test_confirmation_is_required_and_hours_are_never_accepted(env, change):
    lookup, _ = store(env)
    fields = command(lookup).model_dump(mode="json")
    fields.update(change)
    fields = {key: value for key, value in fields.items() if value is not None}
    with pytest.raises(ValidationError):
        AcceptLookupCommand.model_validate(fields)


def test_retry_returns_the_same_decision_and_version_even_later(env):
    lookup, _ = store(env)
    cmd = command(lookup)
    first = accept(env, lookup, cmd)
    after = counts(env)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=2, context=env.staff, labour={
        "hours": "1.60", "source": "MANUAL_AUTODATA", "work_description": "Keksitty", "recorded_by": "x",
        "autodata_reference": "", "hourly_rate": "90.00", "total_price": "144.00", "net_price": "114.74",
        "vat_price": "29.26", "vat_rate": "0.255", "currency": "EUR"})
    env.clock.now = T0 + timedelta(hours=25)
    again = accept(env, lookup, cmd)
    assert again["decision"] == first["decision"] and again["replayed"] is True
    assert again["work"]["version"] == 3
    assert counts(env)["autodata_labour_decisions"] == after["autodata_labour_decisions"]


def test_reused_request_id_with_other_content_or_lookup_conflicts(env):
    lookup, _ = store(env)
    other, _ = store(env)
    cmd = command(lookup)
    accept(env, lookup, cmd)
    with pytest.raises(IdempotencyConflictError):
        accept(env, lookup, command(lookup, request_id=cmd.request_id, expected_version=2))
    with pytest.raises(IdempotencyConflictError):
        env.autodata.accept_lookup(env.staff, inquiry_id=env.inquiry["id"], lookup_id=other["lookup_id"],
                                   command=cmd)


def test_receipts_are_per_user(env):
    lookup, _ = store(env)
    cmd = command(lookup)
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


@pytest.mark.parametrize("table", ["work_inquiries", "work_events", "autodata_labour_decisions",
                                   "autodata_decision_receipts"])
def test_any_failing_write_rolls_back_the_whole_acceptance(env, table):
    lookup, _ = store(env)
    action = "UPDATE" if table == "work_inquiries" else "INSERT"
    sql(env, f"CREATE TRIGGER inject_failure BEFORE {action} ON {table} "
             "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
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


# -- lookup storage -----------------------------------------------------------

def test_stored_lookup_is_recognised_before_any_version_check(env):
    lookup, sha = store(env)
    env.work.record_labour(work_id=env.inquiry["id"], expected_version=1, context=env.staff, labour={
        "hours": "1.60", "source": "MANUAL_AUTODATA", "work_description": "Keksitty", "recorded_by": "x",
        "autodata_reference": "", "hourly_rate": "90.00", "total_price": "144.00", "net_price": "114.74",
        "vat_price": "29.26", "vat_rate": "0.255", "currency": "EUR"})
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


def test_storing_the_same_request_again_returns_the_stored_row(env):
    request_id = str(uuid4())
    first, _ = store(env, request_id=request_id)
    again, _ = store(env, request_id=request_id)
    assert again == first and counts(env)["autodata_lookups"] == 1
    with pytest.raises(IdempotencyConflictError):
        store(env, request_id=request_id, search="levyt")


def test_registration_comes_from_the_inquiry_and_inputs_stay_out_of_json(env):
    with pytest.raises(ValueError, match="registration"):
        store(env, registration="XYZ-999")
    lookup, _ = store(env, vin="WVWZZZ1JZXW000001")
    with sqlite3.connect(env.db) as connection:
        snapshot, = connection.execute("SELECT snapshot_json FROM autodata_lookups WHERE lookup_id = ?",
                                       (lookup["lookup_id"],)).fetchone()
        inputs = connection.execute("SELECT registration, vin, search_text FROM autodata_lookup_vehicle_inputs "
                                    "WHERE lookup_id = ?", (lookup["lookup_id"],)).fetchone()
    assert "ABC" not in snapshot and "WVWZZZ" not in snapshot and "etujarrupalat" not in snapshot
    assert inputs == (REGISTRATION, "WVWZZZ1JZXW000001", "etujarrupalat")


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


@pytest.mark.parametrize(("facts", "selections"), [
    ({"make": "ABC-123"}, None),
    ({"model": "Malli abc 123"}, None),
    ({"engine_code": "WVWZZZ1JZXW000001"}, None),
    (None, {"vehicle_selection": ChoiceIdentity(selection_path=("Testimerkki", "ABC123"))}),
    (None, {"vehicle_selection": ChoiceIdentity(source_id="abc-123", selection_path=("Testimerkki",))}),
])
def test_identifiers_in_vehicle_facts_or_selection_paths_are_rejected(env, facts, selections):
    # Codex P2: these texts would enter immutable snapshot_json, beyond redaction.
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


def test_no_identifier_remains_in_immutable_evidence_after_redaction(env):
    vin = "WVWZZZ1JZXW000001"
    selection = {"vehicle_selection": PETROL.choice.identity}
    lookup, _ = store(env, fake_result(**selection), vin=vin, selections=selection,
                      facts={"make": "Testimerkki A", "model_year": 2016})
    accept(env, lookup)
    redact(env, lookup)
    with sqlite3.connect(env.db) as connection:
        evidence = "".join(
            canonical_json([list(row) for row in connection.execute(f"SELECT * FROM {table}")])
            for table in ("autodata_lookups", "autodata_lookup_vehicle_inputs", "autodata_labour_decisions",
                          "autodata_decision_receipts", "autodata_redaction_events"))
    compact = evidence.upper().replace("-", "").replace(" ", "")
    assert "ABC123" not in compact and vin not in compact
    assert "etujarrupalat" not in evidence  # search text was redacted too


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


# -- closure, redaction and immutability --------------------------------------

def close_inquiry(env, inquiry_id=None):
    inquiry_id = inquiry_id or env.inquiry["id"]
    with sqlite3.connect(env.db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO autodata_inquiry_closures VALUES (?,?,?,?,?,?,?)",
            (inquiry_id, env.staff.workshop_id, str(uuid4()), "{}", "2026-10-22T13:00:00.000000+00:00",
             "Keksitty sulkeminen", "{}"),
        )
        for table in ("autodata_decision_receipts", "autodata_labour_decisions", "autodata_redaction_events",
                      "autodata_lookup_vehicle_inputs", "autodata_lookups"):
            connection.execute(f"DELETE FROM {table} WHERE inquiry_id = ?", (inquiry_id,))
        connection.commit()


def test_closure_deletes_atomically_and_blocks_all_later_evidence(env):
    lookup, _ = store(env)
    accept(env, store(env)[0])
    close_inquiry(env)
    assert {table: count for table, count in counts(env).items() if table != "work_events"} == {
        "autodata_lookups": 0, "autodata_lookup_vehicle_inputs": 0,
        "autodata_labour_decisions": 0, "autodata_decision_receipts": 0}
    with pytest.raises(AutodataClosedError):
        accept(env, lookup)
    with pytest.raises(AutodataClosedError):
        store(env, version=2)
    with pytest.raises(AutodataClosedError):
        env.autodata.prepare_lookup(env.staff, inquiry_id=env.inquiry["id"], expected_version=2,
                                    search_text="etujarrupalat", vin=None)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sql(env, "DELETE FROM autodata_inquiry_closures")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sql(env, "UPDATE autodata_inquiry_closures SET reason = 'muu'")


def test_closure_during_the_adapter_call_stores_nothing(env):
    request_id = str(uuid4())
    result = fake_result()
    close_inquiry(env)
    with pytest.raises(AutodataClosedError):
        store(env, result, request_id=request_id)
    assert counts(env)["autodata_lookups"] == 0


@pytest.mark.parametrize("table", ["autodata_lookups", "autodata_lookup_vehicle_inputs",
                                   "autodata_labour_decisions", "autodata_decision_receipts"])
def test_evidence_cannot_be_deleted_without_closure(env, table):
    accept(env, store(env)[0])
    with pytest.raises(sqlite3.IntegrityError, match="closure"):
        sql(env, f"DELETE FROM {table}")


@pytest.mark.parametrize("statement", [
    "UPDATE autodata_lookups SET hours = '9'",
    "UPDATE autodata_lookups SET command_sha256 = NULL, snapshot_sha256 = NULL",
    "UPDATE autodata_labour_decisions SET hours = '9'",
    "UPDATE autodata_decision_receipts SET response_json = '{}'",
    "UPDATE autodata_lookup_vehicle_inputs SET registration = 'MUU-1'",
])
def test_evidence_is_immutable_except_logged_redaction(env, statement):
    accept(env, store(env)[0])
    with pytest.raises(sqlite3.IntegrityError):
        sql(env, statement)


def redact(env, lookup, *, redacted_by="by-employee", reason="'Keksitty'"):
    redacted_by = env.staff.employee_id if redacted_by == "by-employee" else redacted_by
    with sqlite3.connect(env.db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO autodata_redaction_events VALUES (?,?,?,?,?,?,?,?)", (
            str(uuid4()), lookup["lookup_id"], env.inquiry["id"], env.staff.workshop_id,
            env.staff.employee_id, "{}", "2026-10-22T14:00:00.000000+00:00", "Keksitty redaktointi"))
        connection.execute(
            "UPDATE autodata_lookup_vehicle_inputs SET registration = NULL, vin = NULL, search_text = NULL, "
            f"redacted_at = '2026-10-22T14:00:00.000000+00:00', redacted_by = ?, redaction_reason = {reason} "
            "WHERE lookup_id = ?", (redacted_by, lookup["lookup_id"]))
        connection.execute("UPDATE autodata_lookups SET command_sha256 = NULL, snapshot_sha256 = NULL "
                           "WHERE lookup_id = ?", (lookup["lookup_id"],))
        connection.commit()


@pytest.mark.parametrize(("redacted_by", "reason"), [(None, "'Keksitty'"), ("by-employee", "NULL"),
                                                     (None, "NULL"), ("  ", "'Keksitty'"),
                                                     ("by-employee", "''"), ("by-employee", "'   '")])
def test_redaction_requires_its_actor_and_reason(env, redacted_by, reason):
    # Codex P3: SQLite treats a NULL CHECK result as passing; NULLs must be explicit failures.
    lookup, _ = store(env)
    with pytest.raises(sqlite3.IntegrityError):
        redact(env, lookup, redacted_by=redacted_by, reason=reason)
    with sqlite3.connect(env.db) as connection:
        assert connection.execute("SELECT registration FROM autodata_lookup_vehicle_inputs").fetchone() == (
            REGISTRATION,)


def test_logged_redaction_removes_inputs_and_digests_but_keeps_acceptance_evidence(env):
    lookup, sha = store(env, vin="WVWZZZ1JZXW000001")
    accept(env, lookup)
    redact(env, lookup)
    with pytest.raises(IdempotencyConflictError):
        env.autodata.find_stored_lookup(env.staff, inquiry_id=env.inquiry["id"],
                                        request_id=lookup["request_id"], command_sha256=sha)
    decision = env.autodata.list_decisions(env.staff, inquiry_id=env.inquiry["id"])["items"][0]
    assert decision["hours"] == "0.125" and decision["operation_identity"]["selection_path"][1] == (
        "Etujarrupalat, vaihto")


def test_inputs_cannot_be_redacted_without_a_logged_event(env):
    lookup, _ = store(env)
    with pytest.raises(sqlite3.IntegrityError):
        sql(env, "UPDATE autodata_lookup_vehicle_inputs SET registration = NULL, vin = NULL, search_text = NULL, "
                 "redacted_at = '2026-10-22T14:00:00.000000+00:00', redacted_by = 'x', redaction_reason = 'y'")


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


# -- migration ----------------------------------------------------------------

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
    with sqlite3.connect(tmp_path / "old.db") as connection:
        assert connection.execute("SELECT version FROM autodata_schema_migrations").fetchall() == [(1,)]
        connection.execute("INSERT INTO autodata_schema_migrations VALUES (2, 'x')")
    with pytest.raises(RuntimeError, match="unknown autodata schema version"):
        work.initialize()


# -- helpers -------------------------------------------------------------------

def second_workshop(env):
    workshop = str(uuid5(NAMESPACE_OID, "carbit.tests.second-workshop"))
    employee = str(uuid5(NAMESPACE_OID, "second-workshop-employee"))
    store_id = env.staff.auth_store_id
    with sqlite3.connect(env.db) as connection:
        connection.execute("INSERT INTO workshops VALUES (?,?,'Fictional second workshop','ACTIVE')",
                           (workshop, TEST_ORGANIZATION))
        connection.execute("INSERT INTO workshop_principals VALUES (?,?)", (store_id, employee))
        connection.execute("INSERT INTO workshop_memberships VALUES (?,?,?,1,1)", (store_id, employee, workshop))
    return TrustedWorkshopContext(employee, frozenset(WorkshopPermission), auth_store_id=store_id,
                                  organization_id=TEST_ORGANIZATION, workshop_id=workshop,
                                  membership_version=1)
