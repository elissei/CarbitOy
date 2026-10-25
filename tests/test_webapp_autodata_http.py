"""Autodata 1B-3 HTTP boundary over real MFA sessions, CSRF and the in-process fake.

Every database is a new synthetic test database under pytest's temporary
directory; no existing var database is opened. Only the explicitly injected
fake adapter runs; no real Autodata, browser or credential is used.
"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from http_auth_support import AuthenticatedTestClient, business_headers, create_test_app, grant_session
from integrations.autodata.fake_adapter import (
    FAKE_ADAPTER_VERSION, FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import Choice, ChoiceIdentity
from integrations.autodata.runner import PROCESS_LOOKUP_LOCK, AdapterIdentity, LookupRunner
from webapp import autodata_service, http_access
from webapp.auth_models import SessionCookiePolicy, UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthenticationError
from webapp.autodata_migrations import CLOSE_CONFIRMATION
from webapp.autodata_repository import (
    AlreadyAcceptedError, AutodataClosedError, AutodataDisabledError, AutodataIntegrityError,
    ExpiredLookupError, IdentifierInRequestError, IdentifierInSearchTextError, InvalidLookupTimeError,
    LookupNotFoundError, LookupRedactedError, NotAcceptableError, RequestRedactedError, StaleLookupError,
    StaleViewError, SyntheticNotAcceptableError,
)
from webapp.autodata_service import (
    AutodataAcceptanceService, AutodataLookupService, LookupBusyError, LookupQuota, LookupRateLimitedError,
)
from webapp.autodata_settings import AutodataConfigurationError, AutodataMode, AutodataSettings
from webapp.database import IdempotencyConflictError, WorkNotFoundError, WorkRepository
from webapp.http_autodata import MAX_BODY_BYTES, MESSAGES, actor_view, display_names
from webapp.labour_snapshot import UnpriceableLabourTimeError
from webapp.workshop_context import WorkshopAccessDenied, WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context
from workshop_support import TEST_ORGANIZATION, TEST_WORKSHOP


FAKE = AdapterIdentity("fake", FAKE_ADAPTER_VERSION, True)
ENABLED = AutodataSettings(mode=AutodataMode.FAKE, allow_synthetic_acceptance=True)
NO_SYNTHETIC = AutodataSettings(mode=AutodataMode.FAKE)
DISABLED = AutodataSettings()
CONFIRM = "VEHICLE_OPERATION_VARIANT_TIME"
WORKSHOP_KEY = (TEST_ORGANIZATION, TEST_WORKSHOP)
SECRET = "KEKSITTY-SALAISUUS-EI-SAA-TOISTUA"
COOKIE = SessionCookiePolicy().name
OPENAPI_PATHS = {"/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour"}
AUTODATA_POLICIES = {
    ("/api/works/{work_id}/autodata/lookups", "GET"): "read",
    ("/api/works/{work_id}/autodata/lookups", "POST"): "write",
    ("/api/works/{work_id}/autodata/lookups/{lookup_id}", "GET"): "read",
    ("/api/works/{work_id}/autodata/lookups/{lookup_id}/accept", "POST"): "write",
    ("/api/works/{work_id}/autodata/decisions", "GET"): "read",
}


def choice(*path):
    return Choice(identity=ChoiceIdentity(selection_path=path))


VEHICLE = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1"), operations=(
    FakeOperation(choice=choice("Jarrut", "Etujarrupalat, vaihto"),
                  variants=(FakeVariant(choice=choice("Vakio"), displayed_time_text="0,8 h"),)),
))


def counting_fake(fleet=(VEHICLE,)):
    """The real fake; its clock, read once inside every lookup, counts the calls."""
    def clock():
        adapter.calls += 1
        if adapter.during is not None:
            adapter.during()
        return datetime.now(timezone.utc)

    adapter = FakeAutodataLabourAdapter(fleet, clock=clock)
    adapter.calls, adapter.during = 0, None
    return adapter


@pytest.fixture(autouse=True)
def quota(monkeypatch):
    """A fresh process quota per test function; nothing else ever resets it."""
    fresh = LookupQuota()
    monkeypatch.setattr(autodata_service, "PROCESS_LOOKUP_QUOTA", fresh)
    return fresh


@pytest.fixture
def http(tmp_path):
    adapter = counting_fake()
    app = create_test_app(tmp_path / "works.db", autodata_settings=ENABLED,
                          autodata_runner=LookupRunner(adapter, FAKE))
    with AuthenticatedTestClient(app) as client:
        client.adapter = adapter
        client.work = new_inquiry(client)
        yield client


def new_inquiry(http, registration="ABC-123"):
    response = http.post("/api/works", json={"registration": registration, "customer_description": "Keksitty HTTP-testi",
                                             "idempotency_key": str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def url(http, *parts, work_id=None):
    return "/api/works/" + (work_id or http.work["id"]) + "/autodata/" + "/".join(parts)


def lookup_body(**fields):
    return {"request_id": str(uuid4()), "expected_version": 1, "vehicle_facts": {},
            "work_query": "etujarrupalat", **fields}


def accept_body(**fields):
    return {"request_id": str(uuid4()), "expected_version": 1, "confirmation": CONFIRM, **fields}


def post(http, target, body, **kwargs):
    headers = {**business_headers(http), **kwargs.pop("headers", {})}
    return http.post(target, json=body, headers=headers, **kwargs)


def post_raw(http, target, content, **headers):
    return http.post(target, content=content, headers={**business_headers(http),
                                                       "Content-Type": "application/json", **headers})


def lookup(http, body=None, **kwargs):
    return post(http, url(http, "lookups", **kwargs), body or lookup_body())


def found(http, body=None):
    response = lookup(http, body)
    assert response.status_code == 201, response.text
    return response.json()["lookup"]


def accept(http, lookup_id, body=None, **kwargs):
    return post(http, url(http, "lookups", lookup_id, "accept", **kwargs), body or accept_body())


def dump(http):
    with closing(sqlite3.connect(http.app.state.repository.database_path)) as connection:
        return "\n".join(connection.iterdump())


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": MESSAGES[code], "code": code}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def login(http, role=UserRole.OWNER):
    grant = grant_session(http.app.state.auth_service, role)
    http.cookies.set(COOKIE, grant.token.get_secret_value())
    return grant


def context(http, permission=WorkshopPermission.APPROVE_LABOUR_PLAN):
    return resolve_workshop_context(http.app.state.auth_service, http.cookies.get(COOKIE),
                                    required_permission=permission)


def rewire(http, settings, runner=None):
    """Services rebuilt as a restart with other settings would build them, on the same databases."""
    repository = http.app.state.autodata_repository
    http.app.state.autodata_settings = settings
    http.app.state.autodata_lookups = AutodataLookupService(repository, settings, runner)
    http.app.state.autodata_acceptance = AutodataAcceptanceService(repository, settings)


def redact(http, lookup_id):
    http.app.state.autodata_repository.redact_lookup(context(http), inquiry_id=http.work["id"], lookup_id=lookup_id,
                                                     reason_code="ENTERED_IN_ERROR")


def close(http):
    http.app.state.autodata_repository.close_autodata(context(http), inquiry_id=http.work["id"],
                                                      reason_code="RETENTION", confirmation=CLOSE_CONFIRMATION)


def operations(http, lookup_id):
    return [
        ("POST", url(http, "lookups"), lookup_body()),
        ("GET", url(http, "lookups"), None),
        ("GET", url(http, "lookups", lookup_id), None),
        ("POST", url(http, "lookups", lookup_id, "accept"), accept_body()),
        ("GET", url(http, "decisions"), None),
    ]


# -- G1 route inventory (MAIN 8) -------------------------------------------------------

def test_five_policies_for_four_paths_are_internal_and_not_in_openapi(http):
    for key, policy in AUTODATA_POLICIES.items():
        assert http_access.ROUTE_PERMISSIONS[key] == policy
    assert len(http_access.validate_route_coverage(http.app)) == len(http_access.ROUTE_PERMISSIONS)
    assert len({path for path, _ in AUTODATA_POLICIES}) == 4
    assert set(http.app.openapi()["paths"]) == OPENAPI_PATHS


@pytest.mark.parametrize("missing", sorted(AUTODATA_POLICIES))
def test_a_missing_policy_stops_startup_before_any_initialize(tmp_path, monkeypatch, initializations, missing):
    reduced = {key: value for key, value in http_access.ROUTE_PERMISSIONS.items() if key != missing}
    monkeypatch.setattr(http_access, "ROUTE_PERMISSIONS", MappingProxyType(reduced))
    with pytest.raises(ValueError, match="access policy"):
        start(tmp_path)
    assert initializations == [] and not (tmp_path / "works.db").exists()


def test_an_extra_registered_method_stops_startup(tmp_path, initializations):
    app = create_test_app(tmp_path / "works.db")
    app.add_api_route("/api/works/{work_id}/autodata/lookups", lambda: {"never": "run"}, methods=["PUT"])
    with pytest.raises(ValueError, match="access policy"):
        with TestClient(app, base_url="https://testserver"):
            pass
    assert initializations == [] and not (tmp_path / "works.db").exists()


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_unregistered_methods_never_reach_a_handler(http, method):
    record = found(http)
    before = dump(http)
    for target in (url(http, "lookups"), url(http, "lookups", record["lookup_id"]),
                   url(http, "lookups", record["lookup_id"], "accept"), url(http, "decisions")):
        response = http.request(method, target, headers=business_headers(http), json={})
        assert response.status_code == 405
    assert dump(http) == before and http.adapter.calls == 1


# -- G2 authentication and roles ----------------------------------------------------------

def test_every_route_requires_a_session(http):
    record = found(http)
    anonymous = TestClient(http.app, base_url="https://testserver")
    for method, target, body in operations(http, record["lookup_id"]):
        response = anonymous.request(method, target, json=body, headers={"Origin": "https://testserver"})
        assert response.status_code == 401
        # The gate's own response keeps its shape: no Autodata code.
        assert "code" not in response.json() and response.headers["cache-control"] == "no-store"
    assert http.adapter.calls == 1


def test_it_admin_reads_but_never_writes(http):
    record = found(http)
    login(http, UserRole.IT_ADMIN)
    before = dump(http)
    for method, target, body in operations(http, record["lookup_id"]):
        response = http.request(method, target, json=body, headers=business_headers(http))
        assert response.status_code == (200 if method == "GET" else 403), (target, response.text)
    assert dump(http) == before and http.adapter.calls == 1


@pytest.mark.parametrize("change", ["membership", "session"])
def test_revoked_rights_are_denied_on_every_route(http, change):
    record = found(http)
    headers = business_headers(http)
    service = http.app.state.auth_service
    if change == "membership":
        service.workshop_directory.revoke_membership(http.grant.user.user_id, TEST_WORKSHOP,
                                                     operator_reference="Keksitty HTTP-poisto")
    else:
        service.revoke_session(http.cookies.get(COOKIE))
    before = dump(http)
    for method, target, body in operations(http, record["lookup_id"]):
        response = http.request(method, target, json=body, headers=headers)
        assert response.status_code == (403 if change == "membership" else 401), response.text
    assert dump(http) == before and http.adapter.calls == 1


# -- G3 CSRF and Origin ---------------------------------------------------------------------

@pytest.mark.parametrize("route", ["lookup", "accept"])
@pytest.mark.parametrize("case,status", [
    ("no_token", 403), ("invalid_token", 403), ("no_origin", 403), ("foreign_origin", 403), ("two_tokens", 400),
])
def test_csrf_and_origin_are_checked_before_any_body_or_service(http, route, case, status):
    record = found(http)
    target = url(http, "lookups") if route == "lookup" else url(http, "lookups", record["lookup_id"], "accept")
    good = business_headers(http)
    headers = {
        "no_token": [("Origin", good["Origin"])],
        "invalid_token": [("Origin", good["Origin"]), ("X-CSRF-Token", "invalid")],
        "no_origin": [("X-CSRF-Token", good["X-CSRF-Token"])],
        "foreign_origin": [("Origin", "https://evil.example"), ("X-CSRF-Token", good["X-CSRF-Token"])],
        "two_tokens": [("Origin", good["Origin"]), ("X-CSRF-Token", good["X-CSRF-Token"]),
                       ("X-CSRF-Token", good["X-CSRF-Token"])],
    }[case]
    before = dump(http)
    response = http.post(target, content=json.dumps(lookup_body() if route == "lookup" else accept_body()),
                         headers=headers + [("Content-Type", "application/json")])
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert dump(http) == before and http.adapter.calls == 1


# -- G4 HTTP boundary -------------------------------------------------------------------------

def test_content_type_and_framing(http):
    before = dump(http)
    response = http.post(url(http, "lookups"), content=json.dumps(lookup_body()),
                         headers={**business_headers(http), "Content-Type": "text/plain"})
    assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
    request = http.build_request("POST", url(http, "lookups"), content=b"{}",
                                 headers={**business_headers(http), "Content-Type": "application/json"})
    request.headers["content-length"] = "3"
    assert_error(http.send(request), 400, "INVALID_FRAMING")
    assert dump(http) == before and http.adapter.calls == 0


@pytest.mark.parametrize("content", [
    b'{"request_id":"a","request_id":"b"}', b'{"expected_version":NaN}', b"[]", b"null", b"invalid",
    b"\xff", b"[" * 2000, b'{"work_query":"\x01raaka"}',
])
def test_invalid_json_is_422_without_echo(http, content):
    before = dump(http)
    assert_error(post_raw(http, url(http, "lookups"), content), 422, "INVALID_REQUEST")
    assert dump(http) == before and http.adapter.calls == 0


@pytest.mark.parametrize("change", [
    {"registration": "XYZ-999"}, {"vehicle_facts": {"registration": "XYZ-999"}}, {"hours": "1.0"},
    {"expected_version": 0}, {"expected_version": True}, {"expected_version": "1"}, {"expected_version": 2**63},
    {"request_id": "not-a-uuid"}, {"work_query": ""}, {"work_query": "x" * 201}, {"unknown": SECRET},
    {"vehicle_facts": {"make": SECRET * 10}},
    {"vehicle_selection": {"selection_path": ["a"] * 9}},
    {"vehicle_selection": {"selection_path": ["a" * 201]}},
    {"vehicle_selection": {"selection_path": ["a"], "source_id": "s" * 101}},
    {"operation_selection": {"selection_path": ["a"]}},
])
def test_strict_lookup_body_is_422_without_echo(http, change):
    before = dump(http)
    response = lookup(http, {**lookup_body(), **change})
    assert_error(response, 422, "INVALID_REQUEST")
    assert SECRET not in response.text
    assert dump(http) == before and http.adapter.calls == 0


@pytest.mark.parametrize("change", [
    {"confirmation": "YES"}, {"confirmation": None}, {"hours": "1.0"}, {"lookup_id": str(uuid4())},
    {"vehicle_identity": {"selection_path": ["a"]}}, {"expected_result_sha256": "0" * 64},
    {"expected_version": 2**63}, {"expected_version": 0},
])
def test_strict_accept_body_is_422(http, change):
    record = found(http)
    before = dump(http)
    body = {**accept_body(), **change}
    if change.get("confirmation", CONFIRM) is None:
        body.pop("confirmation")
    assert_error(accept(http, record["lookup_id"], body), 422, "INVALID_REQUEST")
    assert dump(http) == before


def test_limits_at_the_maximum_are_accepted(http):
    selection = {"selection_path": ["a" * 200] * 8, "source_id": "s" * 100}
    response = lookup(http, lookup_body(expected_version=1, vehicle_selection=selection))
    assert response.status_code == 201, response.text
    record = response.json()["lookup"]
    # A valid maximal version passes validation; the unmatched selection is simply not acceptable.
    assert_error(accept(http, record["lookup_id"], accept_body(expected_version=2**63 - 1)), 409, "NOT_ACCEPTABLE")


@pytest.mark.parametrize("query", ["?a=1", "?limit=1", "?request_id=x"])
def test_query_strings_are_refused_on_single_resource_routes(http, query):
    record = found(http)
    assert_error(post(http, url(http, "lookups") + query, lookup_body()), 422, "INVALID_REQUEST")
    assert_error(http.get(url(http, "lookups", record["lookup_id"]) + query), 422, "INVALID_REQUEST")
    assert_error(post(http, url(http, "lookups", record["lookup_id"], "accept") + query, accept_body()), 422,
                 "INVALID_REQUEST")
    assert http.adapter.calls == 1


@pytest.mark.parametrize("query", [
    "?limit=0", "?limit=21", "?limit=01", "?limit=1&limit=2", "?owner=x", "?before=!!", "?before=" + "A" * 129,
    "?before=bm90LWEtY3Vyc29y", "?before=", "?limit=",
])
def test_list_queries_are_bounded(http, query):
    for target in (url(http, "lookups"), url(http, "decisions")):
        assert_error(http.get(target + query), 422, "INVALID_REQUEST")


@pytest.mark.parametrize("bad", ["not-a-uuid", "0" * 36, str(uuid4()).replace("-", "")])
def test_malformed_path_identifiers_are_not_found(http, bad):
    record = found(http)
    assert_error(http.get(url(http, "lookups", work_id=bad)), 404, "NOT_FOUND")
    assert_error(http.get(url(http, "lookups", bad)), 404, "NOT_FOUND")
    assert_error(accept(http, bad), 404, "NOT_FOUND")
    assert_error(lookup(http, work_id=bad), 404, "NOT_FOUND")
    assert record and http.adapter.calls == 1


# -- G5 body bytes, UTF-8, escapes and stored-result limits (MAIN 3, 9) ---------------------------

def test_body_byte_limit_is_independent_of_validity(http):
    base = json.dumps(lookup_body()).encode("utf-8")
    exact = base[:-1] + b" " * (MAX_BODY_BYTES - len(base)) + b"}"
    assert len(exact) == MAX_BODY_BYTES
    assert post_raw(http, url(http, "lookups"), exact).status_code == 201
    over = base[:-1] + b" " * (MAX_BODY_BYTES + 1 - len(base)) + b"}"
    # A valid command one byte over the transport limit still gets 413.
    assert_error(post_raw(http, url(http, "lookups"), over), 413, "REQUEST_TOO_LARGE")
    request = http.build_request("POST", url(http, "lookups"), content=iter([over[:20000], over[20000:]]),
                                 headers={**business_headers(http), "Content-Type": "application/json"})
    request.headers.pop("transfer-encoding", None)
    request.headers.pop("content-length", None)
    assert_error(http.send(request), 413, "REQUEST_TOO_LARGE")
    assert http.adapter.calls == 1


def maximal(char):
    selection = {"selection_path": [char * 200] * 8, "source_id": char * 100}
    return lookup_body(vehicle_facts={"make": char * 100, "model": char * 100, "engine_code": char * 40,
                                      "fuel": char * 40, "model_year": 2015, "power_kw": 2000,
                                      "displacement_cc": 20000},
                       work_query=char * 200, vehicle_selection=selection, operation_selection=selection,
                       variant_selection=selection)


@pytest.mark.parametrize("char,minimum", [("a", 5000), ("ä", 10000), ("€", 16000), ("\U0001d538", 22000)])
def test_maximal_raw_utf8_commands_fit_the_byte_limit(http, char, minimum):
    content = json.dumps(maximal(char), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert minimum < len(content) < MAX_BODY_BYTES
    assert post_raw(http, url(http, "lookups"), content).status_code == 201


def test_escaped_control_characters_can_exceed_the_byte_limit(http):
    escaped = json.dumps(maximal("\x01"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert b"\\u0001" in escaped and len(escaped) > MAX_BODY_BYTES
    assert_error(post_raw(http, url(http, "lookups"), escaped), 413, "REQUEST_TOO_LARGE")
    small = json.dumps(lookup_body(vehicle_selection={"selection_path": ["\x01" * 10]})).encode("utf-8")
    assert post_raw(http, url(http, "lookups"), small).status_code == 201
    assert http.adapter.calls == 1


def test_a_result_beyond_the_limits_is_site_changed_and_not_acceptable(http):
    fleet = tuple(FakeVehicle(choice=choice(f"Ylimerkki {index}"), operations=VEHICLE.operations)
                  for index in range(51))
    rewire(http, ENABLED, LookupRunner(FakeAutodataLabourAdapter(fleet), FAKE))
    record = found(http)
    assert (record["outcome"], record["candidate_count"], record["result"]["candidates"]) == ("SITE_CHANGED", 0, [])
    assert "Ylimerkki" not in dump(http)
    assert_error(accept(http, record["lookup_id"]), 409, "NOT_ACCEPTABLE")


# -- G6 error mapping by class (D7) ------------------------------------------------------------------

@pytest.mark.parametrize("error,status,code", [
    (WorkNotFoundError(SECRET), 404, "NOT_FOUND"),
    (LookupNotFoundError(SECRET), 404, "NOT_FOUND"),
    (PermissionError(SECRET), 403, "PERMISSION_DENIED"),
    (WorkshopAccessDenied(SECRET), 403, "PERMISSION_DENIED"),
    (AuthenticationError(SECRET), 401, "AUTHENTICATION_REQUIRED"),
    (IdempotencyConflictError(SECRET), 409, "IDEMPOTENCY_CONFLICT"),
    (StaleViewError(SECRET), 409, "STALE_VIEW"),
    (StaleLookupError(SECRET), 409, "STALE_LOOKUP"),
    (ExpiredLookupError(SECRET), 409, "EXPIRED"),
    (InvalidLookupTimeError(SECRET), 409, "INVALID_LOOKUP_TIME"),
    (AutodataClosedError(SECRET), 409, "AUTODATA_CLOSED"),
    (NotAcceptableError(SECRET), 409, "NOT_ACCEPTABLE"),
    (SyntheticNotAcceptableError(SECRET), 409, "SYNTHETIC_NOT_ACCEPTABLE"),
    (AlreadyAcceptedError(SECRET), 409, "ALREADY_ACCEPTED"),
    (RequestRedactedError(SECRET), 409, "REQUEST_REDACTED"),
    (LookupRedactedError(SECRET), 409, "LOOKUP_REDACTED"),
    (LookupBusyError(), 409, "LOOKUP_BUSY"),
    (IdentifierInSearchTextError(SECRET), 422, "IDENTIFIER_IN_SEARCH_TEXT"),
    (IdentifierInRequestError(SECRET), 422, "IDENTIFIER_IN_REQUEST"),
    (UnpriceableLabourTimeError(SECRET), 422, "UNPRICEABLE_LABOUR"),
    (AutodataDisabledError(), 503, "AUTODATA_DISABLED"),
    (AutodataIntegrityError(SECRET), 500, "AUTODATA_INTEGRITY"),
    (sqlite3.OperationalError(SECRET), 503, "AUTODATA_UNAVAILABLE"),
    (sqlite3.IntegrityError(SECRET), 503, "AUTODATA_UNAVAILABLE"),
    # An invariant ValueError is a server error, never a client 422.
    (ValueError(SECRET), 500, "AUTODATA_ERROR"),
    (TypeError(SECRET), 500, "AUTODATA_ERROR"),
    (RuntimeError(SECRET), 500, "AUTODATA_ERROR"),
])
@pytest.mark.parametrize("route", ["lookup", "accept"])
def test_service_errors_map_by_class_without_their_text(http, monkeypatch, caplog, error, status, code, route):
    def fail(*args, **kwargs):
        raise error

    if route == "lookup":
        monkeypatch.setattr(http.app.state.autodata_lookups, "lookup", fail)
        response = lookup(http)
    else:
        monkeypatch.setattr(http.app.state.autodata_acceptance, "accept", fail)
        response = accept(http, str(uuid4()))
    assert_error(response, status, code)
    assert SECRET not in response.text and SECRET not in caplog.text


def test_rate_limit_error_carries_retry_after(http, monkeypatch):
    def fail(*args, **kwargs):
        raise LookupRateLimitedError(17)

    monkeypatch.setattr(http.app.state.autodata_lookups, "lookup", fail)
    response = lookup(http)
    assert_error(response, 429, "LOOKUP_RATE_LIMITED")
    assert response.headers["retry-after"] == "17"


def test_real_conflicts_and_client_errors_through_the_services(http):
    assert_error(lookup(http, lookup_body(expected_version=2)), 409, "STALE_VIEW")
    assert_error(lookup(http, lookup_body(work_query="abc 123 jarrut")), 422, "IDENTIFIER_IN_SEARCH_TEXT")
    assert_error(lookup(http, lookup_body(vehicle_facts={"model": "Testimalli ABC-123"})), 422,
                 "IDENTIFIER_IN_REQUEST")
    assert http.adapter.calls == 0
    record = found(http)
    rewire(http, NO_SYNTHETIC, LookupRunner(http.adapter, FAKE))
    assert_error(accept(http, record["lookup_id"]), 409, "SYNTHETIC_NOT_ACCEPTABLE")
    rewire(http, ENABLED, LookupRunner(http.adapter, FAKE))
    http.app.state.autodata_repository.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
    assert_error(accept(http, record["lookup_id"]), 409, "EXPIRED")
    http.app.state.autodata_repository.clock = lambda: datetime.now(timezone.utc) - timedelta(hours=1)
    assert_error(accept(http, record["lookup_id"]), 409, "INVALID_LOOKUP_TIME")
    http.app.state.autodata_repository.clock = lambda: datetime.now(timezone.utc)
    assert_error(accept(http, str(uuid4())), 404, "NOT_FOUND")
    redact(http, record["lookup_id"])
    assert_error(accept(http, record["lookup_id"]), 409, "LOOKUP_REDACTED")
    close(http)
    assert_error(lookup(http), 409, "AUTODATA_CLOSED")
    assert_error(accept(http, record["lookup_id"]), 409, "AUTODATA_CLOSED")


def test_a_manual_labour_entry_makes_the_lookup_stale(http):
    record = found(http)
    response = http.put(f"/api/works/{http.work['id']}/labour",
                        json={"version": 1, "hours": "1.5", "work_description": "Käsin kirjattu"})
    assert response.status_code == 200 and response.json()["version"] == 2
    assert_error(accept(http, record["lookup_id"], accept_body(expected_version=2)), 409, "STALE_LOOKUP")


def test_another_inquirys_lookup_is_not_found(http):
    record = found(http)
    other = new_inquiry(http, "DEF-456")
    assert_error(http.get(url(http, "lookups", record["lookup_id"], work_id=other["id"])), 404, "NOT_FOUND")
    assert_error(accept(http, record["lookup_id"], work_id=other["id"]), 404, "NOT_FOUND")
    assert_error(http.get(url(http, "lookups", work_id=str(uuid4()))), 404, "NOT_FOUND")


# -- G7 disabled (MAIN 5) ---------------------------------------------------------------------------------

def prepared(http):
    """While enabled: a kept lookup, a lookup to redact and an accepted receipt."""
    kept_body, redacted_body, accept_once = lookup_body(), lookup_body(), accept_body()
    kept = found(http, kept_body)
    redacted = found(http, redacted_body)
    decision = accept(http, kept["lookup_id"], accept_once)
    assert decision.status_code == 200, decision.text
    redact(http, redacted["lookup_id"])
    rewire(http, DISABLED)
    return SimpleNamespace(kept_body=kept_body, kept=kept, redacted_body=redacted_body, accept_body=accept_once,
                           decision=decision.json()["decision"])


def test_disabled_replays_a_kept_lookup_and_refuses_a_new_one(http):
    state = prepared(http)
    calls = http.adapter.calls
    replay = lookup(http, state.kept_body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["lookup"]["lookup_id"] == state.kept["lookup_id"]
    assert_error(lookup(http), 503, "AUTODATA_DISABLED")
    assert_error(lookup(http, state.redacted_body), 409, "REQUEST_REDACTED")
    assert http.adapter.calls == calls


def test_disabled_closure_removes_the_lookup_but_keeps_the_receipt(http):
    state = prepared(http)
    close(http)
    # The closure deleted the stored body: no replay is left, and no new lookup runs.
    assert_error(lookup(http, state.kept_body), 503, "AUTODATA_DISABLED")
    replay = accept(http, state.kept["lookup_id"], state.accept_body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["decision"] == state.decision


def test_disabled_acceptance_without_receipt_is_503_while_get_is_404(http):
    state = prepared(http)
    unknown = str(uuid4())
    assert_error(accept(http, unknown), 503, "AUTODATA_DISABLED")
    assert_error(http.get(url(http, "lookups", unknown)), 404, "NOT_FOUND")
    assert_error(accept(http, state.kept["lookup_id"], accept_body(expected_version=2)), 503, "AUTODATA_DISABLED")
    # Body validation and scope still come first.
    assert_error(accept(http, unknown, {"confirmation": CONFIRM}), 422, "INVALID_REQUEST")
    assert_error(lookup(http, work_id=str(uuid4())), 404, "NOT_FOUND")
    anonymous = TestClient(http.app, base_url="https://testserver")
    assert anonymous.post(url(http, "lookups"), json=lookup_body()).status_code == 401


def test_disabled_reads_work_and_report_the_mode(http):
    state = prepared(http)
    listing = http.get(url(http, "lookups"))
    assert listing.status_code == 200 and listing.json()["autodata_enabled"] is False
    assert {item["lookup_id"] for item in listing.json()["items"]} >= {state.kept["lookup_id"]}
    assert http.get(url(http, "lookups", state.kept["lookup_id"])).status_code == 200
    assert http.get(url(http, "decisions")).json()["items"][0]["decision_id"] == state.decision["decision_id"]


def test_disabled_replays_keep_current_rights(http):
    state = prepared(http)
    login(http, UserRole.IT_ADMIN)
    assert lookup(http, state.kept_body).status_code == 403
    assert accept(http, state.kept["lookup_id"], state.accept_body).status_code == 403


# -- G8 replays ------------------------------------------------------------------------------------------

def test_lookup_replay_is_200_with_the_stored_lookup_even_after_a_version_change(http):
    body = lookup_body()
    first = lookup(http, body)
    assert first.status_code == 201 and first.json()["replayed"] is False
    assert accept(http, first.json()["lookup"]["lookup_id"]).status_code == 200
    again = post(http, url(http, "lookups", work_id=http.work["id"].upper()), body)
    assert again.status_code == 200 and again.json() == {"replayed": True, "lookup": first.json()["lookup"]}
    assert again.json()["lookup"]["source_work_version"] == 1 and http.adapter.calls == 1
    assert_error(lookup(http, {**body, "work_query": "etujarrulevyt"}), 409, "IDEMPOTENCY_CONFLICT")


def test_acceptance_replay_returns_the_same_decision_and_conflicts_on_change(http):
    record = found(http)
    body = accept_body()
    first = accept(http, record["lookup_id"], body)
    again = accept(http, record["lookup_id"], body)
    assert first.status_code == again.status_code == 200
    assert (first.json()["replayed"], again.json()["replayed"]) == (False, True)
    assert again.json()["decision"] == first.json()["decision"]
    assert first.json()["decision"]["resulting_work_version"] == 2
    assert first.json()["work"]["labour_source"] == "AUTODATA"
    assert_error(accept(http, record["lookup_id"], {**body, "expected_version": 2}), 409, "IDEMPOTENCY_CONFLICT")


def test_another_user_with_the_same_key_makes_a_separate_request(http):
    body = lookup_body()
    first = found(http, body)
    login(http)
    second = lookup(http, body)
    assert second.status_code == 201 and second.json()["lookup"]["lookup_id"] != first["lookup_id"]
    assert http.adapter.calls == 2


# -- G9 quota (MAIN 1) -------------------------------------------------------------------------------------

def test_a_full_quota_gives_429_while_replays_and_disabled_keep_their_precedence(http, quota):
    body = lookup_body()
    found(http, body)
    for _ in range(59):
        quota.consume(WORKSHOP_KEY)
    response = lookup(http)
    assert_error(response, 429, "LOOKUP_RATE_LIMITED")
    assert int(response.headers["retry-after"]) >= 1
    assert http.adapter.calls == 1 and quota.used(WORKSHOP_KEY) == 60
    assert lookup(http, body).status_code == 200
    rewire(http, DISABLED)
    assert_error(lookup(http), 503, "AUTODATA_DISABLED")
    assert quota.used(WORKSHOP_KEY) == 60


def test_validation_failures_spend_no_start_and_calls_spend_one(http, quota):
    lookup(http, lookup_body(expected_version=2))
    lookup(http, lookup_body(work_query="abc 123"))
    lookup(http, {**lookup_body(), "hours": "1"})
    assert quota.used(WORKSHOP_KEY) == 0
    found(http)
    assert quota.used(WORKSHOP_KEY) == 1 == http.adapter.calls


def test_a_start_is_not_refunded_when_rights_are_lost_during_the_call(http, quota):
    service = http.app.state.auth_service
    http.adapter.during = lambda: service.workshop_directory.revoke_membership(
        http.grant.user.user_id, TEST_WORKSHOP, operator_reference="Keksitty kesken haun")
    assert_error(lookup(http), 403, "PERMISSION_DENIED")
    assert quota.used(WORKSHOP_KEY) == 1 and http.adapter.calls == 1


# -- G10 concurrency -----------------------------------------------------------------------------------------

def test_a_held_process_lock_answers_lookup_busy_without_the_adapter(http, quota):
    assert PROCESS_LOOKUP_LOCK.acquire(blocking=False)
    try:
        assert_error(lookup(http), 409, "LOOKUP_BUSY")
        assert PROCESS_LOOKUP_LOCK.locked()
    finally:
        PROCESS_LOOKUP_LOCK.release()
    assert http.adapter.calls == 0 and quota.used(WORKSHOP_KEY) == 0


def test_the_same_acceptance_key_twice_in_parallel_is_one_decision_and_one_replay(http):
    record = found(http)
    body, ready = accept_body(), threading.Barrier(2)
    headers = business_headers(http)

    def send(_):
        ready.wait(timeout=5)
        return http.post(url(http, "lookups", record["lookup_id"], "accept"), json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert [response.status_code for response in results] == [200, 200]
    assert sorted(response.json()["replayed"] for response in results) == [False, True]
    assert len({response.json()["decision"]["decision_id"] for response in results}) == 1
    assert len(http.get(url(http, "decisions")).json()["items"]) == 1


def test_different_acceptance_keys_in_parallel_have_exactly_one_winner(http):
    record = found(http)
    ready, headers = threading.Barrier(2), business_headers(http)

    def send(_):
        ready.wait(timeout=5)
        return http.post(url(http, "lookups", record["lookup_id"], "accept"), json=accept_body(), headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert sorted(response.status_code for response in results) == [200, 409]
    loser = next(response for response in results if response.status_code == 409)
    # The version already rose, and the version is checked before ALREADY_ACCEPTED.
    assert loser.json()["code"] == "STALE_LOOKUP"
    assert len(http.get(url(http, "decisions")).json()["items"]) == 1


# -- G11 lifecycle (MAIN 4) ----------------------------------------------------------------------------------

@pytest.fixture
def initializations(monkeypatch):
    calls = []
    for owner in (WorkRepository, AuthRepository):
        original = owner.initialize

        def counted(self, _original=original, _name=owner.__name__):
            calls.append(_name)
            return _original(self)
        monkeypatch.setattr(owner, "initialize", counted)
    return calls


def start(tmp_path, **options):
    app = create_test_app(tmp_path / "works.db", **options)
    with TestClient(app, base_url="https://testserver"):
        return app.state


class SettingsSubclass(AutodataSettings):
    pass


class RunnerSubclass(LookupRunner):
    pass


class FakeSubclass(FakeAutodataLabourAdapter):
    pass


@pytest.mark.parametrize("options,error", [
    ({"autodata_settings": {}}, TypeError),
    ({"autodata_settings": False}, TypeError),
    ({"autodata_settings": SettingsSubclass()}, TypeError),
    ({"autodata_settings": ENABLED}, TypeError),
    ({"autodata_settings": ENABLED, "autodata_runner": object()}, TypeError),
    ({"autodata_settings": DISABLED, "autodata_runner": "runner"}, ValueError),
])
def test_wrong_settings_or_runner_stop_startup_before_any_initialize(tmp_path, initializations, options, error):
    if options.get("autodata_runner") == "runner":
        options["autodata_runner"] = LookupRunner(counting_fake(), FAKE)
    with pytest.raises(error):
        start(tmp_path, **options)
    assert initializations == []
    assert not (tmp_path / "works.db").exists() and not (tmp_path / "works.db.auth.db").exists()


@pytest.mark.parametrize("runner,error", [
    (lambda: RunnerSubclass(counting_fake(), FAKE), TypeError),
    (lambda: LookupRunner(FakeSubclass((VEHICLE,)), FAKE), ValueError),
    (lambda: LookupRunner(counting_fake(), AdapterIdentity("fake", "fake-2", True)), ValueError),
    (lambda: LookupRunner(counting_fake(), AdapterIdentity("fake", FAKE_ADAPTER_VERSION, False)), ValueError),
])
def test_a_wrong_runner_class_adapter_or_identity_stops_startup_before_any_initialize(
        tmp_path, initializations, runner, error):
    with pytest.raises(error):
        start(tmp_path, autodata_settings=ENABLED, autodata_runner=runner())
    assert initializations == [] and not (tmp_path / "works.db").exists()


@pytest.mark.parametrize("value", ["fake", "browser", "FAKE", ""])
def test_the_environment_cannot_enable_autodata(tmp_path, monkeypatch, initializations, value):
    monkeypatch.setenv("CARBIT_AUTODATA_MODE", value)
    with pytest.raises(AutodataConfigurationError):
        start(tmp_path)
    assert initializations == []


@pytest.mark.parametrize("worker", ["parent", "web_concurrency"])
def test_an_enabled_mode_refuses_a_worker_child_but_disabled_starts(tmp_path, monkeypatch, initializations, worker):
    if worker == "parent":
        monkeypatch.setattr("multiprocessing.parent_process", lambda: object())
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(AutodataConfigurationError):
        start(tmp_path, autodata_settings=ENABLED, autodata_runner=LookupRunner(counting_fake(), FAKE))
    assert initializations == []
    monkeypatch.delenv("CARBIT_AUTODATA_MODE", raising=False)
    state = start(tmp_path)
    assert state.autodata_settings == DISABLED and initializations


def test_default_startup_is_disabled_and_keeps_the_quota(tmp_path, monkeypatch, quota):
    monkeypatch.delenv("CARBIT_AUTODATA_MODE", raising=False)
    for _ in range(3):
        quota.consume(WORKSHOP_KEY)
    state = start(tmp_path)
    assert state.autodata_settings == DISABLED and state.autodata_lookups.runner is None
    assert isinstance(state.autodata_lookups, AutodataLookupService)
    # Neither the lifespan nor building the services resets the process quota.
    assert autodata_service.PROCESS_LOOKUP_QUOTA is quota and quota.used(WORKSHOP_KEY) == 3


def test_creating_the_app_opens_no_database(tmp_path):
    create_test_app(tmp_path / "works.db", autodata_settings=ENABLED,
                    autodata_runner=LookupRunner(counting_fake(), FAKE))
    assert list(tmp_path.iterdir()) == []


def test_missing_services_answer_unavailable(http):
    record = found(http)
    http.app.state.autodata_lookups = None
    for method, target, body in operations(http, record["lookup_id"]):
        response = http.request(method, target, json=body, headers=business_headers(http))
        assert_error(response, 503, "AUTODATA_UNAVAILABLE")


# -- G12 response projections (MAIN 6) -------------------------------------------------------------------------

INTERNAL = {"request_id", "requested_by", "decided_by", "auth_store_id", "requested_by_attribution",
            "decided_by_attribution"}


def test_responses_carry_names_but_no_internal_actor_identifiers(http):
    response = lookup(http)
    record = response.json()["lookup"]
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    assert not INTERNAL & set(record)
    assert (record["requested_by_name"], record["requested_by_me"]) == ("Fictional HTTP test user", True)
    accepted = accept(http, record["lookup_id"])
    assert accepted.status_code == 200 and set(accepted.json()) == {"decision", "replayed", "work"}
    login(http)  # another staff member of the same workshop and auth store
    single = http.get(url(http, "lookups", record["lookup_id"])).json()
    assert not INTERNAL & set(single)
    assert (single["requested_by_name"], single["requested_by_me"]) == ("Fictional HTTP test user", False)
    decision = http.get(url(http, "decisions")).json()["items"][0]
    assert not INTERNAL & set(decision)
    assert (decision["decided_by_name"], decision["decided_by_me"]) == ("Fictional HTTP test user", False)
    for text in (http.get(url(http, "lookups")).text, http.get(url(http, "decisions")).text):
        assert http.grant.user.user_id.__str__() not in text


def test_an_actor_of_another_auth_store_gets_no_name_and_is_never_me():
    caller = SimpleNamespace(auth_store_id="store-a", employee_id="user-1")
    names = {"user-1": "Nykyinen nimi"}
    assert actor_view(caller, names, "store-a", "user-1", "decided_by") == {
        "decided_by_name": "Nykyinen nimi", "decided_by_me": True}
    assert actor_view(caller, names, "store-b", "user-1", "decided_by") == {
        "decided_by_name": None, "decided_by_me": False}
    assert actor_view(caller, names, None, "user-1", "requested_by") == {
        "requested_by_name": None, "requested_by_me": False}
    asked = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_service=SimpleNamespace(
        display_names=lambda store, ids: asked.append((store, set(ids))) or {}))))
    assert display_names(request, caller, [("store-b", "user-2")]) == {} and asked == []
    display_names(request, caller, [("store-a", "user-1"), ("store-b", "user-2")])
    assert asked == [("store-a", {"user-1"})]


def test_a_name_lookup_outage_only_leaves_names_out(http, monkeypatch):
    record = found(http)

    def outage(*args, **kwargs):
        raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(http.app.state.auth_service, "display_names", outage)
    response = http.get(url(http, "lookups", record["lookup_id"]))
    assert response.status_code == 200 and response.json()["requested_by_name"] is None
    assert SECRET not in response.text


def test_an_unexpected_repository_field_is_a_server_error(http, monkeypatch):
    record = found(http)
    original = http.app.state.autodata_repository.get_lookup
    monkeypatch.setattr(http.app.state.autodata_repository, "get_lookup",
                        lambda *args, **kwargs: {**original(*args, **kwargs), "unexpected": SECRET})
    response = http.get(url(http, "lookups", record["lookup_id"]))
    assert_error(response, 500, "AUTODATA_ERROR")
    assert SECRET not in response.text


# -- pagination -----------------------------------------------------------------------------------------------------

def test_cursors_page_lookups_and_decisions_without_overlap(http):
    ids = {found(http)["lookup_id"] for _ in range(3)}
    first = http.get(url(http, "lookups") + "?limit=2").json()
    assert len(first["items"]) == 2 and first["next"] is not None and first["autodata_enabled"] is True
    second = http.get(url(http, "lookups") + "?limit=2&before=" + first["next"]).json()
    assert len(second["items"]) == 1 and second["next"] is None
    assert {item["lookup_id"] for item in first["items"] + second["items"]} == ids
    version = 1
    for _ in range(2):
        fresh = found(http, lookup_body(expected_version=version))
        assert accept(http, fresh["lookup_id"], accept_body(expected_version=version)).status_code == 200
        version += 1
    page = http.get(url(http, "decisions") + "?limit=1").json()
    rest = http.get(url(http, "decisions") + "?limit=1&before=" + page["next"]).json()
    assert len(page["items"]) == len(rest["items"]) == 1
    assert page["items"][0]["decision_id"] != rest["items"][0]["decision_id"]
