"""Autodata 1B-2 services and settings: lookup order, lock, outcomes and acceptance delegation.

No application lifecycle is started and no route is called. Importing
http_auth_support imports webapp.main, whose module-level create_app() builds
the FastAPI object; that opens no database. Every database is a new synthetic
test database under pytest's temporary directory; the adapter is the real fake.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid4, uuid5

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from http_auth_support import grant_session
from integrations.autodata.adapter import AutodataLabourAdapter
from integrations.autodata.fake_adapter import (
    FAKE_ADAPTER_VERSION, FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import Choice, ChoiceIdentity, LookupOutcome
from integrations.autodata.runner import PROCESS_LOOKUP_LOCK, AdapterIdentity, LookupRunner
from integrations.autodata.synthetic_reader import SYNTHETIC_ADAPTER_VERSION, SyntheticPageAdapter
from webapp.auth_crypto import TotpCipher
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService, AuthenticationError
from webapp.autodata_migrations import CLOSE_CONFIRMATION
from webapp.autodata_records import AcceptLookupCommand, LookupCommand
from webapp.autodata_repository import (
    AutodataClosedError, AutodataRepository, IdentifierInRequestError, IdentifierInSearchTextError,
    RequestRedactedError, StaleViewError, SyntheticNotAcceptableError,
)
from webapp.autodata_service import (
    AutodataAcceptanceService, AutodataDisabledError, AutodataLookupService, LookupBusyError,
)
from webapp.autodata_settings import AutodataConfigurationError, AutodataMode, AutodataSettings
from webapp.database import IdempotencyConflictError, WorkNotFoundError
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context
from webapp.workshop_repository import WorkshopDirectory
from workshop_support import TEST_ORGANIZATION, TEST_WORKSHOP, ScopedTestRepository, test_context


T0 = datetime(2026, 10, 24, 9, 0, 0, 250000, tzinfo=timezone.utc)
REGISTRATION = "ABC-123"
CONFIRM = "VEHICLE_OPERATION_VARIANT_TIME"
FAKE = AdapterIdentity("fake", FAKE_ADAPTER_VERSION, True)
ENABLED = AutodataSettings(mode=AutodataMode.FAKE)
ALLOW_SYNTHETIC = AutodataSettings(mode=AutodataMode.FAKE, allow_synthetic_acceptance=True)
DISABLED = AutodataSettings()
SECRET = "Keksitty sivuteksti https://example.invalid/?evaste=salainen"
TABLES = ("autodata_lookups", "autodata_lookup_payloads", "autodata_labour_decisions",
          "autodata_decision_receipts", "autodata_redaction_events", "autodata_inquiry_closures",
          "work_events")


def choice(*path):
    return Choice(identity=ChoiceIdentity(selection_path=path))


VEHICLE = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1"), operations=(
    FakeOperation(choice=choice("Jarrut", "Etujarrupalat, vaihto"),
                  variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,8 h"),)),
))


class Clock:
    def __init__(self):
        self.now = T0
        self.ticks = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.ticks


def counting_fake(clock):
    """The real fake (the service refuses a subclass); its clock, read once inside
    every lookup, counts the calls and runs an optional action inside the call."""
    def adapter_clock():
        adapter.calls += 1
        if adapter.during is not None:
            adapter.during(adapter.requests[-1])
        return clock()

    adapter = FakeAutodataLabourAdapter((VEHICLE,), clock=adapter_clock)
    adapter.calls, adapter.during = 0, None
    return adapter


def build(work, clock, staff, *, settings=ENABLED):
    repository = AutodataRepository(work, clock=clock)
    adapter = counting_fake(clock)
    # No lock is given: every service uses the one process lock.
    runner = LookupRunner(adapter, FAKE, clock=clock, monotonic=clock.monotonic)
    return SimpleNamespace(repository=repository, adapter=adapter, lock=PROCESS_LOOKUP_LOCK, runner=runner,
                           service=AutodataLookupService(repository, settings, runner),
                           acceptance=AutodataAcceptanceService(repository, settings))


@pytest.fixture
def env(tmp_path):
    work = ScopedTestRepository(tmp_path / "autodata.db")
    work.initialize()
    clock = Clock()
    staff = test_context("test-employee-stable-id")
    inquiry = work.create_work(registration=REGISTRATION, customer_description="Keksitty palvelutesti",
                               actor="Testikirjaaja", idempotency_key=str(uuid4()), context=staff)
    return SimpleNamespace(work=work, clock=clock, staff=staff, inquiry=inquiry, db=tmp_path / "autodata.db",
                           **vars(build(work, clock, staff)))


def lookup_command(*, request_id=None, version=1, search="etujarrupalat", facts=None, **fields):
    return LookupCommand.model_validate({
        "request_id": request_id or str(uuid4()), "expected_version": version,
        "vehicle_facts": facts or {}, "work_query": search, **fields,
    })


def accept_command(*, request_id=None, version=1):
    return AcceptLookupCommand(request_id=request_id or str(uuid4()), expected_version=version,
                               confirmation=CONFIRM)


def run_lookup(env, command=None, *, context=None, inquiry_id=None, service=None):
    return (service or env.service).lookup(context or env.staff, inquiry_id=inquiry_id or env.inquiry["id"],
                                           command=command or lookup_command())


def counts(env):
    with sqlite3.connect(env.db) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


def sql(env, statement, parameters=()):
    with sqlite3.connect(env.db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(statement, parameters)


def database_text(path):
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


def close(env, context=None):
    env.repository.close_autodata(context or env.staff, inquiry_id=env.inquiry["id"], reason_code="RETENTION",
                                  confirmation=CLOSE_CONFIRMATION)


def revoke_membership(env, context):
    sql(env, "UPDATE workshop_memberships SET enabled = 0, version = version + 1 "
             "WHERE auth_store_id = ? AND user_id = ?", (context.auth_store_id, context.employee_id))


def assert_lock_free(lock):
    assert lock.acquire(blocking=False)
    lock.release()


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


def spy(monkeypatch, repository, names=("find_stored_lookup", "prepare_lookup", "store_lookup",
                                         "accept_lookup")):
    calls = []
    for name in names:
        original = getattr(repository, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(repository, name, wrapper)
    return calls


def same_moment(record):
    stored = datetime.fromisoformat(record["retrieved_at"])
    assert stored == datetime.fromisoformat(record["result"]["provenance"]["retrieved_at"])
    assert datetime.fromisoformat(record["requested_at"]) <= stored
    return stored


# -- settings -----------------------------------------------------------------

def test_settings_default_to_disabled_without_synthetic_acceptance():
    settings = AutodataSettings()
    assert (settings.mode, settings.enabled, settings.allow_synthetic_acceptance, settings.workers) == (
        AutodataMode.DISABLED, False, False, 1)
    assert ENABLED.enabled and not ENABLED.allow_synthetic_acceptance
    assert ALLOW_SYNTHETIC.allow_synthetic_acceptance


@pytest.mark.parametrize("value", [None, "disabled"])
def test_environment_selects_only_the_disabled_mode(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CARBIT_AUTODATA_MODE", raising=False)
    else:
        monkeypatch.setenv("CARBIT_AUTODATA_MODE", value)
    assert AutodataSettings.from_environment() == AutodataSettings()


@pytest.mark.parametrize("value", ["fake", "browser", "FAKE", "Disabled", "", "1", "synthetic"])
def test_environment_rejects_every_enabled_or_unknown_mode(monkeypatch, value):
    monkeypatch.setenv("CARBIT_AUTODATA_MODE", value)
    with pytest.raises(AutodataConfigurationError):
        AutodataSettings.from_environment()


@pytest.mark.parametrize("kwargs", [
    {"mode": "fake"},
    {"mode": AutodataMode.BROWSER},
    {"mode": AutodataMode.FAKE, "workers": 2},
    {"mode": AutodataMode.FAKE, "workers": True},
    {"workers": 0},
    {"workers": 1.0},
    {"mode": AutodataMode.FAKE, "allow_synthetic_acceptance": 1},
    {"allow_synthetic_acceptance": True},
])
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(AutodataConfigurationError):
        AutodataSettings(**kwargs)


# -- disabled (MAIN decision 24.10.2026: replays only) ---------------------------

def stored_and_accepted(env):
    """A lookup and its acceptance receipt, both made while the fake was enabled."""
    lookup_cmd, accept_cmd = lookup_command(), accept_command()
    lookup = run_lookup(env, lookup_cmd)["lookup"]
    decision = AutodataAcceptanceService(env.repository, ALLOW_SYNTHETIC).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"], command=accept_cmd)
    return lookup_cmd, lookup, accept_cmd, decision


def disabled(env, runner=None):
    return (AutodataLookupService(env.repository, DISABLED, runner),
            AutodataAcceptanceService(env.repository, DISABLED))


def unchanged(env):
    """Every row of the database, to prove that nothing was written."""
    return counts(env), database_text(env.db)


@pytest.mark.parametrize("with_runner", [False, True])
def test_disabled_replays_a_stored_lookup_without_the_adapter_or_writes(env, monkeypatch, with_runner):
    lookup_cmd, lookup, _, _ = stored_and_accepted(env)
    lookups, _ = disabled(env, env.runner if with_runner else None)
    before = unchanged(env)
    calls = spy(monkeypatch, env.repository)
    again = run_lookup(env, lookup_command(request_id=lookup_cmd.request_id.upper()), service=lookups)
    assert again == {"lookup": lookup, "replayed": True}
    assert calls == ["find_stored_lookup"] and env.adapter.calls == 1 and unchanged(env) == before
    assert_lock_free(env.lock)


def test_disabled_replays_an_acceptance_receipt_without_writes_after_redaction_and_closure(env):
    lookup_cmd, lookup, accept_cmd, decision = stored_and_accepted(env)
    lookups, acceptance = disabled(env)

    def replay():
        before = unchanged(env)
        again = acceptance.accept(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                                  command=accept_cmd)
        assert again["replayed"] is True and again["decision"] == decision["decision"]
        assert unchanged(env) == before

    replay()
    env.repository.redact_lookup(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                                 reason_code="ENTERED_IN_ERROR")
    replay()
    # The redacted lookup request itself keeps its contract: refused, never looked up again.
    with pytest.raises(RequestRedactedError):
        run_lookup(env, lookup_cmd, service=lookups)
    close(env)
    replay()
    assert env.adapter.calls == 1


def test_disabled_refuses_a_new_lookup_and_a_new_acceptance_without_writes(env, monkeypatch):
    # A stored, acceptable-looking synthetic lookup: the refusal is AUTODATA_DISABLED, not SYNTHETIC.
    lookup = run_lookup(env)["lookup"]
    lookups, acceptance = disabled(env, env.runner)
    before = unchanged(env)
    calls = spy(monkeypatch, env.repository)
    with pytest.raises(AutodataDisabledError) as raised:
        run_lookup(env, service=lookups)
    assert raised.value.code == "AUTODATA_DISABLED"
    with pytest.raises(AutodataDisabledError) as raised:
        acceptance.accept(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                          command=accept_command())
    assert raised.value.code == "AUTODATA_DISABLED"
    # One read-only retry check, then one acceptance transaction; no prepare, store or adapter call.
    assert calls == ["find_stored_lookup", "accept_lookup"]
    assert env.adapter.calls == 1 and unchanged(env) == before
    assert_lock_free(env.lock)


def test_disabled_acceptance_is_one_replay_only_repository_call(env, monkeypatch):
    # The receipt check and the refusal of a new write share one transaction: no service-side check.
    received, sentinel = [], object()
    monkeypatch.setattr(env.repository, "accept_lookup",
                        lambda *args, **kwargs: received.append((args, kwargs)) or sentinel)
    command, lookup_id = accept_command(), str(uuid4())
    result = AutodataAcceptanceService(env.repository, DISABLED).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup_id, command=command)
    assert result is sentinel
    assert received == [((env.staff,), {"inquiry_id": env.inquiry["id"], "lookup_id": lookup_id,
                                        "command": command, "replay_only": True})]


def test_disabled_replays_require_current_rights_and_workshop_scope(env):
    lookup_cmd, lookup, accept_cmd, _ = stored_and_accepted(env)
    lookups, acceptance = disabled(env)

    def both(error, context, inquiry_id=None):
        # Called directly: run_lookup would replace a missing context with the staff member.
        with pytest.raises(error):
            lookups.lookup(context, inquiry_id=inquiry_id or env.inquiry["id"], command=lookup_cmd)
        with pytest.raises(error):
            acceptance.accept(context, inquiry_id=inquiry_id or env.inquiry["id"], lookup_id=lookup["lookup_id"],
                              command=accept_cmd)

    foreign = second_workshop(env)
    foreign_inquiry = env.work.create_work(registration="GHI-789", customer_description="Vieras",
                                           actor="Testikirjaaja", idempotency_key=str(uuid4()), context=foreign)
    before = unchanged(env)
    reader = test_context("trusted-reader", permissions=frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    both(PermissionError, reader)
    both(PermissionError, None)
    # Another workshop's member, and another workshop's inquiry, see "not found".
    both(WorkNotFoundError, foreign)
    both(WorkNotFoundError, env.staff, foreign_inquiry["id"])
    revoke_membership(env, env.staff)
    after_revoke = unchanged(env)
    both(WorkshopAccessDenied, env.staff)
    assert unchanged(env) == after_revoke and after_revoke[0] == before[0]
    assert env.adapter.calls == 1


def test_disabled_replays_keep_the_same_key_conflicts(env):
    lookup_cmd, lookup, accept_cmd, _ = stored_and_accepted(env)
    lookups, acceptance = disabled(env)
    before = unchanged(env)
    with pytest.raises(IdempotencyConflictError):
        run_lookup(env, lookup_command(request_id=lookup_cmd.request_id, search="etujarrulevyt"), service=lookups)
    for changed in ({"command": accept_command(request_id=accept_cmd.request_id, version=2)},
                    {"lookup_id": str(uuid4())}):
        fields = {"inquiry_id": env.inquiry["id"], "lookup_id": lookup["lookup_id"], "command": accept_cmd}
        with pytest.raises(IdempotencyConflictError):
            acceptance.accept(env.staff, **(fields | changed))
    assert unchanged(env) == before and env.adapter.calls == 1


# -- new lookup ---------------------------------------------------------------

def test_lookup_stores_the_server_completed_request_and_leaves_the_inquiry_unchanged(env, monkeypatch):
    calls = spy(monkeypatch, env.repository)
    work_before = env.work.get_work(env.inquiry["id"], context=env.staff)
    outcome = run_lookup(env, lookup_command(facts={"make": "Testimerkki A", "model_year": 2015}))
    lookup = outcome["lookup"]
    assert outcome["replayed"] is False and calls == ["find_stored_lookup", "find_stored_lookup",
                                                      "prepare_lookup", "store_lookup"]
    assert (lookup["outcome"], lookup["hours"], lookup["time_text"], lookup["synthetic"]) == (
        "FOUND", "0.8", "0,8 h", True)
    assert (lookup["adapter_name"], lookup["adapter_version"], lookup["source_work_version"]) == (
        "fake", FAKE_ADAPTER_VERSION, 1)
    # The registration comes from the inquiry; the browser never sends it.
    request = env.adapter.requests[0]
    assert request.vehicle_query.registration == REGISTRATION
    assert (request.vehicle_query.make, request.vehicle_query.model_year) == ("Testimerkki A", 2015)
    assert same_moment(lookup) == T0
    # A lookup does not change the inquiry version or its events.
    assert env.work.get_work(env.inquiry["id"], context=env.staff) == work_before
    assert env.adapter.calls == 1
    assert_lock_free(env.lock)


def test_command_digest_covers_only_the_client_body(env):
    command = lookup_command()
    sha = command.command_sha256(actor_user_id=env.staff.employee_id, inquiry_id=env.inquiry["id"])
    with sqlite3.connect(env.db) as connection:
        run_lookup(env, command)
        stored = connection.execute("SELECT command_sha256, snapshot_sha256 FROM autodata_lookup_payloads"
                                    ).fetchone()
    assert stored[0] == sha and stored[1] != sha
    upper = lookup_command(request_id=command.request_id.upper())
    assert upper.command_sha256(actor_user_id=env.staff.employee_id, inquiry_id=env.inquiry["id"]) == sha
    with pytest.raises(ValidationError):
        LookupCommand.model_validate({**command.model_dump(), "vehicle_facts": {"registration": "XYZ-999"}})
    with pytest.raises(ValidationError):
        LookupCommand.model_validate({**command.model_dump(), "registration": "XYZ-999"})


# -- stored retries -----------------------------------------------------------

def test_stored_retry_returns_without_an_adapter_call_even_after_a_version_change(env):
    command = lookup_command()
    first = run_lookup(env, command)
    AutodataAcceptanceService(env.repository, ALLOW_SYNTHETIC).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=first["lookup"]["lookup_id"], command=accept_command())
    assert env.work.get_work(env.inquiry["id"], context=env.staff)["version"] == 2
    again = run_lookup(env, lookup_command(request_id=command.request_id.upper()))
    assert again == {"lookup": first["lookup"], "replayed": True}
    assert again["lookup"]["source_work_version"] == 1 and env.adapter.calls == 1


def test_retry_conflict_and_redacted_retry_never_call_the_adapter(env):
    command = lookup_command()
    stored = run_lookup(env, command)
    with pytest.raises(IdempotencyConflictError):
        run_lookup(env, lookup_command(request_id=command.request_id, search="etujarrulevyt"))
    env.repository.redact_lookup(env.staff, inquiry_id=env.inquiry["id"], lookup_id=stored["lookup"]["lookup_id"],
                                 reason_code="ENTERED_IN_ERROR")
    with pytest.raises(RequestRedactedError):
        run_lookup(env, command)
    assert env.adapter.calls == 1
    assert_lock_free(env.lock)


def test_stored_retry_is_returned_while_another_lookup_holds_the_lock(env):
    command = lookup_command()
    first = run_lookup(env, command)
    assert env.lock.acquire(blocking=False)
    try:
        assert run_lookup(env, command) == {"lookup": first["lookup"], "replayed": True}
        with pytest.raises(LookupBusyError) as raised:
            run_lookup(env)
        assert raised.value.code == "LOOKUP_BUSY"
    finally:
        env.lock.release()
    assert env.adapter.calls == 1


def test_retry_is_checked_again_under_the_lock(env, monkeypatch):
    command = lookup_command()
    sha = command.command_sha256(actor_user_id=env.staff.employee_id, inquiry_id=env.inquiry["id"])
    original, parallel_store = env.repository.find_stored_lookup, env.repository.store_lookup
    locked, parallel = [], []

    def racing(context, **kwargs):
        locked.append(env.lock.locked())
        found = original(context, **kwargs)
        if len(locked) == 1:
            # A parallel request stores the same command between the two checks.
            request = command.adapter_request(registration=REGISTRATION)
            parallel.append(parallel_store(
                context, inquiry_id=env.inquiry["id"], request_id=command.request_id, command_sha256=sha,
                request=request, result=FakeAutodataLabourAdapter((VEHICLE,), clock=env.clock).lookup(request),
                source_work_version=1, requested_at=T0, retrieved_at=T0))
        return found

    monkeypatch.setattr(env.repository, "find_stored_lookup", racing)
    calls = spy(monkeypatch, env.repository, names=("prepare_lookup", "store_lookup"))
    outcome = run_lookup(env, command)
    assert outcome == {"lookup": parallel[0], "replayed": True}
    assert locked == [False, True] and calls == [] and env.adapter.calls == 0
    assert_lock_free(env.lock)


# -- lock ---------------------------------------------------------------------

def test_busy_lookup_calls_neither_prepare_nor_the_adapter(env, monkeypatch):
    calls = spy(monkeypatch, env.repository)
    before = counts(env)
    assert env.lock.acquire(blocking=False)
    try:
        with pytest.raises(LookupBusyError):
            run_lookup(env)
    finally:
        env.lock.release()
    assert calls == ["find_stored_lookup"] and env.adapter.calls == 0 and counts(env) == before


def test_separately_built_services_share_the_one_process_lock(env):
    # Codex r1: no lock is handed to either service or runner; none can be.
    first = build(env.work, env.clock, env.staff)
    second = SimpleNamespace(adapter=counting_fake(env.clock))
    second.service = AutodataLookupService(AutodataRepository(env.work, clock=env.clock), ENABLED,
                                           LookupRunner(second.adapter, FAKE))
    assert first.service is not second.service and first.runner is not second.service.runner
    entered, release = threading.Event(), threading.Event()
    results = []

    def wait(request):
        entered.set()
        assert release.wait(timeout=10)

    first.adapter.during = wait
    worker = threading.Thread(target=lambda: results.append(run_lookup(env, service=first.service)))
    worker.start()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(LookupBusyError):
            run_lookup(env, service=second.service)
        assert second.adapter.calls == 0
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and results[0]["lookup"]["outcome"] == "FOUND"
    assert_lock_free(PROCESS_LOOKUP_LOCK)


def _stale(env):
    return lookup_command(version=2), StaleViewError, 0


def _closed(env):
    close(env)
    return lookup_command(), AutodataClosedError, 0


def _identifier_in_search_text(env):
    return lookup_command(search="abc 123 jarrut"), IdentifierInSearchTextError, 0


def _identifier_in_snapshot(env):
    return lookup_command(facts={"model": "Testimalli ABC-123"}), IdentifierInRequestError, 0


def _store_fails(env):
    sql(env, "CREATE TRIGGER inject_failure BEFORE INSERT ON autodata_lookup_payloads "
             "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    env.cleanup = lambda: sql(env, "DROP TRIGGER inject_failure")
    return lookup_command(), sqlite3.IntegrityError, 1


def _membership_revoked_during_call(env):
    env.adapter.during = lambda request: revoke_membership(env, env.staff)
    return lookup_command(), WorkshopAccessDenied, 1


def _closed_during_call(env):
    env.adapter.during = lambda request: close(env)
    return lookup_command(), AutodataClosedError, 1


def _registration_rejected_by_the_request_model(env):
    env.inquiry = env.work.create_work(registration="ABC-123-KEKSITTY-LIIAN-PITKA", customer_description="Pitkä",
                                       actor="Testikirjaaja", idempotency_key=str(uuid4()), context=env.staff)
    return lookup_command(), ValidationError, 0


@pytest.mark.parametrize("case", [
    _stale, _closed, _identifier_in_search_text, _identifier_in_snapshot, _store_fails,
    _membership_revoked_during_call, _closed_during_call, _registration_rejected_by_the_request_model,
])
def test_every_failure_stores_nothing_and_releases_the_lock(env, case):
    env.cleanup = None
    command, error, adapter_calls = case(env)
    before = counts(env)
    with pytest.raises(error):
        run_lookup(env, command)
    assert env.adapter.calls == adapter_calls
    after = counts(env)
    if case is _closed_during_call:
        # Only the closure itself was written, by the action inside the adapter call.
        assert after.pop("autodata_inquiry_closures") == before.pop("autodata_inquiry_closures") + 1
    assert after == before
    assert_lock_free(env.lock)
    if env.cleanup is not None:
        env.cleanup()
    env.adapter.during = None
    # The next lookup runs normally: the lock was released.
    other = test_context("another-test-employee")
    inquiry = env.work.create_work(registration="DEF-456", customer_description="Seuraava", actor="Testikirjaaja",
                                   idempotency_key=str(uuid4()), context=other)
    assert run_lookup(env, context=other, inquiry_id=inquiry["id"])["lookup"]["outcome"] == "FOUND"


# -- ERROR and TIMEOUT ----------------------------------------------------------

def test_adapter_exception_is_stored_as_error_without_its_text(env, caplog):
    def fail(request):
        env.clock.now += timedelta(seconds=3)
        raise RuntimeError(SECRET)

    env.adapter.during = fail
    caplog.set_level(logging.DEBUG)
    lookup = run_lookup(env)["lookup"]
    assert (lookup["outcome"], lookup["hours"], lookup["adapter_name"], lookup["adapter_version"],
            lookup["synthetic"]) == ("ERROR", None, "fake", FAKE_ADAPTER_VERSION, True)
    assert same_moment(lookup) == T0 + timedelta(seconds=3)
    assert datetime.fromisoformat(lookup["requested_at"]) == T0
    assert SECRET not in database_text(env.db) and "example.invalid" not in database_text(env.db)
    assert SECRET not in caplog.text and "etujarrupalat" not in caplog.text
    assert_lock_free(env.lock)


def test_late_result_is_stored_as_timeout_with_the_runner_identity(env):
    def slow(request):
        env.clock.ticks += env.runner.timeout_seconds + 1
        env.clock.now += timedelta(seconds=3)

    env.adapter.during = slow
    lookup = run_lookup(env)["lookup"]
    assert (lookup["outcome"], lookup["hours"], lookup["time_text"], lookup["adapter_name"],
            lookup["adapter_version"], lookup["synthetic"]) == (
        "TIMEOUT", None, None, "fake", FAKE_ADAPTER_VERSION, True)
    assert same_moment(lookup) == T0 + timedelta(seconds=3)
    assert env.adapter.calls == 1
    assert_lock_free(env.lock)


def test_retrieved_at_equals_the_result_provenance_for_found_results(env):
    env.adapter.during = lambda request: setattr(env.clock, "now", T0 + timedelta(seconds=7))
    lookup = run_lookup(env)["lookup"]
    assert lookup["outcome"] == "FOUND" and same_moment(lookup) == T0 + timedelta(seconds=7)
    assert datetime.fromisoformat(lookup["requested_at"]) == T0


# -- service construction -----------------------------------------------------

@pytest.mark.parametrize("identity", [
    AdapterIdentity("synthetic-page", SYNTHETIC_ADAPTER_VERSION, True),
    AdapterIdentity("fake", FAKE_ADAPTER_VERSION, False),
    AdapterIdentity("fake", "fake-2", True),
])
def test_enabled_service_accepts_only_the_injected_fake_runner(env, identity):
    runner = LookupRunner(env.adapter, identity)
    with pytest.raises(ValueError):
        AutodataLookupService(env.repository, ENABLED, runner)


class StubPage:
    """Records every browser call; no browser exists."""

    url = "about:blank"

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
        return call


class FakeSubclass(FakeAutodataLabourAdapter):
    def lookup(self, request):
        self.calls = getattr(self, "calls", 0) + 1
        return super().lookup(request)


class OtherAdapter(AutodataLabourAdapter):
    def lookup(self, request):
        self.calls = getattr(self, "calls", 0) + 1
        raise AssertionError("never called")


@pytest.mark.parametrize("make", [
    lambda page, clock: SyntheticPageAdapter(page, "https://synthetic.invalid/"),
    lambda page, clock: FakeSubclass((VEHICLE,), clock=clock),
    lambda page, clock: OtherAdapter(),
])
def test_a_wrong_adapter_with_the_fake_identity_is_refused_before_any_call(env, monkeypatch, make):
    # Codex r1: the declared fake identity alone is not enough; the class is checked too.
    page = StubPage()
    adapter = make(page, env.clock)
    with pytest.raises(ValueError):
        AutodataLookupService(env.repository, ENABLED, LookupRunner(adapter, FAKE))
    # Rewired after construction: refused after the retry check, before the lock and the adapter.
    env.service.runner.adapter = adapter
    before = unchanged(env)
    calls = spy(monkeypatch, env.repository)
    with pytest.raises(ValueError):
        run_lookup(env)
    assert calls == ["find_stored_lookup"] and unchanged(env) == before
    assert page.calls == [] and getattr(adapter, "calls", 0) == 0 and env.adapter.calls == 0
    assert_lock_free(env.lock)


def test_the_real_fake_still_works_and_a_runner_subclass_is_refused(env):
    assert run_lookup(env)["lookup"]["outcome"] == "FOUND" and env.adapter.calls == 1

    class Runner(LookupRunner):
        pass

    with pytest.raises(TypeError):
        AutodataLookupService(env.repository, ENABLED, Runner(counting_fake(env.clock), FAKE))


def test_service_constructors_require_server_objects(env):
    with pytest.raises(TypeError):
        AutodataLookupService(env.repository, ENABLED)
    with pytest.raises(TypeError):
        AutodataLookupService(env.repository, {"mode": "fake"}, env.runner)
    with pytest.raises(TypeError):
        AutodataLookupService(env.work, ENABLED, env.runner)
    with pytest.raises(TypeError):
        AutodataAcceptanceService(env.repository, None)
    assert AutodataLookupService(env.repository, DISABLED).runner is None


def test_services_require_the_save_permission(env, monkeypatch):
    calls = spy(monkeypatch, env.repository)
    reader = test_context("trusted-reader", permissions=frozenset({WorkshopPermission.READ_LABOUR_PLAN}))
    with pytest.raises(PermissionError):
        run_lookup(env, context=reader)
    with pytest.raises(PermissionError):
        env.acceptance.accept(reader, inquiry_id=env.inquiry["id"], lookup_id=str(uuid4()),
                              command=accept_command())
    assert calls == [] and env.adapter.calls == 0


# -- acceptance ---------------------------------------------------------------

def test_synthetic_acceptance_is_refused_by_default(env):
    lookup = run_lookup(env)["lookup"]
    before = counts(env)
    with pytest.raises(SyntheticNotAcceptableError):
        env.acceptance.accept(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                              command=accept_command())
    assert counts(env) == before
    accepted = AutodataAcceptanceService(env.repository, ALLOW_SYNTHETIC).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"], command=accept_command())
    assert accepted["decision"]["resulting_work_version"] == 2 and accepted["replayed"] is False


@pytest.mark.parametrize("settings", [ENABLED, ALLOW_SYNTHETIC])
def test_acceptance_delegates_with_the_setting_and_returns_the_repository_result(env, monkeypatch, settings):
    received, sentinel = [], object()
    monkeypatch.setattr(env.repository, "accept_lookup",
                        lambda *args, **kwargs: received.append((args, kwargs)) or sentinel)
    command, lookup_id = accept_command(), str(uuid4())
    result = AutodataAcceptanceService(env.repository, settings).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup_id, command=command)
    assert result is sentinel
    assert received == [((env.staff,), {"inquiry_id": env.inquiry["id"], "lookup_id": lookup_id,
                                        "command": command,
                                        "allow_synthetic": settings.allow_synthetic_acceptance})]


def test_acceptance_retry_keeps_the_receipt_contract_also_when_disabled(env):
    lookup = run_lookup(env)["lookup"]
    acceptance = AutodataAcceptanceService(env.repository, ALLOW_SYNTHETIC)
    command = accept_command()
    first = acceptance.accept(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                              command=command)
    again = acceptance.accept(env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"],
                              command=command)
    assert again["replayed"] is True and again["decision"] == first["decision"]
    assert set(again["decision"]) == {"decision_id", "inquiry_id", "lookup_id", "resulting_work_version",
                                      "decided_at"}
    replayed = AutodataAcceptanceService(env.repository, DISABLED).accept(
        env.staff, inquiry_id=env.inquiry["id"], lookup_id=lookup["lookup_id"], command=command)
    assert replayed["replayed"] is True and replayed["decision"] == first["decision"]


# -- real auth sessions -------------------------------------------------------

@pytest.fixture
def bound(tmp_path):
    work = ScopedTestRepository(tmp_path / "works.db")
    work.initialize()
    auth_repository = AuthRepository(tmp_path / "auth.db")
    auth_repository.initialize()
    service = AuthService(auth_repository, TotpCipher((Fernet.generate_key(),)))
    service.workshop_directory = WorkshopDirectory(work, auth_repository)
    session = grant_session(service)
    token = session.token.get_secret_value()
    context = resolve_workshop_context(service, token, required_permission=WorkshopPermission.SAVE_LABOUR_PLAN)
    inquiry = work.create_work(registration=REGISTRATION, customer_description="Keksitty sidottu testi",
                               actor=context.actor, idempotency_key=str(uuid4()), context=context)
    clock = Clock()
    return SimpleNamespace(work=work, clock=clock, staff=context, inquiry=inquiry, auth=service, token=token,
                           user_id=session.user.user_id, db=tmp_path / "works.db",
                           auth_db=tmp_path / "auth.db", **vars(build(work, clock, context)))


def test_adapter_runs_outside_every_operation_and_its_transactions(bound, monkeypatch):
    original = bound.work.operation
    depth, entries, observed = [0], [0], []

    @contextmanager
    def counted(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            depth[0] += 1
            entries[0] += 1
            try:
                yield connection
            finally:
                depth[0] -= 1

    def probe(request):
        observed.append(depth[0])
        # Neither the auth reservation nor a business write transaction is open.
        for path in (bound.auth_db, bound.db):
            connection = sqlite3.connect(path, timeout=0)
            try:
                connection.execute("PRAGMA busy_timeout = 0")
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()

    monkeypatch.setattr(bound.work, "operation", counted)
    bound.adapter.during = probe
    assert run_lookup(bound)["lookup"]["outcome"] == "FOUND"
    assert observed == [0] and entries[0] == 4 and depth[0] == 0


def test_membership_revoked_during_the_adapter_call_stores_nothing(bound):
    bound.adapter.during = lambda request: bound.auth.workshop_directory.revoke_membership(
        bound.user_id, TEST_WORKSHOP, operator_reference="Keksitty samanaikainen poisto")
    before = counts(bound)
    with pytest.raises(WorkshopAccessDenied):
        run_lookup(bound)
    assert counts(bound) == before and bound.adapter.calls == 1
    assert_lock_free(bound.lock)


def test_session_revoked_during_the_adapter_call_stores_nothing(bound):
    bound.adapter.during = lambda request: bound.auth.revoke_session(bound.token)
    before = counts(bound)
    with pytest.raises(AuthenticationError):
        run_lookup(bound)
    assert counts(bound) == before and bound.adapter.calls == 1
    assert_lock_free(bound.lock)
