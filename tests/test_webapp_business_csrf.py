"""Raw HTTPS clients verify the business boundary without automatic proof injection."""

import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from http_auth_support import business_headers, create_test_app, grant_session, settings_for
from webapp.auth_csrf import MFA_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE
from webapp.auth_models import UserRole
from webapp.http_access import WRITE_ROLES


ORIGIN = "https://testserver"


@pytest.fixture
def http(tmp_path):
    with TestClient(create_test_app(tmp_path / "works.db"), base_url=ORIGIN) as client:
        service = client.app.state.auth_service
        client.now = datetime.now(timezone.utc).replace(microsecond=0)
        service.clock = lambda: client.now
        grant = grant_session(service)
        client.cookies.set(SESSION_COOKIE, grant.token.get_secret_value())
        client.grant = grant
        yield client


@pytest.fixture(params=["create", "labour"])
def write(http, request):
    if request.param == "create":
        return "POST", "/api/works", dict(registration="TEST-CSRF", customer_description="Fictional",
                                          actor="Untrusted legacy label", idempotency_key=str(uuid4()))
    work = http.app.state.repository.create_work(registration="TEST-CSRF", customer_description="Fictional",
                                                actor="Offline fixture", idempotency_key=str(uuid4()))
    return "PUT", f"/api/works/{work['id']}/labour", dict(version=1, hours="1.60",
                work_description="Fictional Autodata time", recorded_by="Untrusted legacy label")


def snapshot(http):
    with http.app.state.repository._connect() as connection:
        return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                for table in ("work_inquiries", "work_events", "scoped_inquiry_creation_requests")}


def session_activity(http):
    with http.app.state.auth_service.repository._connect() as connection:
        return dict(connection.execute("SELECT session_id,last_seen_at FROM auth_sessions"))


def invalidate_session(http, state):
    service = http.app.state.auth_service
    if state == "revoked":
        service.revoke_session(http.cookies.get(SESSION_COOKIE))
    elif state in {"idle", "absolute"}:
        http.now += timedelta(seconds=1800 if state == "idle" else 28800)
    else:
        changes = {"role": "role='IT_ADMIN'", "disabled": "enabled=0",
                   "version": "auth_version=auth_version+1",
                   "mfa": "mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL"}
        with service.repository.transaction() as connection:
            connection.execute("UPDATE auth_users SET " + changes[state])


def rejected(http, write, headers, status, *, body=None):
    before = snapshot(http)
    activity = session_activity(http)
    method, path, payload = write
    response = http.request(method, path, headers=headers, json=payload) if body is None else http.request(
        method, path, headers=headers, content=body)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert not response.headers.get_list("set-cookie")
    assert snapshot(http) == before
    assert session_activity(http) == activity
    return response


def test_valid_bootstrap_proof_allows_write_and_remains_reusable(http, write):
    response = http.get("/api/auth/csrf", headers={"X-Carbit-Auth": "bootstrap"})
    assert response.status_code == 200 and response.json()["state"] == "session"
    headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
    method, path, payload = write
    first = http.request(method, path, headers=headers, json=payload)
    assert first.status_code == (201 if method == "POST" else 200)
    # CSRF proof reuse does not replace idempotency or optimistic versions.
    second = http.request(method, path, headers=headers, json=payload)
    assert second.status_code == (201 if method == "POST" else 409)
    assert len(snapshot(http)["work_events"]) == (1 if method == "POST" else 2)


@pytest.mark.parametrize("origin", [None, "null", ORIGIN + "/", ORIGIN.upper(), "http://testserver", "https://evil.example", ORIGIN + ", " + ORIGIN])
def test_exact_origin_required(http, write, origin):
    headers = business_headers(http)
    headers.pop("Origin")
    if origin is not None:
        headers["Origin"] = origin
    rejected(http, write, headers, 403)


@pytest.mark.parametrize("proof", [None, "", "fictional", "A" * 513])
def test_missing_malformed_proof_precedes_body_validation(http, write, proof):
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    if proof is not None:
        headers["X-CSRF-Token"] = proof
    rejected(http, write, headers, 403, body=b"invalid-json")


def test_tampered_and_expired_proofs_do_not_touch_idle_time(http, write):
    headers = business_headers(http)
    service = http.app.state.auth_service
    token = http.cookies.get(SESSION_COOKIE)
    initial = service.inspect_session(token)
    http.now += timedelta(seconds=100)
    fields = headers["X-CSRF-Token"].split(".")
    fields[5] = secrets.token_urlsafe(16)
    rejected(http, write, {**headers, "X-CSRF-Token": ".".join(fields)}, 403)
    assert service.inspect_session(token).idle_expiry == initial.idle_expiry
    http.now += timedelta(seconds=500)
    rejected(http, write, headers, 403)
    assert service.inspect_session(token).idle_expiry == initial.idle_expiry


@pytest.mark.parametrize("purpose", ["preauth", "mfa"])
def test_anonymous_purposes_are_not_session_proofs(http, write, purpose):
    protector = http.app.state.csrf_protector
    context = protector.create_preauth(ORIGIN).value.get_secret_value()
    bindings = {"preauth_cookie": context}
    if purpose == "mfa":
        bindings["mfa_cookie"] = protector.seal_mfa(context, secrets.token_urlsafe(32), ORIGIN,
            challenge_expires_at=http.now + timedelta(seconds=300)).value.get_secret_value()
    proof = protector.issue_proof(purpose, ORIGIN, **bindings)
    rejected(http, write, {"Origin": ORIGIN, "X-CSRF-Token": proof}, 403)


def test_proof_cannot_move_between_sessions(http, write):
    headers = business_headers(http)
    other = grant_session(http.app.state.auth_service)
    http.cookies.set(SESSION_COOKIE, other.token.get_secret_value())
    rejected(http, write, headers, 403)


@pytest.mark.parametrize("name", ["Origin", "Host", "X-CSRF-Token", "X-Carbit-Auth", "Content-Type", "Content-Length", "Authorization", "Cookie"])
def test_duplicate_security_headers_rejected(http, write, name):
    headers = business_headers(http)
    values = {**headers, "Host": "testserver", "Content-Type": "application/json", "Content-Length": "2",
              "X-Carbit-Auth": "bootstrap", "Authorization": "fictional",
              "Cookie": SESSION_COOKIE + "=" + http.cookies.get(SESSION_COOKIE)}
    pairs = [(key, value) for key, value in headers.items() if key != name] + [(name, values[name])] * 2
    rejected(http, write, pairs, 403 if name == "Origin" else 400, body=b"{}")


@pytest.mark.parametrize("cookie,status", [
    (SESSION_COOKIE + "=a; " + SESSION_COOKIE + "=b", 400),
    (SESSION_COOKIE + '="quoted"', 400), (SESSION_COOKIE + "=", 400),
    (PREAUTH_COOKIE + "=a; " + PREAUTH_COOKIE + "=b", 400),
    ("broken", 400),
    ("mixed-preauth", 409), ("mixed-mfa", 409),
])
def test_ambiguous_and_malformed_cookies(http, write, cookie, status):
    headers = business_headers(http)
    if cookie.startswith("mixed-"):
        name = PREAUTH_COOKIE if cookie == "mixed-preauth" else MFA_COOKIE
        cookie = SESSION_COOKIE + "=" + http.cookies.get(SESSION_COOKIE) + "; " + name + "=fictional"
    rejected(http, write, {**headers, "Cookie": cookie}, status)


@pytest.mark.parametrize("name,value", [("Authorization", "Bearer fictional"), ("Transfer-Encoding", "chunked")])
def test_unsupported_security_headers(http, write, name, value):
    rejected(http, write, {**business_headers(http), name: value}, 400)


@pytest.mark.parametrize("name,value", [("Content-Length", "2, 2"), ("Content-Length", "-1"),
    ("Content-Length", "fictional"), ("Content-Type", "application/json, application/json")])
def test_combined_or_malformed_framing_headers(http, write, name, value):
    rejected(http, write, {**business_headers(http), name: value}, 400, body=b"{}")


@pytest.mark.parametrize("state", ["revoked", "idle", "absolute", "disabled", "version"])
def test_session_invalidity_overrides_possession_of_proof(http, write, state):
    headers = business_headers(http)
    service = http.app.state.auth_service
    if state == "revoked":
        service.revoke_session(http.cookies.get(SESSION_COOKIE))
    elif state in {"idle", "absolute"}:
        http.now += timedelta(seconds=1800 if state == "idle" else 28800)
    else:
        with service.repository.transaction() as connection:
            connection.execute("UPDATE auth_users SET " + ("enabled=0" if state == "disabled" else "auth_version=auth_version+1"))
    rejected(http, write, headers, 401)


@pytest.mark.parametrize("failure", ["csrf", "storage", "revoked-after-proof", "role-after-proof",
    "disabled-after-proof", "version-after-proof", "mfa-after-proof", "idle-after-proof", "absolute-after-proof"])
def test_fail_closed_before_mutation(http, write, monkeypatch, failure):
    headers = business_headers(http)
    http.now += timedelta(seconds=60)
    service = http.app.state.auth_service
    original = service.inspect_session
    calls = 0
    def inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "storage":
            raise sqlite3.OperationalError("fictional-sensitive-error")
        if calls == 2:
            invalidate_session(http, failure.removesuffix("-after-proof"))
        return original(*args, **kwargs)
    if failure == "csrf":
        monkeypatch.setattr(http.app.state, "csrf_protector", None)
    else:
        monkeypatch.setattr(service, "inspect_session", inspect)
    response = rejected(http, write, headers, {"csrf": 503, "storage": 503,
        "role-after-proof": 403}.get(failure, 401))
    assert "sensitive" not in response.text


@pytest.mark.parametrize("body", [b"invalid-json", b"{}", b'{"unexpected":true}'])
def test_invalid_business_body_does_not_refresh_idle_time(http, write, body):
    headers = {**business_headers(http), "Content-Type": "application/json"}
    http.now += timedelta(seconds=60)
    rejected(http, write, headers, 422, body=body)


def test_invalid_business_field_does_not_refresh_idle_time(http, write):
    headers = business_headers(http)
    method, _, payload = write
    payload["registration" if method == "POST" else "hours"] = "   " if method == "POST" else "0"
    http.now += timedelta(seconds=60)
    rejected(http, write, headers, 422)


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER])
def test_valid_write_refreshes_idle_time_after_success(http, write, role):
    with http.app.state.auth_service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET role=?", (role.value,))
    headers = business_headers(http)
    before = session_activity(http)
    http.now += timedelta(seconds=60)
    method, path, payload = write
    response = http.request(method, path, headers=headers, json=payload)
    assert response.status_code == (201 if method == "POST" else 200)
    assert session_activity(http) == {key: int(http.now.timestamp()) for key in before}
    assert session_activity(http) != before


def test_business_conflict_does_not_refresh_idle_time(http, write):
    headers = business_headers(http)
    method, path, payload = write
    assert http.request(method, path, headers=headers, json=payload).status_code == (201 if method == "POST" else 200)
    if method == "POST":
        payload["registration"] = "DIFFERENT"
    http.now += timedelta(seconds=60)
    rejected(http, write, headers, 409)


def test_labour_domain_validation_and_missing_work_do_not_refresh(http):
    work = http.app.state.repository.create_work(registration="TEST", customer_description="Fictional",
                                               actor="Offline fixture", idempotency_key=str(uuid4()))
    payload = dict(version=1, hours="0.000001", work_description="Fictional", recorded_by="Legacy")
    headers = business_headers(http)
    http.now += timedelta(seconds=60)
    rejected(http, ("PUT", f"/api/works/{work['id']}/labour", payload), headers, 422)
    payload["hours"] = "1.60"
    rejected(http, ("PUT", "/api/works/missing/labour", payload), headers, 404)


@pytest.mark.parametrize("state", ["role", "revoked", "disabled", "version", "mfa", "idle", "absolute"])
def test_post_success_refresh_rechecks_current_state_without_rewriting_committed_response(http, write, monkeypatch, state):
    headers = business_headers(http)
    service = http.app.state.auth_service
    refresh = service.refresh_session_for_roles
    before = snapshot(http)
    activity = session_activity(http)
    http.now += timedelta(seconds=60)
    called = False
    def change_before_refresh(*args, **kwargs):
        nonlocal called
        called = True
        assert snapshot(http) != before  # The handler has already succeeded.
        invalidate_session(http, state)
        assert refresh(*args, **kwargs) is False
        return False
    monkeypatch.setattr(service, "refresh_session_for_roles", change_before_refresh)
    method, path, payload = write
    response = http.request(method, path, headers=headers, json=payload)
    assert called and response.status_code == (201 if method == "POST" else 200)
    assert response.headers["cache-control"] == "no-store"
    assert session_activity(http) == activity


def test_refresh_storage_failure_preserves_committed_success_without_touch(http, write, monkeypatch, caplog):
    headers = business_headers(http)
    activity = session_activity(http)
    before = snapshot(http)
    http.now += timedelta(seconds=60)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fictional-sensitive-error")
    monkeypatch.setattr(http.app.state.auth_service, "refresh_session_for_roles", unavailable)
    method, path, payload = write
    response = http.request(method, path, headers=headers, json=payload)
    assert response.status_code == (201 if method == "POST" else 200)
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(http) != before and session_activity(http) == activity
    assert "could not refresh session idle time" in caplog.text
    assert "sensitive" not in caplog.text + response.text


@pytest.mark.parametrize("state", ["role", "revoked", "disabled", "version", "mfa", "idle", "absolute"])
def test_refresh_waiting_for_auth_lock_observes_committed_state_and_current_time(http, monkeypatch, state):
    service = http.app.state.auth_service
    activity = session_activity(http)
    business = snapshot(http)
    token = http.cookies.get(SESSION_COOKIE)
    http.now += timedelta(seconds=60)
    attempting = Event()
    transaction = service.repository.transaction
    @contextmanager
    def attempting_transaction():
        attempting.set()
        with transaction() as connection:
            yield connection
    # Main thread holds the real SQLite write lock; the refresh must wait. Change
    # identity/revocation (or advance time) before releasing that lock.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction() as connection:
            monkeypatch.setattr(service.repository, "transaction", attempting_transaction)
            future = pool.submit(service.refresh_session_for_roles, token, allowed_roles=WRITE_ROLES)
            assert attempting.wait(2)
            assert not future.done()
            if state in {"idle", "absolute"}:
                http.now += timedelta(seconds=1800 if state == "idle" else 28800)
            elif state == "revoked":
                connection.execute("UPDATE auth_sessions SET revoked_at=?", (int(http.now.timestamp()),))
            else:
                changes = {"role": "role='IT_ADMIN'", "disabled": "enabled=0",
                           "version": "auth_version=auth_version+1",
                           "mfa": "mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL"}
                connection.execute("UPDATE auth_users SET " + changes[state])
        assert future.result(timeout=3) is False
    assert session_activity(http) == activity and snapshot(http) == business


@pytest.mark.parametrize("change", ["role", "revoked"])
def test_refresh_validation_and_update_hold_one_auth_write_lock(http, monkeypatch, change):
    service = http.app.state.auth_service
    token = http.cookies.get(SESSION_COOKIE)
    http.now += timedelta(seconds=60)
    validated, release, changing = Event(), Event(), Event()
    original = service._session
    def hold_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        assert args[0].in_transaction
        with service.repository._connect() as contender:
            contender.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        validated.set()
        assert release.wait(3)
        return result
    def concurrent_change():
        changing.set()
        with service.repository.transaction() as connection:
            if change == "role":
                connection.execute("UPDATE auth_users SET role='IT_ADMIN'")
            else:
                connection.execute("UPDATE auth_sessions SET revoked_at=?", (int(http.now.timestamp()),))
    monkeypatch.setattr(service, "_session", hold_validation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(service.refresh_session_for_roles, token, allowed_roles=WRITE_ROLES)
        try:
            assert validated.wait(2)
            update = pool.submit(concurrent_change)
            assert changing.wait(2)
            assert not update.done()  # Cannot commit between validation and touch.
        finally:
            release.set()
        assert refresh.result(timeout=3) is True
        update.result(timeout=3)
    activity = session_activity(http)
    assert set(activity.values()) == {int(http.now.timestamp())}
    http.now += timedelta(seconds=60)
    assert service.refresh_session_for_roles(token, allowed_roles=WRITE_ROLES) is False
    assert session_activity(http) == activity


def test_plain_http_and_configured_different_origin_cannot_write(tmp_path):
    settings = replace(settings_for(tmp_path / "auth.db"), allowed_hosts=("testserver", "other.testserver"),
                       external_origins=(ORIGIN, "https://other.testserver"))
    with TestClient(create_test_app(tmp_path / "works.db", auth_settings=settings), base_url=ORIGIN) as http:
        grant = grant_session(http.app.state.auth_service)
        http.cookies.set(SESSION_COOKIE, grant.token.get_secret_value())
        proof = business_headers(http)
        payload = dict(registration="TEST", customer_description="Fictional", actor="Legacy", idempotency_key=str(uuid4()))
        assert http.post("http://testserver/api/works", headers=proof, json=payload).status_code == 400
        assert http.post("https://other.testserver/api/works", headers={**proof,
            "Cookie": SESSION_COOKIE + "=" + grant.token.get_secret_value()}, json=payload).status_code == 403
        # Also check proof binding with an Origin valid for the target itself.
        assert http.post("https://other.testserver/api/works", headers={**proof, "Origin": "https://other.testserver",
            "Cookie": SESSION_COOKIE + "=" + grant.token.get_secret_value()}, json=payload).status_code == 403
        assert http.app.state.repository.list_works() == []


def test_concurrent_writes_reuse_proof_but_keep_existing_transaction_guards(http, write):
    headers = business_headers(http)
    method, path, payload = write
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: http.request(method, path, headers=headers, json=payload), range(2)))
    assert sorted(response.status_code for response in responses) == ([201, 201] if method == "POST" else [200, 409])
    assert len(snapshot(http)["work_events"]) == (1 if method == "POST" else 2)


def test_long_business_text_keeps_existing_contract(http):
    payload = dict(registration="TEST", customer_description="F" * 4000, actor="A" * 200, idempotency_key=str(uuid4()))
    assert len(json.dumps(payload)) > 4096
    assert http.post("/api/works", headers=business_headers(http), json=payload).status_code == 201
