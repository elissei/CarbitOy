"""SEC-1 availability and abuse regressions with isolated auth/business storage."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from test_webapp_auth_service import setup
from test_webapp_http_auth import bootstrap, complete, headers, http, passwords, provision
from webapp.auth_models import SessionCookiePolicy
from webapp.auth_service import AuthPolicy, AuthService, RateLimitError, TrustedClientContext


def rate_rows(repository):
    with repository._connect() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT scope_hash,window_start,attempts FROM auth_rate_limits ORDER BY scope_hash"
        )]


def reserve_scope(service, phase, account, client):
    with service.repository.transaction() as connection:
        return service._reserve(connection, phase, account, TrustedClientContext(client), service._now())


@pytest.mark.parametrize("field", [
    "maintenance_window_seconds", "maintenance_individual_attempts", "maintenance_global_attempts",
])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_maintenance_policy_rejects_unbounded_or_noninteger_values(field, value):
    with pytest.raises(ValueError):
        AuthPolicy(**{field: value})


def test_maintenance_policy_rejects_individual_limit_above_global():
    with pytest.raises(ValueError):
        AuthPolicy(maintenance_individual_attempts=11, maintenance_global_attempts=10)


@pytest.mark.parametrize("operation", ["login", "mfa"])
def test_maintenance_and_credential_budgets_are_independent(setup, operation):
    service, _, _ = setup
    client = TrustedClientContext("198.51.100.10")
    for _ in range(40):
        service.reserve_http_auth("session", client=client)
        service.reserve_http_auth("csrf", client=client)
    for _ in range(5):
        service.reserve_http_auth(operation, client=client)
    with pytest.raises(RateLimitError):
        service.reserve_http_auth(operation, client=client)
    service.reserve_http_auth("session", client=client)
    service.reserve_http_auth("csrf", client=client)


def test_maintenance_endpoints_share_a_bounded_persistent_budget(setup):
    service, repository, clock = setup
    service.policy = replace(service.policy, maintenance_individual_attempts=3,
                             maintenance_global_attempts=10)
    client = TrustedClientContext("198.51.100.10")
    for operation in ("csrf", "session", "csrf"):
        service.reserve_http_auth(operation, client=client)
    restarted = AuthService(repository, service.cipher, passwords=service.passwords,
                            clock=clock, policy=service.policy)
    before = rate_rows(repository)
    with pytest.raises(RateLimitError):
        restarted.reserve_http_auth("session", client=client)
    assert rate_rows(repository) == before
    clock.advance(60)
    restarted.reserve_http_auth("session", client=client)


def test_maintenance_window_does_not_reset_strict_login_window(setup):
    service, _, clock = setup
    service.policy = replace(service.policy, maintenance_individual_attempts=2,
                             maintenance_global_attempts=10)
    client = TrustedClientContext("198.51.100.10")
    service.reserve_http_auth("csrf", client=client)
    service.reserve_http_auth("session", client=client)
    for _ in range(5):
        service.reserve_http_auth("login", client=client)
    clock.advance(59)
    with pytest.raises(RateLimitError):
        service.reserve_http_auth("csrf", client=client)
    clock.advance(1)
    service.reserve_http_auth("csrf", client=client)
    with pytest.raises(RateLimitError):
        service.reserve_http_auth("login", client=client)


@pytest.mark.parametrize("operation", ["login", "mfa", "logout", "csrf", "session"])
def test_blocked_client_does_not_drain_other_clients_global_budget(setup, operation):
    service, repository, _ = setup
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=3,
                             maintenance_individual_attempts=2, maintenance_global_attempts=3)
    def reserve(client):
        service.reserve_http_auth(operation, client=client)
        if operation == "logout":
            # The adapter invokes this second stage only for validated logout.
            service.reserve_logout_global(client=client)

    blocked = TrustedClientContext("198.51.100.10")
    for _ in range(2):
        reserve(blocked)
    before = rate_rows(repository)
    for _ in range(20):
        with pytest.raises(RateLimitError):
            reserve(blocked)
    assert rate_rows(repository) == before
    reserve(TrustedClientContext("198.51.100.11"))
    with pytest.raises(RateLimitError):
        reserve(TrustedClientContext("198.51.100.12"))


@pytest.mark.parametrize("phase", ["PASSWORD", "MFA"])
def test_blocked_account_or_client_spends_no_other_scope_budget(setup, phase):
    service, repository, _ = setup
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=4)
    for _ in range(2):
        assert reserve_scope(service, phase, "fictional-account-a", "198.51.100.10")
    before = rate_rows(repository)
    for index in range(10):
        assert not reserve_scope(service, phase, f"rotated-account-{index}", "198.51.100.10")
        assert not reserve_scope(service, phase, "fictional-account-a", f"198.51.100.{20 + index}")
    assert rate_rows(repository) == before
    for _ in range(2):
        assert reserve_scope(service, phase, "fictional-account-b", "198.51.100.11")
    before = rate_rows(repository)
    assert not reserve_scope(service, phase, "fictional-account-c", "198.51.100.12")
    assert rate_rows(repository) == before


def test_blocked_client_does_not_reset_an_expired_global_window(setup):
    service, repository, clock = setup
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=10)
    assert reserve_scope(service, "PASSWORD", "first-account", "198.51.100.11")
    clock.advance(100)
    for _ in range(2):
        assert reserve_scope(service, "PASSWORD", "blocked-account", "198.51.100.10")
    clock.advance(200)
    before = rate_rows(repository)
    assert not reserve_scope(service, "PASSWORD", "blocked-account", "198.51.100.10")
    assert rate_rows(repository) == before
    assert reserve_scope(service, "PASSWORD", "other-account", "198.51.100.12")


@pytest.mark.parametrize("operation", ["login", "csrf"])
def test_concurrent_clients_cannot_overrun_global_limit(setup, operation):
    service, _, _ = setup
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=5,
                             maintenance_individual_attempts=2, maintenance_global_attempts=5)

    def attempt(index):
        try:
            service.reserve_http_auth(operation, client=TrustedClientContext(f"198.51.100.{index}"))
        except RateLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(32))) == 5


def test_many_session_and_csrf_requests_keep_the_real_session_usable(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SessionCookiePolicy().name)
    for _ in range(12):
        response = http.get("/api/auth/session")
        assert response.status_code == 200
        assert response.json()["user"]["user_id"] == str(user.user_id)
        response = http.get("/api/auth/csrf")
        assert response.status_code == 200 and response.json()["state"] == "session"
    assert http.cookies.get(SessionCookiePolicy().name) == token
    assert http.get("/api/works").status_code == 200


def test_maintenance_never_authenticates_anonymous_clients(http):
    for _ in range(10):
        assert http.get("/api/auth/session").status_code == 401
        response = http.get("/api/auth/csrf")
        assert response.status_code == 200 and response.json()["state"] == "preauth"
    assert http.get("/api/works").status_code == 401
    assert not http.cookies.get(SessionCookiePolicy().name)


@pytest.mark.parametrize("operation", ["login", "mfa"])
def test_forged_forwarded_addresses_cannot_evade_strict_http_limit(http, operation):
    for index in range(6):
        response = http.post("/api/auth/" + operation, headers={
            "Origin": "https://testserver", "X-Forwarded-For": f"198.51.100.{index}",
            "Forwarded": f"for=198.51.100.{index};proto=https", "X-Real-IP": f"198.51.100.{index}",
        }, json={})
        assert response.status_code == (403 if index < 5 else 429)
        assert response.headers["cache-control"] == "no-store"
    assert not http.cookies.get(SessionCookiePolicy().name)
    assert http.get("/api/auth/csrf").status_code == 200
    assert http.get("/api/auth/session").status_code == 401


def test_bad_maintenance_requests_still_share_an_ip_limit(http):
    service = http.app.state.auth_service
    service.policy = replace(service.policy, maintenance_individual_attempts=2,
                             maintenance_global_attempts=10)
    before = rate_rows(service.repository)
    assert http.get("/api/auth/csrf", headers={"X-Carbit-Auth": "invalid"}).status_code == 403
    assert rate_rows(service.repository) == before  # Stateless rejection spends no quota.
    assert http.get("/api/auth/session").status_code == 401
    assert http.get("/api/auth/session").status_code == 401
    before = rate_rows(service.repository)
    response = http.get("/api/auth/csrf", headers={"X-Forwarded-For": "198.51.100.99"})
    assert response.status_code == 429
    assert rate_rows(service.repository) == before
    assert not response.headers.get_list("set-cookie")


def test_reusing_session_csrf_proof_neither_spends_maintenance_nor_bypasses_auth(http):
    from uuid import uuid4

    user, secret, _ = provision(http)
    complete(http, user, secret)
    proof = bootstrap(http)
    service = http.app.state.auth_service
    before = rate_rows(service.repository)
    for index in range(6):
        response = http.post("/api/works", headers=headers(proof), json={
            "registration": f"TEST-SEC-{index}", "customer_description": "Fictional SEC-1 inquiry",
            "idempotency_key": str(uuid4()),
        })
        assert response.status_code == 201, response.text
    assert rate_rows(service.repository) == before
    payload = {"registration": "TEST-SEC-X", "customer_description": "Fictional rejected inquiry",
               "idempotency_key": str(uuid4())}
    assert http.post("/api/works", headers=headers("invalid"), json=payload).status_code == 403
    service.revoke_session(http.cookies.get(SessionCookiePolicy().name))
    assert http.post("/api/works", headers=headers(proof), json=payload).status_code == 401
