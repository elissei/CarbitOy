"""SEC-1.2 audit privacy, guarded logout budgets and concurrent revocation."""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from http_auth_support import grant_session
from test_webapp_auth_rate_limits import rate_rows, reserve_scope
from test_webapp_auth_security_sec_1_1 import event_count
from test_webapp_auth_service import setup
from test_webapp_http_auth import (
    ORIGIN, bootstrap, complete, headers, http, passwords, provision, rows,
)
from webapp.auth_crypto import token_digest
from webapp.auth_csrf import SESSION_COOKIE
from webapp.auth_service import AuthenticationError, AuthPolicy, RateLimitError, TrustedClientContext


def counter(service, phase, scope):
    digest = hashlib.sha256(f"{phase}:{scope}".encode()).hexdigest()
    with service.repository._connect() as connection:
        row = connection.execute("SELECT attempts FROM auth_rate_limits WHERE scope_hash=?", (digest,)).fetchone()
    return None if row is None else row["attempts"]


def rate_events(repository):
    with repository._connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM auth_events WHERE event_type='AUTH_RATE_LIMITED'"
        )]


@pytest.mark.parametrize("phase", ["PASSWORD", "MFA"])
@pytest.mark.parametrize("scope_kind", ["global", "account", "client"])
def test_rate_audit_details_contain_only_categories_not_identifiers(setup, phase, scope_kind):
    service, repository, _ = setup
    service.policy = replace(service.policy, individual_attempts=1, global_attempts=2)
    account, address = "fictional.private.account", "198.51.100.23"
    assert reserve_scope(service, phase, account, address)
    if scope_kind == "global":
        assert reserve_scope(service, phase, "second.fictional.account", "198.51.100.24")
        assert not reserve_scope(service, phase, "third.fictional.account", "198.51.100.25")
    elif scope_kind == "account":
        assert not reserve_scope(service, phase, account, "198.51.100.24")
    else:
        assert not reserve_scope(service, phase, "second.fictional.account", address)
    events = rate_events(repository)
    assert len(events) == 1
    event = events[0]
    details = json.loads(event["details_json"])
    assert set(details) == {"phase", "scope_kind", "sample_key", "sample_window_seconds", "sampled"}
    assert details["phase"] == phase and details["scope_kind"] == scope_kind
    assert details["sample_key"] == f"RATE:{phase}:{scope_kind}"
    assert details["sample_window_seconds"] == 60 and details["sampled"] is True
    assert event["actor_user_id"] is None and event["subject_user_id"] is None
    for value in (account, address, "second.fictional.account", "198.51.100.24"):
        assert value not in event["details_json"]
    assert not re.search(r"[0-9a-f]{64}", event["details_json"])
    assert "scope_hash" not in details and "window_start" not in details


def test_http_rate_audit_retains_operation_without_client_hash(setup):
    service, repository, _ = setup
    service.policy = replace(service.policy, individual_attempts=1)
    client = TrustedClientContext("198.51.100.23")
    service.reserve_http_auth("login", client=client)
    with pytest.raises(RateLimitError):
        service.reserve_http_auth("login", client=client)
    details = json.loads(rate_events(repository)[0]["details_json"])
    assert set(details) == {"phase", "operation", "scope_kind", "sample_key", "sample_window_seconds", "sampled"}
    assert details["operation"] == "login" and details["scope_kind"] == "client"
    assert details["sample_key"] == "RATE:HTTP_AUTH:login:client"
    assert "198.51.100.23" not in json.dumps(details)
    assert not re.search(r"[0-9a-f]{64}", json.dumps(details))


@pytest.mark.parametrize("field", ["limit_window_seconds", "maintenance_window_seconds"])
@pytest.mark.parametrize("seconds", [1, 86400])
def test_rate_window_boundaries_are_accepted_without_clamping(field, seconds):
    policy = AuthPolicy(**{field: seconds})
    assert getattr(policy, field) == seconds


@pytest.mark.parametrize("field", ["limit_window_seconds", "maintenance_window_seconds"])
@pytest.mark.parametrize("seconds", [86401, 172800])
def test_rate_windows_above_one_day_fail_configuration(field, seconds):
    with pytest.raises(ValueError, match="86400"):
        AuthPolicy(**{field: seconds})


def test_rate_window_defaults_are_unchanged():
    policy = AuthPolicy()
    assert policy.limit_window_seconds == 300
    assert policy.maintenance_window_seconds == 60


@pytest.mark.parametrize("kind,status", [
    ("missing_session", 401), ("invalid_session", 401), ("invalid_csrf", 403), ("invalid_payload", 422),
])
def test_distributed_invalid_logout_cannot_exhaust_global_quota_or_block_valid_logout(http, kind, status):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    service = http.app.state.auth_service
    service.policy = replace(service.policy, maintenance_individual_attempts=2, maintenance_global_attempts=2)
    strict_before = {phase: counter(service, phase, "global") for phase in ("HTTP_AUTH:login", "HTTP_AUTH:mfa")}
    assert counter(service, "HTTP_LOGOUT", "global") is None
    request_headers = headers("invalid")
    payload = {}
    if kind == "invalid_session":
        request_headers["Cookie"] = f"{SESSION_COOKIE}=" + "A" * 43
    elif kind in {"invalid_csrf", "invalid_payload"}:
        request_headers["Cookie"] = f"{SESSION_COOKIE}={token}"
    if kind == "invalid_payload":
        request_headers["X-CSRF-Token"] = proof
        payload = {"unexpected": True}
    # Twelve requests from six actual ASGI peers exceed the global cap of two.
    # Forged forwarding headers are supplied but never used as identity.
    for index in range(6):
        attacker = TestClient(http.app, base_url=ORIGIN, client=(f"198.51.100.{30 + index}", 50000))
        try:
            for _ in range(2):
                response = attacker.post("/api/auth/logout", headers={
                    **request_headers, "X-Forwarded-For": "127.0.0.1",
                }, json=payload)
                assert response.status_code == status and not response.headers.get_list("set-cookie")
            assert attacker.post("/api/auth/logout", headers=request_headers, json=payload).status_code == 429
        finally:
            attacker.close()
        assert counter(service, "HTTP_LOGOUT", "global") is None
    assert service.require_session(token).user_id == user.user_id
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 204 and not http.cookies.get(SESSION_COOKIE)
    assert counter(service, "HTTP_LOGOUT", "global") == 1
    assert {phase: counter(service, phase, "global") for phase in strict_before} == strict_before
    with pytest.raises(AuthenticationError):
        service.require_session(token)
    with service.repository._connect() as connection:
        assert connection.execute("SELECT revoked_at FROM auth_sessions WHERE token_hash=?",
                                  (token_digest(token),)).fetchone()[0] is not None


def test_global_logout_limit_still_protects_validated_requests_and_preserves_cookie_on_429(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    service = http.app.state.auth_service
    other_clients = []
    try:
        # Prepare valid sessions/proofs before tightening the maintenance cap.
        other_proofs = []
        for index in range(2):
            grant = grant_session(service)
            client = TestClient(http.app, base_url=ORIGIN, client=(f"198.51.100.{50 + index}", 50000))
            other_clients.append(client)
            client.cookies.set(SESSION_COOKIE, grant.token.get_secret_value())
            response = client.get("/api/auth/csrf", headers={"X-Carbit-Auth": "bootstrap"})
            assert response.status_code == 200
            other_proofs.append(response.json()["csrf_token"])
        service.policy = replace(service.policy, maintenance_individual_attempts=2, maintenance_global_attempts=2)
        for client, other_proof in zip(other_clients, other_proofs):
            assert client.post("/api/auth/logout", headers=headers(other_proof), json={}).status_code == 204
        assert counter(service, "HTTP_LOGOUT", "global") == 2
        response = http.post("/api/auth/logout", headers=headers(proof), json={})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert not response.headers.get_list("set-cookie") and http.cookies.get(SESSION_COOKIE) == token
        assert service.require_session(token)
        details = json.loads(rate_events(service.repository)[-1]["details_json"])
        assert details["scope_kind"] == "global" and details["operation"] == "logout"
        http.clock.advance(60)
        assert http.post("/api/auth/logout", headers=headers(proof), json={}).status_code == 204
        with pytest.raises(AuthenticationError):
            service.require_session(token)
    finally:
        for client in other_clients:
            client.close()


def test_anonymous_logout_does_not_consult_even_an_exhausted_global_quota(http):
    service = http.app.state.auth_service
    service.policy = replace(service.policy, maintenance_individual_attempts=2, maintenance_global_attempts=2)
    # Trusted service calls represent two already validated reservations.
    for index in range(2):
        service.reserve_logout_global(client=TrustedClientContext(f"198.51.100.{60 + index}"))
    assert counter(service, "HTTP_LOGOUT", "global") == 2
    assert http.post("/api/auth/logout", headers=headers("invalid"), json={}).status_code == 401
    assert counter(service, "HTTP_LOGOUT", "global") == 2


def test_concurrent_logout_revokes_once_and_only_one_response_confirms_success(http, monkeypatch):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    service = http.app.state.auth_service
    revoked_before = event_count(service.repository, "SESSION_REVOKED")
    barrier = Barrier(2)
    original = service.revoke_session

    def overlap(value):
        barrier.wait(timeout=5)
        return original(value)

    monkeypatch.setattr(service, "revoke_session", overlap)
    request_headers = {**headers(proof), "Cookie": f"{SESSION_COOKIE}={token}"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: http.post("/api/auth/logout", headers=request_headers, json={}), range(2)))
    assert sorted(response.status_code for response in responses) == [204, 401]
    confirmed = next(response for response in responses if response.status_code == 204)
    rejected = next(response for response in responses if response.status_code == 401)
    assert confirmed.headers.get_list("set-cookie")
    assert not rejected.headers.get_list("set-cookie")
    assert counter(service, "HTTP_LOGOUT", "global") == 2
    assert counter(service, "HTTP_LOGOUT", "client:testclient") == 2
    assert event_count(service.repository, "SESSION_REVOKED") == revoked_before + 1
    with pytest.raises(AuthenticationError):
        service.require_session(token)
    with service.repository._connect() as connection:
        assert connection.execute("SELECT revoked_at FROM auth_sessions WHERE token_hash=?",
                                  (token_digest(token),)).fetchone()[0] is not None
