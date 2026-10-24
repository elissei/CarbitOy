"""Autodata 1B-2 lookup runner: synchronous call, outcome mapping and outcome-only logging.

Fake adapter and scripted test adapters only; no browser, network or database.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from integrations.autodata.adapter import AutodataLabourAdapter
from integrations.autodata.fake_adapter import (
    FAKE_ADAPTER_VERSION, FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import (
    Choice, ChoiceIdentity, LabourTimeRequest, LookupOutcome, VehicleQuery, WorkQuery,
)
from integrations.autodata.runner import (
    DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, PROCESS_LOOKUP_LOCK, AdapterIdentity, LookupRun,
    LookupRunner,
)
from integrations.autodata.synthetic_reader import SYNTHETIC_ADAPTER_VERSION


T0 = datetime(2026, 10, 24, 9, 0, 0, 250000, tzinfo=timezone.utc)
FAKE = AdapterIdentity("fake", FAKE_ADAPTER_VERSION, True)
SECRET = "Keksitty sivuteksti https://example.invalid/?evaste=salainen"


def choice(*path):
    return Choice(identity=ChoiceIdentity(selection_path=path))


VEHICLE = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1"), operations=(
    FakeOperation(choice=choice("Jarrut", "Etujarrupalat, vaihto"),
                  variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,8 h"),)),
))
REQUEST = LabourTimeRequest(vehicle_query=VehicleQuery(registration="ABC-123"),
                            work_query=WorkQuery(search_text="etujarrupalat"))


class Clock:
    """Wall clock and monotonic seconds, advanced explicitly by the tests."""

    def __init__(self):
        self.now = T0
        self.ticks = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.ticks


class Scripted(AutodataLabourAdapter):
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def lookup(self, request):
        self.calls += 1
        return self.action(request)


def runner(adapter, clock, *, identity=FAKE, **kwargs):
    return LookupRunner(adapter, identity, clock=clock, monotonic=clock.monotonic, **kwargs)


def fake(clock, **kwargs):
    return FakeAutodataLabourAdapter((VEHICLE,), clock=clock, **kwargs)


def test_found_passes_through_with_its_request_time():
    clock = Clock()
    run = runner(fake(clock), clock).run(REQUEST)
    assert isinstance(run, LookupRun)
    assert run.result.outcome == LookupOutcome.FOUND and run.result.hours == Decimal("0.8")
    assert run.requested_at == T0 == run.result.provenance.retrieved_at


def test_adapter_exception_becomes_error_with_the_explicit_identity_and_logs_only_the_outcome(caplog):
    clock = Clock()

    def fail(request):
        clock.now += timedelta(seconds=2)
        raise RuntimeError(SECRET)

    caplog.set_level(logging.DEBUG)
    run = runner(Scripted(fail), clock).run(REQUEST)
    provenance = run.result.provenance
    assert run.result.outcome == LookupOutcome.ERROR and run.result.hours is None
    assert (provenance.adapter_name, provenance.adapter_version, provenance.synthetic) == (
        "fake", FAKE_ADAPTER_VERSION, True)
    assert provenance.retrieved_at == T0 + timedelta(seconds=2) and run.requested_at == T0
    assert [record.getMessage() for record in caplog.records] == ["autodata lookup finished: outcome=ERROR"]
    assert all(record.exc_info is None for record in caplog.records)
    for text in (SECRET, "example.invalid", "evaste", "etujarrupalat", "ABC-123"):
        assert text not in caplog.text


@pytest.mark.parametrize(("elapsed", "outcome"), [
    (DEFAULT_TIMEOUT_SECONDS, LookupOutcome.FOUND),
    (DEFAULT_TIMEOUT_SECONDS + 0.001, LookupOutcome.TIMEOUT),
])
def test_timeout_is_detected_after_the_call_returns(elapsed, outcome):
    clock = Clock()
    adapter = fake(clock)

    def slow(request):
        clock.ticks += elapsed
        clock.now += timedelta(seconds=1)
        return adapter.lookup(request)

    run = runner(Scripted(slow), clock).run(REQUEST)
    assert run.result.outcome == outcome
    assert run.result.provenance.retrieved_at == T0 + timedelta(seconds=1)
    if outcome == LookupOutcome.TIMEOUT:
        # The late FOUND is discarded, never stored with hours.
        assert run.result.hours is None and run.result.vehicle is None
        assert run.result.provenance.adapter_name == "fake"


@pytest.mark.parametrize("failure", [LookupOutcome.TIMEOUT, LookupOutcome.ERROR, LookupOutcome.LOGIN_REQUIRED])
def test_adapter_reported_failures_pass_through(failure):
    clock = Clock()
    run = runner(fake(clock, failure=failure), clock).run(REQUEST)
    assert run.result.outcome == failure and run.result.provenance.retrieved_at == T0


def _shifted(delta):
    def lookup(request):
        return fake(lambda: T0 + delta).lookup(request)
    return lookup


@pytest.mark.parametrize(("case", "identity"), [
    ("other adapter identity", AdapterIdentity("synthetic-page", SYNTHETIC_ADAPTER_VERSION, True)),
    ("synthetic flag differs", AdapterIdentity("fake", FAKE_ADAPTER_VERSION, False)),
    ("version differs", AdapterIdentity("fake", "fake-2", True)),
    ("retrieved before the request", FAKE),
    ("retrieved after the call returned", FAKE),
    ("not a result", FAKE),
])
def test_results_that_do_not_match_the_run_become_error(case, identity):
    clock = Clock()
    actions = {
        "retrieved before the request": _shifted(-timedelta(microseconds=1)),
        "retrieved after the call returned": _shifted(timedelta(hours=1)),
        "not a result": lambda request: {"outcome": "FOUND", "hours": "0.8"},
    }
    run = runner(Scripted(actions.get(case, fake(clock).lookup)), clock, identity=identity).run(REQUEST)
    provenance = run.result.provenance
    assert run.result.outcome == LookupOutcome.ERROR and run.result.hours is None
    assert (provenance.adapter_name, provenance.adapter_version, provenance.synthetic) == (
        identity.name, identity.version, identity.synthetic)
    assert provenance.retrieved_at == T0


def test_a_clock_stepping_back_never_stores_retrieved_before_requested():
    clock = Clock()

    def step_back(request):
        clock.now = T0 - timedelta(minutes=5)
        raise RuntimeError(SECRET)

    run = runner(Scripted(step_back), clock).run(REQUEST)
    assert run.result.outcome == LookupOutcome.ERROR
    assert run.result.provenance.retrieved_at == run.requested_at == T0


def test_the_adapter_runs_synchronously_in_the_callers_thread_without_the_lock():
    clock = Clock()
    seen = []
    adapter = fake(clock)

    def observe(request):
        seen.append((threading.current_thread() is threading.main_thread(), threading.active_count(),
                     PROCESS_LOOKUP_LOCK.locked()))
        return adapter.lookup(request)

    threads = threading.active_count()
    assert runner(Scripted(observe), clock).run(REQUEST).result.outcome == LookupOutcome.FOUND
    # run() never takes the lookup lock; the service holds it around the whole lookup.
    assert seen == [(True, threads, False)] and threading.active_count() == threads
    assert not PROCESS_LOOKUP_LOCK.locked()


def test_the_runner_accepts_no_lock_of_its_own():
    # Codex r1: an injected lock let separately built services bypass the process limit.
    clock = Clock()
    with pytest.raises(TypeError):
        LookupRunner(fake(clock), FAKE, lock=threading.Lock())
    built = runner(fake(clock), clock)
    assert not any(hasattr(built, name) for name in ("_lock", "lock", "try_acquire", "release"))


def test_base_exceptions_are_not_turned_into_outcomes():
    clock = Clock()

    def interrupt(request):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runner(Scripted(interrupt), clock).run(REQUEST)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5",
                                     MAX_TIMEOUT_SECONDS + 0.001])
def test_timeout_must_be_a_finite_positive_bounded_number(timeout):
    clock = Clock()
    with pytest.raises(ValueError):
        runner(fake(clock), clock, timeout_seconds=timeout)


def test_constructor_requires_an_adapter_and_an_explicit_identity():
    clock = Clock()
    assert runner(fake(clock), clock, timeout_seconds=MAX_TIMEOUT_SECONDS).timeout_seconds == MAX_TIMEOUT_SECONDS
    with pytest.raises(TypeError):
        LookupRunner(object(), FAKE)
    with pytest.raises(TypeError):
        LookupRunner(fake(clock), ("fake", FAKE_ADAPTER_VERSION, True))
    for name, version, synthetic in (("", "v", True), ("fake", " ", True), ("fake", "v", 1), (None, "v", True)):
        with pytest.raises(ValueError):
            AdapterIdentity(name, version, synthetic)


def test_a_naive_clock_is_rejected_before_the_adapter_is_called():
    adapter = Scripted(lambda request: None)
    naive = LookupRunner(adapter, FAKE, clock=lambda: datetime(2026, 10, 24, 9))
    with pytest.raises(ValueError):
        naive.run(REQUEST)
    assert adapter.calls == 0
