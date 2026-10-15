"""Real HTTPS endpoint lifecycle with fictional, offline-enrolled identities."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from uuid import uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient

from http_auth_support import TEST_PASSWORD, create_test_app, settings_for
from webapp.auth_crypto import CryptoBusyError, PasswordService
from webapp.auth_csrf import COOKIE_NAMES, MFA_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE
from webapp.auth_models import UserRole
from webapp.auth_service import AuthenticationError, AuthPolicy, TrustedClientContext
from webapp.auth_settings import AuthConfigurationError
from workshop_support import TEST_WORKSHOP


ORIGIN = "https://testserver"


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


@pytest.fixture
def http(tmp_path, passwords):
    app = create_test_app(tmp_path / "works.db")
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["X-Carbit-Auth"] = "bootstrap"
        service = app.state.auth_service
        service.passwords = passwords
        client.clock = Clock()
        service.clock = client.clock
        yield client


def provision(http, role=UserRole.OWNER):
    service = http.app.state.auth_service
    user = service.provision_identity(username="fictional." + uuid4().hex, display_name="Fictional HTTP user",
                                      role=role, password=TEST_PASSWORD)
    service.workshop_directory.grant_membership(user.user_id, TEST_WORKSHOP,
        operator_reference="Explicit HTTP lifecycle fixture membership")
    client = TrustedClientContext(str(user.user_id))
    challenge = service.begin_login(user.username, TEST_PASSWORD, client=client)
    token = challenge.token.get_secret_value()
    enrollment = service.begin_mfa_enrollment(token)
    secret = enrollment.secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(http.clock()), client=client)
    service.revoke_session(grant.session.token.get_secret_value())
    http.clock.advance(60)
    return user, secret, tuple(code.get_secret_value() for code in grant.recovery_codes)


def headers(proof):
    return {"Origin": ORIGIN, "X-CSRF-Token": proof}


def bootstrap(http):
    response = http.get("/api/auth/csrf", headers={"X-Carbit-Auth": "bootstrap"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["csrf_token"]


def begin(http, user):
    proof = bootstrap(http)
    response = http.post("/api/auth/login", headers=headers(proof), json={"username": user.username, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "mfa"
    return response.json()["csrf_token"]


def complete(http, user, secret, *, recovery=None):
    proof = begin(http, user)
    payload = {"recovery_code": recovery} if recovery else {"totp_code": pyotp.TOTP(secret).at(http.clock())}
    response = http.post("/api/auth/mfa", headers=headers(proof), json=payload)
    assert response.status_code == 200, response.text
    return response


def snapshot(http):
    return {cookie.name: cookie.value for cookie in http.cookies.jar if cookie.name in COOKIE_NAMES}


def cookie_header(values):
    return "; ".join(name + "=" + value for name, value in values.items())


def rows(http, table):
    assert table in {"auth_sessions", "auth_challenges", "auth_events", "auth_users", "auth_recovery_codes"}
    with http.app.state.auth_service.repository._connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM " + table)]


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN])
def test_complete_http_lifecycle_and_role_isolation(http, role):
    user, secret, _ = provision(http, role)
    first = bootstrap(http)
    initial_context = http.cookies.get(PREAUTH_COOKIE)
    response = http.post("/api/auth/login", headers=headers(first), json={"username": user.username, "password": TEST_PASSWORD})
    assert response.status_code == 200
    assert http.cookies.get(PREAUTH_COOKIE) != initial_context
    assert not http.cookies.get(SESSION_COOKIE)
    assert http.get("/api/works").status_code == 401
    mfa_proof = response.json()["csrf_token"]
    response = http.post("/api/auth/mfa", headers=headers(mfa_proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 200
    assert response.json() == {"state": "session"}
    token = http.cookies.get(SESSION_COOKIE)
    assert not http.cookies.get(PREAUTH_COOKIE) and not http.cookies.get(MFA_COOKIE)
    assert token not in response.text
    response = http.get("/api/auth/session", headers={"X-Role": "OWNER", "X-User-Id": str(uuid4())})
    assert response.status_code == 200
    assert response.json()["user"]["user_id"] == str(user.user_id)
    assert response.json()["user"]["role"] == role.value
    assert response.json()["absolute_expires_at"] - int(http.clock().timestamp()) == 8 * 3600
    assert response.json()["idle_expires_at"] - int(http.clock().timestamp()) == 1800
    assert token not in response.text
    assert http.get("/api/works").status_code == 200
    payload = {"registration": "TEST-ONLY", "customer_description": "Fictional work", "actor": "Untrusted legacy text", "idempotency_key": str(uuid4())}
    assert http.post("/api/works", headers=headers(bootstrap(http)), json=payload).status_code == (403 if role == UserRole.IT_ADMIN else 201)
    proof = bootstrap(http)
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 204 and not response.content
    assert not snapshot(http)
    with pytest.raises(AuthenticationError):
        http.app.state.auth_service.require_session(token)
    assert http.get("/api/works").status_code == 401
    assert http.get("/api/auth/session").status_code == 401
    assert any(row["event_type"] == "SESSION_REVOKED" for row in rows(http, "auth_events"))


def test_cookie_flags_expiry_and_deletion(http):
    user, secret, _ = provision(http)
    bootstrap_response = http.get("/api/auth/csrf")
    proof = bootstrap_response.json()["csrf_token"]
    login = http.post("/api/auth/login", headers=headers(proof), json={"username": user.username, "password": TEST_PASSWORD})
    mfa = http.post("/api/auth/mfa", headers=headers(login.json()["csrf_token"]), json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    logout = http.post("/api/auth/logout", headers=headers(bootstrap(http)), json={})
    for response in (bootstrap_response, login, mfa, logout):
        assert response.headers["cache-control"] == "no-store"
        for value in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie(value)
            assert len(parsed) == 1
            morsel = next(iter(parsed.values()))
            assert morsel["secure"] and morsel["httponly"]
            assert morsel["samesite"] == "strict" and morsel["path"] == "/" and not morsel["domain"]
            assert morsel["expires"]
    assert {next(iter(SimpleCookie(v).values()))["max-age"] for v in login.headers.get_list("set-cookie")} == {"600", "300"}
    assert next(iter(SimpleCookie(mfa.headers.get_list("set-cookie")[-1]).values()))["max-age"] == "28800"
    assert len(logout.headers.get_list("set-cookie")) == 3
    assert all(next(iter(SimpleCookie(value).values()))["max-age"] == "0" for value in logout.headers.get_list("set-cookie"))


@pytest.mark.parametrize("kind", ["unknown", "wrong", "disabled", "unenrolled", "openclaw"])
def test_rejected_accounts_have_generic_response_and_no_session(http, kind):
    service = http.app.state.auth_service
    if kind == "unknown":
        username = "fictional.missing"
    elif kind == "openclaw":
        username = service.provision_identity(username="fictional.service", display_name="Fictional service", role=UserRole.OPENCLAW).username
    elif kind == "unenrolled":
        username = service.provision_identity(username="fictional.pending", display_name="Fictional unenrolled", role=UserRole.OWNER, password=TEST_PASSWORD).username
    else:
        user, _, _ = provision(http)
        username = user.username
        if kind == "disabled":
            with service.repository.transaction() as connection:
                connection.execute("UPDATE auth_users SET enabled=0 WHERE user_id=?", (str(user.user_id),))
    proof = bootstrap(http)
    response = http.post("/api/auth/login", headers=headers(proof), json={"username": username, "password": "Fictional wrong passphrase" if kind == "wrong" else TEST_PASSWORD})
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication failed"}
    assert not response.headers.get_list("set-cookie") and not http.cookies.get(MFA_COOKIE)
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))
    assert username not in response.text and TEST_PASSWORD not in response.text


@pytest.mark.parametrize("endpoint", ["login", "mfa", "logout"])
@pytest.mark.parametrize("origin", [None, "null", "https://evil.example", ORIGIN + "/", ORIGIN.upper()])
def test_auth_writes_require_exact_single_origin(http, endpoint, origin):
    proof = bootstrap(http)
    request_headers = {"X-CSRF-Token": proof}
    if origin is not None:
        request_headers["Origin"] = origin
    response = http.post("/api/auth/" + endpoint, headers=request_headers, json={})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    assert rows(http, "auth_events")[-1]["event_type"] == "AUTH_HTTP_RESULT"


@pytest.mark.parametrize("duplicate", ["Origin", "Host", "X-CSRF-Token", "X-Carbit-Auth", "Content-Type", "Content-Length", "Authorization", "Cookie"])
def test_duplicate_security_headers_rejected(http, duplicate):
    proof = bootstrap(http)
    values = {"Origin": ORIGIN, "Host": "testserver", "X-CSRF-Token": proof, "X-Carbit-Auth": "bootstrap", "Content-Type": "application/json",
              "Content-Length": "2", "Authorization": "fictional", "Cookie": cookie_header(snapshot(http))}
    request_headers = [(name, value) for name, value in headers(proof).items() if name.lower() != duplicate.lower()]
    request_headers += [(duplicate, values[duplicate])] * 2
    response = http.post("/api/auth/login", headers=request_headers, content="{}")
    assert response.status_code == (403 if duplicate == "Origin" else 400)
    assert not response.headers.get_list("set-cookie")


@pytest.mark.parametrize("name", COOKIE_NAMES)
def test_duplicate_security_cookie_pairs_rejected_before_credentials(http, name):
    proof = bootstrap(http)
    response = http.post("/api/auth/login", headers={**headers(proof), "Cookie": f"{name}=fictional; {name}=fictional"}, json={})
    assert response.status_code == 400
    assert not rows(http, "auth_challenges")
    assert http.get("/api/works", headers={"Cookie": f"{name}=fictional; {name}=fictional"}).status_code == 400


@pytest.mark.parametrize("value", ['__Host-carbit-preauth="quoted"', "__Host-carbit-session=one,two", "__Host-carbit-session=", "__Host-carbit-session= space", "broken", "__Host-carbit-preauth=" + "A" * 513])
def test_malformed_security_cookies_rejected(http, value):
    assert http.get("/api/auth/csrf", headers={"Cookie": value}).status_code == 400


@pytest.mark.parametrize("endpoint", ["login", "mfa", "logout"])
def test_missing_or_invalid_csrf_precedes_body_validation(http, endpoint):
    user, secret, _ = provision(http)
    if endpoint == "login":
        bootstrap(http)
    elif endpoint == "mfa":
        begin(http, user)
    else:
        complete(http, user, secret)
    for proof in (None, "fictional.invalid"):
        request_headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        if proof:
            request_headers["X-CSRF-Token"] = proof
        response = http.post("/api/auth/" + endpoint, headers=request_headers, content=b"invalid-json-secret")
        assert response.status_code == 403 and "secret" not in response.text
        assert not response.headers.get_list("set-cookie")


def test_cross_flow_and_cross_user_challenge_substitution(http):
    first, _, _ = provision(http)
    second, second_secret, _ = provision(http)
    first_proof = begin(http, first)
    first_cookies = snapshot(http)
    http.cookies.clear()
    second_proof = begin(http, second)
    second_cookies = snapshot(http)
    for substitutions, proof in (
        ({**first_cookies, MFA_COOKIE: second_cookies[MFA_COOKIE]}, second_proof),
        ({**second_cookies, MFA_COOKIE: first_cookies[MFA_COOKIE]}, first_proof),
        (second_cookies, first_proof),
    ):
        response = http.post("/api/auth/mfa", headers={**headers(proof), "Cookie": cookie_header(substitutions)}, json={"totp_code": pyotp.TOTP(second_secret).at(http.clock())})
        assert response.status_code == 403
    response = http.post("/api/auth/mfa", headers=headers(second_proof), json={"totp_code": pyotp.TOTP(second_secret).at(http.clock()), "user_id": str(first.user_id)})
    assert response.status_code == 422
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))


def test_same_user_independent_flows_cannot_exchange_challenges(http):
    user, secret, _ = provision(http)
    first_proof = begin(http, user)
    first = snapshot(http)
    http.cookies.clear()
    begin(http, user)
    second = snapshot(http)
    response = http.post("/api/auth/mfa", headers={**headers(first_proof), "Cookie": cookie_header({**first, MFA_COOKIE: second[MFA_COOKIE]})}, json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 403


def test_password_proof_and_challenge_cannot_fixate_session(http):
    user, secret, _ = provision(http)
    old_proof = bootstrap(http)
    old_context = snapshot(http)
    response = http.post("/api/auth/login", headers=headers(old_proof), json={"username": user.username, "password": TEST_PASSWORD})
    assert response.status_code == 200
    mfa_proof = response.json()["csrf_token"]
    assert http.post("/api/auth/mfa", headers=headers(old_proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 403
    assert http.post("/api/auth/login", headers=headers(old_proof), json={"username": user.username, "password": TEST_PASSWORD}).status_code == 409
    assert http.post("/api/auth/mfa", headers={**headers(mfa_proof), "Cookie": cookie_header(old_context)}, json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 403
    assert http.post("/api/auth/mfa", headers=headers(mfa_proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 200
    token = http.cookies.get(SESSION_COOKIE)
    assert http.post("/api/auth/login", headers=headers(old_proof), json={"username": user.username, "password": TEST_PASSWORD}).status_code == 409
    assert http.cookies.get(SESSION_COOKIE) == token


@pytest.mark.parametrize("seconds", [300, 301])
def test_challenge_expiry_at_five_minutes(http, seconds):
    user, secret, _ = provision(http)
    proof = begin(http, user)
    cookies = snapshot(http)
    http.clock.advance(seconds)
    response = http.post("/api/auth/mfa", headers={**headers(proof), "Cookie": cookie_header(cookies)}, json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 403
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))


def test_consumed_challenge_and_cookie_replay_fail_in_storage(http):
    user, secret, _ = provision(http)
    proof = begin(http, user)
    old = snapshot(http)
    assert http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 200
    token = http.cookies.get(SESSION_COOKIE)
    http.clock.advance(60)
    response = http.post("/api/auth/mfa", headers={**headers(proof), "Cookie": cookie_header(old)}, json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 401 and not response.headers.get_list("set-cookie")
    assert http.cookies.get(SESSION_COOKIE) == token
    assert http.get("/api/auth/csrf", headers={"Cookie": cookie_header(old)}).status_code == 401
    assert len([row for row in rows(http, "auth_sessions") if row["revoked_at"] is None]) == 1


def test_simultaneous_mfa_completion_creates_one_session(http):
    user, secret, _ = provision(http)
    proof = begin(http, user)
    values = snapshot(http)
    request_headers = {**headers(proof), "Cookie": cookie_header(values)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: http.post("/api/auth/mfa", headers=request_headers, json={"totp_code": pyotp.TOTP(secret).at(http.clock())}), range(2)))
    assert sorted(response.status_code for response in results) == [200, 401]
    assert len([row for row in rows(http, "auth_sessions") if row["revoked_at"] is None]) == 1


def test_recovery_code_http_login_is_single_use(http):
    user, secret, recovery = provision(http)
    complete(http, user, secret, recovery=recovery[0])
    assert rows(http, "auth_sessions")[-1]["factor"] == "RECOVERY"
    http.post("/api/auth/logout", headers=headers(bootstrap(http)), json={})
    proof = begin(http, user)
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"recovery_code": recovery[0]})
    assert response.status_code == 401
    assert not http.cookies.get(SESSION_COOKIE)


@pytest.mark.parametrize("state", ["absolute", "idle", "revoked", "disabled", "version", "mfa_disabled"])
def test_invalid_server_sessions_fail_and_bootstrap_only_after_confirmed_invalidity(http, state):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    proof = bootstrap(http)
    service = http.app.state.auth_service
    if state == "absolute":
        http.clock.advance(8 * 3600)
    elif state == "idle":
        http.clock.advance(1800)
    elif state == "revoked":
        service.revoke_session(token)
    else:
        with service.repository.transaction() as connection:
            statement = {"disabled": "enabled=0", "version": "auth_version=auth_version+1", "mfa_disabled": "mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL"}[state]
            connection.execute("UPDATE auth_users SET " + statement + " WHERE user_id=?", (str(user.user_id),))
    request_cookies = {"Cookie": cookie_header({SESSION_COOKIE: token})}
    assert http.get("/api/auth/session", headers=request_cookies).status_code == 401
    response = http.post("/api/auth/logout", headers={**headers(proof), **request_cookies}, json={})
    assert response.status_code == 401 and not response.headers.get_list("set-cookie")
    response = http.get("/api/auth/csrf", headers=request_cookies)
    assert response.status_code == 200 and response.json()["state"] == "preauth"
    assert response.headers.get_list("set-cookie")


def test_csrf_deadlines_and_failed_logout_do_not_extend_idle_or_absolute_expiry(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    service = http.app.state.auth_service
    before = service.inspect_session(token)
    http.clock.advance(1790)
    short = http.app.state.csrf_protector.issue_proof("session", ORIGIN, session_token=token,
                                                     session_absolute_expiry=before.absolute_expiry,
                                                     session_idle_expiry=before.idle_expiry)
    assert int(short.split(".")[4]) == int(before.idle_expiry.timestamp())
    assert http.post("/api/auth/logout", headers=headers("invalid"), json={}).status_code == 403
    assert service.inspect_session(token).idle_expiry == before.idle_expiry
    proof = bootstrap(http)
    assert int(proof.split(".")[4]) - int(http.clock().timestamp()) == 600
    assert service.inspect_session(token).absolute_expiry == before.absolute_expiry
    http.clock.advance(600)
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")


def test_preauth_proof_expires_at_ten_minutes(http):
    proof = bootstrap(http)
    old = snapshot(http)
    assert int(proof.split(".")[4]) - int(proof.split(".")[3]) == 600
    http.clock.advance(600)
    response = http.post("/api/auth/login", headers={**headers(proof), "Cookie": cookie_header(old)}, json={"username": "fictional.missing", "password": TEST_PASSWORD})
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ["csrf", "session", "logout"])
def test_storage_failure_never_becomes_anonymous_or_clears_live_cookie(http, monkeypatch, endpoint):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    proof = bootstrap(http)
    token = http.cookies.get(SESSION_COOKIE)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fictional-sensitive-db-path")
    monkeypatch.setattr(http.app.state.auth_service, "inspect_session", unavailable)
    response = http.post("/api/auth/logout", headers=headers(proof), json={}) if endpoint == "logout" else http.get("/api/auth/" + endpoint)
    assert response.status_code == 503 and "sensitive" not in response.text
    assert not response.headers.get_list("set-cookie") and http.cookies.get(SESSION_COOKIE) == token


def test_logout_revocation_failure_keeps_cookie_for_retry(http, monkeypatch):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    proof = bootstrap(http)
    token = http.cookies.get(SESSION_COOKIE)
    def unavailable(*args):
        raise sqlite3.OperationalError("fictional-sensitive-error")
    monkeypatch.setattr(http.app.state.auth_service, "revoke_session", unavailable)
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 503 and not response.headers.get_list("set-cookie")
    assert http.app.state.auth_service.require_session(token)


def test_invalid_http_requests_are_persistently_limited_and_audited(http):
    for _ in range(5):
        assert http.post("/api/auth/login", headers={"Origin": ORIGIN}, json={}).status_code == 403
    response = http.post("/api/auth/login", headers={"Origin": ORIGIN, "X-Forwarded-For": str(uuid4())}, json={})
    assert response.status_code == 429
    events = rows(http, "auth_events")
    assert sum(event["event_type"] == "AUTH_HTTP_RESULT" for event in events) == 2
    assert any(event["event_type"] == "AUTH_RATE_LIMITED" for event in events)
    http.clock.advance(300)
    assert http.post("/api/auth/login", headers={"Origin": ORIGIN}, json={}).status_code == 403


def test_password_and_mfa_service_limits_remain_at_http_boundary(http):
    user, secret, _ = provision(http)
    # Offline enrollment legitimately consumed a password attempt for this
    # account; start a fresh window to measure the HTTP account budget.
    http.clock.advance(300)
    proof = bootstrap(http)
    for _ in range(5):
        assert http.post("/api/auth/login", headers=headers(proof), json={"username": user.username, "password": "Fictional incorrect passphrase"}).status_code == 401
    assert http.post("/api/auth/login", headers=headers(proof), json={"username": user.username, "password": TEST_PASSWORD}).status_code == 429
    http.clock.advance(300)
    proof = begin(http, user)
    for _ in range(5):
        assert http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": "bad"}).status_code == 401
    assert http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 429


@pytest.mark.parametrize("payload,status", [
    (b'{"username":"secret","username":"second","password":"secret"}', 422),
    (b'{"username":"secret","password":NaN}', 422),
    (b'[]', 422), (b'null', 422), (b'{"username":1,"password":"secret"}', 422),
    (b'{"username":"secret","password":"secret","role":"OWNER"}', 422),
    (b'\xff', 422), (b'X' * 4097, 413),
])
def test_bounded_unambiguous_json_has_no_credential_echo(http, payload, status):
    proof = bootstrap(http)
    response = http.post("/api/auth/login", headers={**headers(proof), "Content-Type": "application/json"}, content=payload)
    assert response.status_code == status and "secret" not in response.text
    assert not rows(http, "auth_challenges")


def test_unsupported_content_types_query_credentials_and_headers(http):
    proof = bootstrap(http)
    assert http.post("/api/auth/login", headers=headers(proof), data={"username": "fictional", "password": "secret"}).status_code == 415
    assert http.post("/api/auth/login?password=fictional", headers=headers(proof), json={}).status_code == 422
    assert http.post("/api/auth/login", headers={**headers(proof), "Authorization": "Bearer fictional"}, json={}).status_code == 400
    assert http.post("/api/auth/login", headers={**headers(proof), "Transfer-Encoding": "chunked"}, json={}).status_code == 400


def test_missing_configuration_or_crypto_capacity_fails_closed(http, monkeypatch):
    proof = bootstrap(http)
    def busy(*args, **kwargs):
        raise CryptoBusyError("fictional-sensitive-error")
    monkeypatch.setattr(http.app.state.auth_service.passwords, "verify", busy)
    response = http.post("/api/auth/login", headers=headers(proof), json={"username": "fictional.missing", "password": TEST_PASSWORD})
    assert response.status_code == 503 and "sensitive" not in response.text
    monkeypatch.setattr(http.app.state, "csrf_protector", None)
    assert http.get("/api/auth/csrf").status_code == 503
    monkeypatch.setattr(http.app.state, "auth_settings", None)
    assert http.get("/api/auth/session").status_code == 503


@pytest.mark.parametrize("policy", [AuthPolicy(session_seconds=28801), AuthPolicy(idle_seconds=1801), AuthPolicy(challenge_seconds=301)])
def test_configuration_cannot_extend_approved_lifetimes(tmp_path, policy):
    with pytest.raises(AuthConfigurationError, match="lifetimes"):
        replace(settings_for(tmp_path / "auth.db"), policy=policy)


def test_no_bearer_secrets_in_json_or_auth_events(http):
    user, secret, recovery = provision(http)
    proof = begin(http, user)
    old = snapshot(http)
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"recovery_code": recovery[0]})
    token = http.cookies.get(SESSION_COOKIE)
    session = http.get("/api/auth/session")
    events = json.dumps(rows(http, "auth_events"))
    for sensitive in (TEST_PASSWORD, secret, recovery[0], token, *old.values()):
        assert sensitive not in response.text and sensitive not in session.text and sensitive not in events


def test_mixed_cookie_states_are_rejected_without_issuing_or_clearing_cookies(http):
    user, secret, _ = provision(http)
    begin(http, user)
    old = snapshot(http)
    proof = bootstrap(http)
    assert http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())}).status_code == 200
    mixed = {**old, SESSION_COOKIE: http.cookies.get(SESSION_COOKIE)}
    response = http.get("/api/auth/csrf", headers={"Cookie": cookie_header(mixed)})
    assert response.status_code == 409 and not response.headers.get_list("set-cookie")


def test_anonymous_bootstrap_does_not_accept_client_supplied_challenge(http):
    proof = bootstrap(http)
    assert http.post("/api/auth/mfa", headers=headers(proof), json={"challenge_token": "fictional", "totp_code": "123456"}).status_code == 403
    assert not rows(http, "auth_sessions")


@pytest.mark.parametrize("change", ["consumed", "disabled", "version", "mfa_disabled"])
def test_persisted_challenge_invalidation_rejects_valid_crypto(http, change):
    user, secret, _ = provision(http)
    proof = begin(http, user)
    service = http.app.state.auth_service
    with service.repository.transaction() as connection:
        if change == "consumed":
            connection.execute("UPDATE auth_challenges SET consumed_at=? WHERE purpose='LOGIN'", (service._now(),))
        else:
            statement = {"disabled": "enabled=0", "version": "auth_version=auth_version+1", "mfa_disabled": "mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL"}[change]
            connection.execute("UPDATE auth_users SET " + statement + " WHERE user_id=?", (str(user.user_id),))
    response = http.get("/api/auth/csrf")
    assert response.status_code == 401 and not response.headers.get_list("set-cookie")
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 401
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))


def test_recovery_code_from_different_user_cannot_complete_bound_challenge(http):
    first, _, first_recovery = provision(http)
    second, _, _ = provision(http)
    proof = begin(http, second)
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"recovery_code": first_recovery[0]})
    assert response.status_code == 401
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))
    assert all(row["consumed_at"] is None for row in rows(http, "auth_recovery_codes") if row["user_id"] == str(first.user_id))


def test_auth_http_mfa_totp_step_replay_fails_even_with_new_challenge(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    assert http.post("/api/auth/logout", headers=headers(bootstrap(http)), json={}).status_code == 204
    proof = begin(http, user)
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 401
    assert not http.cookies.get(SESSION_COOKIE)


def test_boundary_audit_failure_after_revocation_does_not_claim_success(http, monkeypatch):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    proof = bootstrap(http)
    token = http.cookies.get(SESSION_COOKIE)
    def unavailable(*args):
        raise sqlite3.OperationalError("fictional-sensitive-audit-error")
    monkeypatch.setattr(http.app.state.auth_service, "audit_http_auth", unavailable)
    response = http.post("/api/auth/logout", headers=headers(proof), json={})
    assert response.status_code == 503 and not response.headers.get_list("set-cookie")
    assert "sensitive" not in response.text
    with pytest.raises(AuthenticationError):
        http.app.state.auth_service.require_session(token)


def test_unavailable_boundary_storage_never_bootstraps_anonymous_cookie(http, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fictional-sensitive-db-error")
    monkeypatch.setattr(http.app.state.auth_service, "reserve_http_auth", unavailable)
    response = http.get("/api/auth/csrf")
    assert response.status_code == 503 and not response.headers.get_list("set-cookie")
    assert "sensitive" not in response.text and not snapshot(http)


def test_corrupt_encrypted_totp_storage_has_controlled_failure(http):
    user, secret, _ = provision(http)
    proof = begin(http, user)
    with http.app.state.auth_service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET totp_ciphertext=? WHERE user_id=?", ("fictional-sensitive-ciphertext", str(user.user_id)))
    response = http.post("/api/auth/mfa", headers=headers(proof), json={"totp_code": pyotp.TOTP(secret).at(http.clock())})
    assert response.status_code == 503 and "sensitive" not in response.text
    assert not response.headers.get_list("set-cookie")
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))


@pytest.mark.parametrize("payload", [{}, {"totp_code": "123456", "recovery_code": "fictional"}, {"totp_code": 123456}, {"recovery_code": "X" * 41}, {"totp_code": "123456", "challenge_token": "fictional"}])
def test_mfa_payload_is_exact_and_never_accepts_browser_identity(http, payload):
    user, _, _ = provision(http)
    proof = begin(http, user)
    response = http.post("/api/auth/mfa", headers=headers(proof), json=payload)
    assert response.status_code == 422 and not response.headers.get_list("set-cookie")
    assert not any(row["revoked_at"] is None for row in rows(http, "auth_sessions"))


def test_auth_origin_must_match_target_even_when_both_are_configured(tmp_path):
    settings = replace(settings_for(tmp_path / "auth.db"), allowed_hosts=("testserver", "other.testserver"),
                       external_origins=(ORIGIN, "https://other.testserver"))
    app = create_test_app(tmp_path / "works.db", auth_settings=settings)
    with TestClient(app, base_url=ORIGIN) as http:
        proof = bootstrap(http)
        context = snapshot(http)
        response = http.post("https://other.testserver/api/auth/login", headers={**headers(proof), "Cookie": cookie_header(context)}, json={})
        assert response.status_code == 403
        assert http.get("/api/auth/csrf", headers={"Origin": "https://other.testserver"}).status_code == 403
        assert http.get("http://testserver/api/auth/csrf").status_code == 400


def test_session_proof_cannot_be_replayed_with_different_session(http):
    first, first_secret, _ = provision(http)
    second, second_secret, _ = provision(http)
    complete(http, first, first_secret)
    first_token = http.cookies.get(SESSION_COOKIE)
    first_proof = bootstrap(http)
    http.cookies.clear()
    complete(http, second, second_secret)
    second_token = http.cookies.get(SESSION_COOKIE)
    response = http.post("/api/auth/logout", headers=headers(first_proof), json={})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    assert http.app.state.auth_service.require_session(first_token).user_id == first.user_id
    assert http.app.state.auth_service.require_session(second_token).user_id == second.user_id


def test_validated_session_inspection_has_persisted_deadlines_and_no_token(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    service = http.app.state.auth_service
    initial = service.inspect_session(token)
    http.clock.advance(200)
    untouched = service.inspect_session(token)
    assert untouched.idle_expiry == initial.idle_expiry
    refreshed = service.inspect_session(token, touch=True)
    assert refreshed.idle_expiry == http.clock() + timedelta(seconds=1800)
    assert refreshed.absolute_expiry == initial.absolute_expiry
    assert token not in repr(refreshed)


def test_bootstrap_requires_custom_header_and_anonymous_requests_never_delete_session(http):
    http.headers.pop("X-Carbit-Auth")
    response = http.get("/api/auth/csrf")
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    response = http.get("/api/auth/csrf", headers={"X-Carbit-Auth": "wrong"})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    response = http.get("/api/auth/csrf", headers={"X-Carbit-Auth": "bootstrap"})
    assert response.status_code == 200
    assert len(response.headers.get_list("set-cookie")) == 1
    assert next(iter(SimpleCookie(response.headers.get_list("set-cookie")[0]))) == PREAUTH_COOKIE


def test_navigation_with_withheld_session_cookie_cannot_logout_or_poison_flow(http):
    user, secret, _ = provision(http)
    complete(http, user, secret)
    token = http.cookies.get(SESSION_COOKIE)
    http.headers.pop("X-Carbit-Auth")
    http.cookies.clear()  # Simulate SameSite withholding cookies on navigation.
    response = http.get("/api/auth/csrf", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert response.status_code == 403 and not response.headers.get_list("set-cookie")
    assert http.app.state.auth_service.require_session(token).user_id == user.user_id
