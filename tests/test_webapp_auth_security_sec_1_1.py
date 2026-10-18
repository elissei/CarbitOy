"""SEC-1.1 logout, preflight, Retry-After and durable denial-audit regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from test_webapp_auth_rate_limits import rate_rows
from test_webapp_auth_service import PASSWORD, enroll, provision as provision_service, setup
from test_webapp_http_auth import bootstrap, complete, headers, http, passwords, provision, rows
from webapp.auth_crypto import token_digest
from webapp.auth_csrf import SESSION_COOKIE
from webapp.auth_service import AuthenticationError, AuthService, RateLimitError, TrustedClientContext


def event_count(repository, event, status=None):
    with repository._connect() as connection:
        events = connection.execute("SELECT event_type,details_json FROM auth_events").fetchall()
    return sum(row["event_type"] == event and
               (status is None or json.loads(row["details_json"]).get("status") == status)
               for row in events)


def test_logout_survives_strict_and_session_csrf_limit_exhaustion(http):
    import hashlib

    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    service = http.app.state.auth_service

    def global_attempts(phase):
        scope_hash = hashlib.sha256(f"{phase}:global".encode()).hexdigest()
        with service.repository._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM auth_rate_limits WHERE scope_hash=?", (scope_hash,),
            ).fetchone()
        assert row is not None
        return row["attempts"]

    # complete() already spent one HTTP login and one HTTP MFA reservation.
    # Their later state-conflict rejections use the same operation/client budget.
    for operation, expected_status in (("login", 409), ("mfa", 403)):
        phase = "HTTP_AUTH:" + operation
        consumed = global_attempts(phase)
        assert consumed == 1
        for _ in range(service.policy.individual_attempts - consumed):
            response = http.post("/api/auth/" + operation, headers=headers(proof), json={})
            assert response.status_code == expected_status
        assert global_attempts(phase) == service.policy.individual_attempts
        assert global_attempts(phase) < service.policy.global_attempts
        before = rate_rows(service.repository)
        response = http.post("/api/auth/" + operation, headers=headers(proof), json={})
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(service.policy.limit_window_seconds)
        assert rate_rows(service.repository) == before
        denials = [json.loads(event["details_json"]) for event in rows(http, "auth_events")
                   if event["event_type"] == "AUTH_RATE_LIMITED"]
        assert any(denial["phase"] == phase for denial in denials)

    service.policy = replace(service.policy, maintenance_individual_attempts=6,
                             maintenance_global_attempts=60)
    # Login bootstrap and the session proof share the maintenance budget.
    consumed = global_attempts("HTTP_MAINTENANCE")
    assert consumed == 2
    for _ in range(service.policy.maintenance_individual_attempts - consumed):
        assert http.get("/api/auth/session").status_code == 200
    assert global_attempts("HTTP_MAINTENANCE") == service.policy.maintenance_individual_attempts
    assert global_attempts("HTTP_MAINTENANCE") < service.policy.maintenance_global_attempts
    before = rate_rows(service.repository)
    for endpoint in ("session", "csrf"):
        response = http.get("/api/auth/" + endpoint)
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(service.policy.maintenance_window_seconds)
        assert rate_rows(service.repository) == before
    assert service.require_session(token).user_id == user.user_id
    revoked_before = event_count(service.repository, "SESSION_REVOKED")
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 204
    assert not http.cookies.get(SESSION_COOKIE)
    with pytest.raises(AuthenticationError):
        service.require_session(token)
    with service.repository._connect() as connection:
        session = connection.execute(
            "SELECT revoked_at FROM auth_sessions WHERE token_hash=?", (token_digest(token),),
        ).fetchone()
    assert session is not None and session["revoked_at"] is not None
    assert event_count(service.repository, "SESSION_REVOKED") == revoked_before + 1


def test_more_than_five_rejected_logout_requests_do_not_block_valid_logout(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    for _ in range(8):
        assert http.post("/api/auth/logout", headers=headers(proof), json={"unexpected": True}).status_code == 422
    assert http.app.state.auth_service.require_session(token)
    assert http.post("/api/auth/logout", headers=headers(proof), json={}).status_code == 204
    with pytest.raises(AuthenticationError):
        http.app.state.auth_service.require_session(token)


@pytest.mark.parametrize("csrf", [None, "invalid"])
def test_logout_still_requires_valid_session_bound_csrf(http, csrf):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    request_headers = {"Origin": "https://testserver"}
    if csrf is not None:
        request_headers["X-CSRF-Token"] = csrf
    response = http.post("/api/auth/logout", headers=request_headers, json={})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    assert http.app.state.auth_service.require_session(token)


@pytest.mark.parametrize("operation", ["login", "mfa", "logout", "csrf", "session"])
@pytest.mark.parametrize("bad_headers,status", [
    ({"Host": "evil.example"}, 400),
    ({"Origin": "null"}, 403),
    ({"Origin": "https://evil.example"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Sec-Fetch-Site": "none"}, 403),
    ({"Sec-Fetch-Site": "same-origin, cross-site"}, 403),
    ({"Cookie": "__Host-carbit-session=invalid value"}, 400),
])
def test_stateless_preflight_denials_never_reserve_auth_budget(http, monkeypatch, operation, bad_headers, status):
    service = http.app.state.auth_service
    before = rate_rows(service.repository)

    def forbidden_reservation(*args, **kwargs):
        raise AssertionError("preflight must reject before persistent reservation")

    monkeypatch.setattr(service, "reserve_http_auth", forbidden_reservation)
    request_headers = {"Origin": "https://testserver", "X-Carbit-Auth": "bootstrap",
                       "Sec-Fetch-Site": "same-origin", **bad_headers}
    method = "GET" if operation in {"csrf", "session"} else "POST"
    response = http.request(method, "/api/auth/" + operation, headers=request_headers)
    assert response.status_code == status
    assert rate_rows(service.repository) == before
    assert not response.headers.get_list("set-cookie")


def test_duplicate_fetch_site_header_is_rejected_before_reservation(http):
    before = rate_rows(http.app.state.auth_service.repository)
    response = http.get("/api/auth/csrf", headers=[
        ("X-Carbit-Auth", "bootstrap"), ("Sec-Fetch-Site", "same-origin"),
        ("Sec-Fetch-Site", "same-origin"),
    ])
    assert response.status_code == 403
    assert rate_rows(http.app.state.auth_service.repository) == before


def test_crypto_csrf_failure_after_preflight_still_consumes_strict_budget(http):
    bootstrap(http)
    for _ in range(5):
        assert http.post("/api/auth/login", headers=headers("invalid"), json={}).status_code == 403
    assert http.post("/api/auth/login", headers=headers("invalid"), json={}).status_code == 429


def test_transport_denial_audits_are_sampled_and_successes_remain_complete(http):
    service = http.app.state.auth_service
    for _ in range(40):
        assert http.post("/api/auth/login", headers={"Origin": "null"}, json={}).status_code == 403
    assert not rate_rows(service.repository)
    assert event_count(service.repository, "AUTH_HTTP_RESULT", 403) == 1
    http.clock.advance(59)
    assert http.post("/api/auth/login", headers={"Origin": "null"}, json={}).status_code == 403
    assert event_count(service.repository, "AUTH_HTTP_RESULT", 403) == 1
    http.clock.advance(1)
    assert http.post("/api/auth/login", headers={"Origin": "null"}, json={}).status_code == 403
    assert event_count(service.repository, "AUTH_HTTP_RESULT", 403) == 2
    for _ in range(3):
        assert http.get("/api/auth/csrf").status_code == 200
    assert event_count(service.repository, "AUTH_HTTP_RESULT", 200) == 3
    details = json.loads(rows(http, "auth_events")[0]["details_json"])
    assert details["sampled"] is True and details["sample_window_seconds"] == 60


def test_repeated_429s_write_one_representative_event_per_kind(http):
    service = http.app.state.auth_service
    for _ in range(5):
        assert http.post("/api/auth/login", headers=headers("invalid"), json={}).status_code == 403
    before = rate_rows(service.repository)
    for _ in range(30):
        assert http.post("/api/auth/login", headers=headers("invalid"), json={}).status_code == 429
    assert rate_rows(service.repository) == before
    assert event_count(service.repository, "AUTH_RATE_LIMITED") == 1
    assert event_count(service.repository, "AUTH_HTTP_RESULT", 429) == 1


def test_denial_sampling_survives_restart_and_concurrent_workers(setup):
    service, repository, clock = setup
    restarted = AuthService(repository, service.cipher, passwords=service.passwords,
                            clock=clock, policy=service.policy)

    def denial(index):
        (service if index % 2 else restarted).audit_http_auth("csrf", 403)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(denial, range(32)))
    assert event_count(repository, "AUTH_HTTP_RESULT", 403) == 1
    clock.advance(60)
    restarted.audit_http_auth("csrf", 403)
    assert event_count(repository, "AUTH_HTTP_RESULT", 403) == 2


@pytest.mark.parametrize("purpose", ["enrollment", "login"])
def test_rate_blocked_mfa_is_sampled_without_fake_factor_rejections(setup, monkeypatch, purpose):
    service, repository, clock = setup
    user = provision_service(service)
    if purpose == "login":
        enroll(service, clock, user)
        clock.advance(300)
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=10)
    client = TrustedClientContext("198.51.100.10")
    challenge = service.begin_login(user.username, PASSWORD, client=client)
    token = challenge.token.get_secret_value()
    if purpose == "enrollment":
        service.begin_mfa_enrollment(token)

    def attempt(target):
        if purpose == "enrollment":
            return target.confirm_mfa_enrollment(token, "invalid", client=client)
        return target.complete_login(token, totp_code="invalid", client=client)

    for _ in range(2):
        with pytest.raises(AuthenticationError):
            attempt(service)
    assert event_count(repository, "MFA_REJECTED") == 2

    def forbidden_factor_check(*args, **kwargs):
        raise AssertionError("rate-blocked requests must not check MFA factors")

    monkeypatch.setattr("webapp.auth_service.matching_totp_step", forbidden_factor_check)
    restarted = AuthService(repository, service.cipher, passwords=service.passwords,
                            clock=clock, policy=service.policy)
    before = rate_rows(repository)
    for _ in range(20):
        with pytest.raises(RateLimitError):
            attempt(restarted)
    assert rate_rows(repository) == before
    assert event_count(repository, "MFA_REJECTED") == 2
    assert event_count(repository, "AUTH_RATE_LIMITED") == 1
    clock.advance(60)
    with pytest.raises(RateLimitError) as error:
        attempt(restarted)
    assert error.value.retry_after == 240
    assert event_count(repository, "AUTH_RATE_LIMITED") == 2
    assert event_count(repository, "MFA_REJECTED") == 2


@pytest.mark.parametrize("operation,window", [("login", 300), ("mfa", 300), ("csrf", 60), ("logout", 60)])
def test_retry_after_uses_remaining_actual_window(http, operation, window):
    service = http.app.state.auth_service
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=10,
                             maintenance_individual_attempts=2, maintenance_global_attempts=10)
    for _ in range(2):
        if operation == "csrf":
            assert http.get("/api/auth/csrf").status_code == 200
        else:
            assert http.post("/api/auth/" + operation, headers=headers("invalid"), json={}).status_code == (401 if operation == "logout" else 403)
    http.clock.advance(17)
    response = (http.get("/api/auth/csrf") if operation == "csrf" else
                http.post("/api/auth/" + operation, headers=headers("invalid"), json={}))
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(window - 17)
    assert not response.headers.get_list("set-cookie")


def test_retry_after_also_reflects_service_account_limit(http):
    service = http.app.state.auth_service
    service.policy = replace(service.policy, individual_attempts=2, global_attempts=10)
    client = TrustedClientContext("198.51.100.10")
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            service.begin_login("fictional.unknown", PASSWORD, client=client)
    http.clock.advance(73)
    proof = bootstrap(http)
    response = http.post("/api/auth/login", headers=headers(proof), json={
        "username": "fictional.unknown", "password": PASSWORD,
    })
    assert response.status_code == 429 and response.headers["retry-after"] == "227"


def test_significant_auth_audits_are_not_sampled(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    assert http.post("/api/auth/logout", headers=headers(proof), json={}).status_code == 204
    service = http.app.state.auth_service
    for event, expected in [("PASSWORD_VERIFIED", 2), ("MFA_ENROLLED", 1),
                            ("SESSION_GRANTED", 2), ("SESSION_REVOKED", 2)]:
        assert event_count(service.repository, event) == expected
    with service.repository._connect() as connection:
        assert connection.execute("SELECT revoked_at FROM auth_sessions WHERE token_hash=?",
                                  (token_digest(token),)).fetchone()[0] is not None


def test_plain_http_auth_is_rejected_before_rate_reservation(http):
    before = rate_rows(http.app.state.auth_service.repository)
    response = http.get("http://testserver/api/auth/csrf")
    assert response.status_code == 400 and not response.headers.get_list("set-cookie")
    assert rate_rows(http.app.state.auth_service.repository) == before


@pytest.mark.parametrize("fetch_headers", [{}, {"Sec-Fetch-Site": "same-origin"}])
def test_same_origin_and_headerless_clients_remain_compatible(http, fetch_headers):
    response = http.get("/api/auth/csrf", headers=fetch_headers)
    assert response.status_code == 200 and response.json()["state"] == "preauth"


def test_denial_sampling_lookup_is_bounded_when_audit_history_is_busy(setup):
    service, repository, clock = setup
    service.audit_http_auth("csrf", 403)
    with repository.transaction() as connection:
        connection.executemany(
            "INSERT INTO auth_events(event_type,occurred_at,details_json) VALUES (?,?,?)",
            [("FICTIONAL_TEST_LOAD", int(clock().timestamp()), "{}") for _ in range(4097)],
        )
    # An evicted marker permits one extra sample, then suppresses repeats again.
    service.audit_http_auth("csrf", 403)
    service.audit_http_auth("csrf", 403)
    assert event_count(repository, "AUTH_HTTP_RESULT", 403) == 2
