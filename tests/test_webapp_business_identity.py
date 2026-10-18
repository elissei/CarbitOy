"""Authenticated attribution, legacy separation and the token-only workshop bridge."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from http_auth_support import business_headers, create_test_app, grant_session
from test_webapp_labour_service import approval, save_command
from webapp.auth_csrf import SESSION_COOKIE
from webapp.auth_models import UserRole
from webapp.auth_service import AuthenticationError, PermissionDeniedError
from webapp.business_identity import BusinessActor
from webapp.database import IdempotencyConflictError
from webapp.labour_service import LabourPlanService, TrustedWorkshopContext, WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context


@pytest.fixture
def http(tmp_path):
    with TestClient(create_test_app(tmp_path / "works.db"), base_url="https://testserver") as client:
        service = client.app.state.auth_service
        client.now = datetime.now(timezone.utc).replace(microsecond=0)
        service.clock = lambda: client.now
        client.grant = grant_session(service)
        client.cookies.set(SESSION_COOKIE, client.grant.token.get_secret_value())
        yield client


def payload():
    return dict(registration="TEST-123", customer_description="Fictional request", idempotency_key=str(uuid4()))


def create(http, body=None):
    response = http.post("/api/works", headers=business_headers(http), json=body or payload())
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(params=["create", "labour"])
def write(http, request):
    if request.param == "create":
        return "POST", "/api/works", payload()
    work = create(http)
    return "PUT", f"/api/works/{work['id']}/labour", dict(version=1, hours="1.60", work_description="Fictional Autodata")


def persisted(http):
    with http.app.state.repository._connect() as connection:
        return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                for table in ("work_inquiries", "work_events", "scoped_inquiry_creation_requests")}


def activity(http):
    with http.app.state.auth_service.repository._connect() as connection:
        return dict(connection.execute("SELECT session_id,last_seen_at FROM auth_sessions"))


def rename(http, name):
    with http.app.state.auth_service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET display_name=? WHERE user_id=?", (name, str(http.grant.user.user_id)))


def assert_actor(event, user, display_name=None):
    assert event["actor"] == str(user.user_id)
    assert event["details"]["actor_attribution"] == {
        "version": 1, "provenance": "authenticated_session", "user_id": str(user.user_id),
        "display_name": display_name or user.display_name,
    }


@pytest.mark.parametrize("label", ["A different employee", str(uuid4()), '{"version":1,"user_id":"spoofed"}'])
def test_client_labels_cannot_spoof_authoritative_actor(http, write, label):
    method, path, body = write
    body["actor" if method == "POST" else "recorded_by"] = label
    response = http.request(method, path, headers=business_headers(http), json=body)
    assert response.status_code == (201 if method == "POST" else 200)
    event = response.json()["events"][0]
    assert_actor(event, http.grant.user)
    assert label not in json.dumps(event)
    if method == "PUT":
        assert response.json()["labour_recorded_by"] == event["details"]["recorded_by"] == str(http.grant.user.user_id)
        assert response.json()["total_price"] == "144.00"


@pytest.mark.parametrize("value", ["", " " * 2, "A" * 201, {"user_id": "spoofed"}, 123])
def test_deprecated_labels_remain_bounded_validated_strings(http, write, value):
    method, path, body = write
    body["actor" if method == "POST" else "recorded_by"] = value
    headers = business_headers(http)
    before, idle = persisted(http), activity(http)
    http.now += timedelta(seconds=60)
    response = http.request(method, path, headers=headers, json=body)
    assert response.status_code == 422
    assert persisted(http) == before and activity(http) == idle


@pytest.mark.parametrize("field", ["actor_user_id", "actor_attribution", "authenticated_user", "workshop_context", "approved_by", "created_by"])
def test_request_identity_objects_and_extra_actor_fields_are_rejected(http, write, field):
    method, path, body = write
    body[field] = {"user_id": str(uuid4()), "role": "OWNER", "permissions": ["SAVE_LABOUR_PLAN"]}
    before = persisted(http)
    response = http.request(method, path, headers=business_headers(http), json=body)
    assert response.status_code == 422 and persisted(http) == before


@pytest.mark.parametrize("explicit_null", [False, True])
def test_labels_can_be_omitted_or_null_without_losing_attribution(http, write, explicit_null):
    method, path, body = write
    if explicit_null:
        body["actor" if method == "POST" else "recorded_by"] = None
    response = http.request(method, path, headers=business_headers(http), json=body)
    assert response.status_code == (201 if method == "POST" else 200)
    assert_actor(response.json()["events"][0], http.grant.user)


def test_uuid_stays_stable_and_historical_display_snapshots_are_preserved(http):
    work = create(http)
    original = work["events"][0]
    rename(http, "Fictional renamed employee")
    response = http.put(f"/api/works/{work['id']}/labour", headers=business_headers(http),
                        json=dict(version=1, hours="1.60", work_description="Fictional Autodata", recorded_by="Spoofed"))
    assert response.status_code == 200
    events = response.json()["events"]
    assert events[1] == original
    assert_actor(events[1], http.grant.user)
    assert_actor(events[0], http.grant.user, "Fictional renamed employee")
    rename(http, "Another fictional name")
    assert http.get(f"/api/works/{work['id']}").json()["events"] == events


def test_same_user_retry_ignores_mutable_names_and_labels_and_returns_original_snapshot(http):
    body = payload()
    original = create(http, {**body, "actor": "Untrusted first label"})
    rename(http, "Fictional changed display name")
    # Update the resource: creation replay still returns the original response.
    response = http.put(f"/api/works/{original['id']}/labour", headers=business_headers(http),
                        json=dict(version=1, hours="1.60", work_description="Fictional Autodata"))
    assert response.status_code == 200
    before = persisted(http)
    assert create(http, {**body, "actor": "Untrusted changed label"}) == original
    assert persisted(http) == before
    with http.app.state.repository._connect() as connection:
        fingerprint = json.loads(connection.execute("SELECT request_json FROM scoped_inquiry_creation_requests").fetchone()[0])
    assert fingerprint == {"contract": "carbit.create-work.authenticated.v1", "actor_user_id": str(http.grant.user.user_id),
                           "registration": body["registration"], "customer_description": body["customer_description"]}


def test_cross_user_key_reuse_conflicts_even_with_identical_display_name(http):
    body = payload()
    first = create(http, body)
    other = grant_session(http.app.state.auth_service)
    assert other.user.display_name == http.grant.user.display_name
    http.cookies.set(SESSION_COOKIE, other.token.get_secret_value())
    before, idle = persisted(http), activity(http)
    headers = business_headers(http)
    http.now += timedelta(seconds=60)
    response = http.post("/api/works", headers=headers, json={**body, "actor": str(http.grant.user.user_id)})
    assert response.status_code == 409
    assert persisted(http) == before and activity(http) == idle
    assert http.get(f"/api/works/{first['id']}").json() == first


@pytest.mark.parametrize("legacy_label", ["Legacy fictional name", "current-user-uuid"])
def test_legacy_idempotency_keys_fail_closed_and_historical_rows_stay_unchanged(http, legacy_label):
    body = payload()
    if legacy_label == "current-user-uuid":
        legacy_label = str(http.grant.user.user_id)
    old = http.app.state.repository.create_work(**body, actor=legacy_label)
    before = persisted(http)
    assert "actor_attribution" not in old["events"][0]["details"]
    response = http.post("/api/works", headers=business_headers(http), json={**body, "actor": legacy_label})
    assert response.status_code == 409 and persisted(http) == before
    assert http.get(f"/api/works/{old['id']}").json() == old
    updated = http.put(f"/api/works/{old['id']}/labour", headers=business_headers(http),
                       json=dict(version=1, hours="1.60", work_description="Fictional Autodata"))
    assert updated.status_code == 200 and updated.json()["events"][1] == old["events"][0]
    with http.app.state.repository._connect() as connection:
        events = [tuple(row) for row in connection.execute("SELECT * FROM work_events ORDER BY id")]
        requests = [tuple(row) for row in connection.execute("SELECT * FROM scoped_inquiry_creation_requests")]
    assert events[:1] == before["work_events"] and requests == before["scoped_inquiry_creation_requests"]


@pytest.mark.parametrize("same_user", [True, False])
def test_concurrent_key_reuse_is_atomic_and_identity_scoped(http, same_user):
    body = payload()
    headers = [business_headers(http)]
    first_token = http.cookies.get(SESSION_COOKIE)
    if same_user:
        headers *= 2
    else:
        other = grant_session(http.app.state.auth_service)
        http.cookies.set(SESSION_COOKIE, other.token.get_secret_value())
        headers.append(business_headers(http))
        headers[0]["Cookie"] = SESSION_COOKIE + "=" + first_token
        headers[1]["Cookie"] = SESSION_COOKIE + "=" + other.token.get_secret_value()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda header: http.post("/api/works", headers=header, json=body), headers))
    assert sorted(r.status_code for r in responses) == ([201, 201] if same_user else [201, 409])
    if same_user:
        assert responses[0].json() == responses[1].json()
    assert len(persisted(http)["work_events"]) == len(persisted(http)["scoped_inquiry_creation_requests"]) == 1


def test_event_insert_failure_rolls_back_authenticated_mutation_and_idle_refresh(http, write, monkeypatch):
    method, path, body = write
    headers = business_headers(http)
    before, idle = persisted(http), activity(http)
    http.now += timedelta(seconds=60)
    def fail(*args, **kwargs):
        raise RuntimeError("fictional event failure")
    monkeypatch.setattr(http.app.state.repository, "_insert_event", fail)
    with pytest.raises(RuntimeError, match="fictional event failure"):
        http.request(method, path, headers=headers, json=body)
    assert persisted(http) == before and activity(http) == idle


@pytest.mark.parametrize("state", ["revoked", "idle", "absolute", "role", "disabled", "version", "mfa", "storage"])
def test_handler_bridge_revalidates_after_body_validation_before_mutation(http, write, monkeypatch, state):
    method, path, body = write
    service = http.app.state.auth_service
    headers = business_headers(http)
    original = service.inspect_session
    before, idle = persisted(http), activity(http)
    http.now += timedelta(seconds=60)
    calls = 0
    def inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # The handler bridge, after the two CP3B gate checks.
            if state == "storage":
                raise sqlite3.OperationalError("fictional-sensitive-error")
            if state == "revoked":
                service.revoke_session(http.cookies.get(SESSION_COOKIE))
            elif state in {"idle", "absolute"}:
                http.now += timedelta(seconds=1800 if state == "idle" else 28800)
            else:
                updates = {"role": "role='IT_ADMIN'", "disabled": "enabled=0", "version": "auth_version=auth_version+1",
                           "mfa": "mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL"}
                with service.repository.transaction() as connection:
                    connection.execute("UPDATE auth_users SET " + updates[state])
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "inspect_session", inspect)
    response = http.request(method, path, headers=headers, json=body)
    assert calls == 3 and response.status_code == {"role": 403, "storage": 503}.get(state, 401)
    assert "sensitive" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert persisted(http) == before and activity(http) == idle


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN])
@pytest.mark.parametrize("permission", list(WorkshopPermission))
def test_bridge_derives_permissions_and_canonical_uuid_from_current_session(http, role, permission):
    service = http.app.state.auth_service
    with service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET role=?", (role.value,))
    before = activity(http)
    http.now += timedelta(seconds=60)
    kwargs = dict(required_permission=permission)
    if role == UserRole.IT_ADMIN and permission != WorkshopPermission.READ_LABOUR_PLAN:
        with pytest.raises(PermissionDeniedError):
            resolve_workshop_context(service, http.cookies.get(SESSION_COOKIE), **kwargs)
    else:
        context = resolve_workshop_context(service, http.cookies.get(SESSION_COOKIE), **kwargs)
        assert context.employee_id == str(http.grant.user.user_id)
        assert context.actor.attribution()["display_name"] == http.grant.user.display_name
        assert context.permissions == (frozenset({WorkshopPermission.READ_LABOUR_PLAN}) if role == UserRole.IT_ADMIN else frozenset(WorkshopPermission))
        with pytest.raises(FrozenInstanceError):
            context.actor.display_name = "Spoofed"
    assert activity(http) == before


@pytest.mark.parametrize("proof", ["uuid", "user", "json", "context"])
def test_bridge_rejects_user_or_context_objects_as_session_proof(http, proof):
    user = http.grant.user
    values = {"uuid": str(user.user_id), "user": user, "json": user.model_dump(mode="json"),
              "context": TrustedWorkshopContext(str(user.user_id), frozenset(WorkshopPermission))}
    with pytest.raises(AuthenticationError):
        resolve_workshop_context(http.app.state.auth_service, values[proof], required_permission=WorkshopPermission.READ_LABOUR_PLAN)


def test_bridge_openclaw_and_untyped_permission_denied(http):
    service = http.app.state.auth_service
    with pytest.raises(PermissionDeniedError):
        resolve_workshop_context(service, http.cookies.get(SESSION_COOKIE), required_permission="SAVE_LABOUR_PLAN")
    with service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET kind='SERVICE',role='OPENCLAW',password_hash=NULL,mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL")
    with pytest.raises(AuthenticationError):
        resolve_workshop_context(service, http.cookies.get(SESSION_COOKIE), required_permission=WorkshopPermission.READ_LABOUR_PLAN)
    assert http.get("/api/works").status_code == 401


def test_internal_workshop_operations_use_uuid_and_attributed_events_without_rewriting_originals(http):
    work = create(http)
    service = LabourPlanService(http.app.state.repository)
    auth = http.app.state.auth_service
    token = http.cookies.get(SESSION_COOKIE)
    command = save_command(work["id"])
    before = command.model_dump(round_trip=True)
    context = resolve_workshop_context(auth, token, required_permission=WorkshopPermission.SAVE_LABOUR_PLAN)
    result = service.save_plan(context, command)
    assert command.model_dump(round_trip=True) == before
    saved = http.app.state.repository.get_work(work["id"])["events"][0]
    assert_actor(saved, http.grant.user)
    rename(http, "Fictional reviewer name")
    context = resolve_workshop_context(auth, token, required_permission=WorkshopPermission.APPROVE_LABOUR_PLAN)
    approved = service.approve_plan(context, approval(result))
    events = http.app.state.repository.get_work(work["id"])["events"]
    assert events[1] == saved
    assert_actor(events[0], http.grant.user, "Fictional reviewer name")
    assert events[0]["details"]["reviewer_id"] == str(http.grant.user.user_id)
    assert service.decision_history(context, work["id"])[0].reviewer_id == str(http.grant.user.user_id)
    assert str(approved.final_billable_hours) == "3.60"


@pytest.mark.parametrize("operation", ["save", "approve"])
def test_authenticated_workshop_retries_are_versioned_and_preserve_original_name(http, operation):
    work = create(http)
    service = LabourPlanService(http.app.state.repository)
    auth, token = http.app.state.auth_service, http.cookies.get(SESSION_COOKIE)
    permission = WorkshopPermission.SAVE_LABOUR_PLAN if operation == "save" else WorkshopPermission.APPROVE_LABOUR_PLAN
    context = resolve_workshop_context(auth, token, required_permission=permission)
    command = save_command(work["id"])
    if operation == "approve":
        command = approval(service.save_plan(context, command))
    function = service.save_plan if operation == "save" else service.approve_plan
    first = function(context, command)
    before = persisted(http)
    rename(http, "Fictional renamed workshop user")
    context = resolve_workshop_context(auth, token, required_permission=permission)
    assert function(context, command) == first and persisted(http) == before
    with http.app.state.repository._connect() as connection:
        rows = connection.execute("SELECT request_json FROM labour_plan_requests").fetchall()
    for row in rows:
        fingerprint = json.loads(row[0])
        assert fingerprint["contract"] == "carbit.workshop.authenticated.v1"
        assert fingerprint["actor_user_id"] == str(http.grant.user.user_id)
        assert "display_name" not in fingerprint


@pytest.mark.parametrize("operation", ["save", "approve"])
def test_authenticated_workshop_key_reuse_by_another_user_conflicts_before_mutation(http, operation):
    work = create(http)
    service = LabourPlanService(http.app.state.repository)
    auth = http.app.state.auth_service
    permission = WorkshopPermission.SAVE_LABOUR_PLAN if operation == "save" else WorkshopPermission.APPROVE_LABOUR_PLAN
    first_context = resolve_workshop_context(auth, http.cookies.get(SESSION_COOKIE), required_permission=permission)
    command = save_command(work["id"])
    if operation == "approve":
        command = approval(service.save_plan(first_context, command))
    function = service.save_plan if operation == "save" else service.approve_plan
    result = function(first_context, command)
    other = grant_session(auth)
    context = resolve_workshop_context(auth, other.token.get_secret_value(), required_permission=permission)
    before = persisted(http)
    # A current version would permit a new mutation under the old actor-scoped
    # retry lookup; the authenticated key owner check must reject it first.
    with pytest.raises(IdempotencyConflictError):
        function(context, command.model_copy(update={"expected_version": result.work_version}))
    assert persisted(http) == before


@pytest.mark.parametrize("operation", ["save", "approve"])
def test_authenticated_workshop_cannot_replay_legacy_uuid_context_record(http, operation):
    work = create(http)
    service = LabourPlanService(http.app.state.repository)
    from dataclasses import replace
    legacy = replace(resolve_workshop_context(http.app.state.auth_service, http.cookies.get(SESSION_COOKIE), required_permission=WorkshopPermission.SAVE_LABOUR_PLAN), actor=None)
    command = save_command(work["id"])
    if operation == "approve":
        command = approval(service.save_plan(legacy, command))
    function = service.save_plan if operation == "save" else service.approve_plan
    function(legacy, command)
    before = persisted(http)
    with http.app.state.repository._connect() as connection:
        records = [tuple(row) for row in connection.execute("SELECT * FROM labour_plan_requests")]
    context = resolve_workshop_context(http.app.state.auth_service, http.cookies.get(SESSION_COOKIE),
                                       required_permission=WorkshopPermission.SAVE_LABOUR_PLAN if operation == "save" else WorkshopPermission.APPROVE_LABOUR_PLAN)
    with pytest.raises(IdempotencyConflictError):
        function(context, command)
    assert persisted(http) == before
    with http.app.state.repository._connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM labour_plan_requests")] == records


def test_concurrent_authenticated_workshop_key_reuse_cannot_create_two_actor_records(http):
    work = create(http)
    service = LabourPlanService(http.app.state.repository)
    auth = http.app.state.auth_service
    other = grant_session(auth)
    contexts = [resolve_workshop_context(auth, token, required_permission=WorkshopPermission.SAVE_LABOUR_PLAN)
                for token in (http.cookies.get(SESSION_COOKIE), other.token.get_secret_value())]
    command = save_command(work["id"])
    def save(context):
        try:
            service.save_plan(context, command)
            return "accepted"
        except IdempotencyConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, contexts)) == ["accepted", "conflict"]
    assert http.app.state.repository.get_work(work["id"])["version"] == 2
    with http.app.state.repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM labour_plan_requests").fetchone()[0] == 1


def test_snapshot_and_context_reject_noncanonical_or_mismatched_identities():
    with pytest.raises(ValueError):
        BusinessActor("not-a-uuid", "Fictional")
    with pytest.raises(ValueError):
        BusinessActor(uuid4(), " " * 2)
    actor = BusinessActor(uuid4(), "Fictional")
    with pytest.raises(ValueError):
        TrustedWorkshopContext(str(uuid4()), frozenset(WorkshopPermission), actor=actor)
