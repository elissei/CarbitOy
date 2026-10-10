"""Fail-closed HTTP access: real MFA sessions, explicit HTTPS configuration."""

import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from http_auth_support import TEST_PASSWORD, business_headers, create_test_app, grant_session, settings_for
from webapp.auth_crypto import token_digest
from webapp.auth_models import SessionCookiePolicy, UserRole
from webapp.auth_service import AuthenticationError, TrustedClientContext
from webapp.auth_settings import AuthConfigurationError, AuthSettings
from webapp.http_access import ROUTE_PERMISSIONS, validate_route_coverage
from webapp.main import create_app
from starlette.applications import Starlette


COOKIE = SessionCookiePolicy().name
BUSINESS_REQUESTS = (
    ("GET", "/api/works"), ("POST", "/api/works"),
    ("GET", "/api/works/fictional"), ("PUT", "/api/works/fictional/labour"),
    ("GET", "/"), ("GET", "/static/index.html"),
    ("GET", "/static/app.js"), ("GET", "/static/labour-review-demo.html"),
    ("HEAD", "/static/index.html"),
)


@pytest.fixture
def http(tmp_path):
    app = create_test_app(tmp_path / "works.db")
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def authenticate(http, role=UserRole.OWNER):
    grant = grant_session(http.app.state.auth_service, role)
    http.cookies.set(COOKIE, grant.token.get_secret_value())
    return grant


@pytest.mark.parametrize("method,path", BUSINESS_REQUESTS)
def test_anonymous_requests_rejected_before_business_access(http, monkeypatch, method, path):
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized request reached business storage")
    for operation in ("list_works", "get_work", "create_work", "record_labour"):
        monkeypatch.setattr(http.app.state.repository, operation, forbidden)
    response = http.request(method, path, content=b"invalid request body")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("state", ["invalid", "expired", "idle", "revoked", "disabled", "version", "mfa_incomplete"])
def test_invalid_session_states_cannot_read_or_write(http, state):
    service = http.app.state.auth_service
    authenticate(http)
    now = service._now()
    with service.repository.transaction() as connection:
        if state == "expired":
            connection.execute(
                "UPDATE auth_sessions SET created_at=?,mfa_verified_at=?,expires_at=?,last_seen_at=?",
                (now - 100, now - 100, now - 1, now - 100),
            )
        elif state == "idle":
            connection.execute("UPDATE auth_sessions SET created_at=?,mfa_verified_at=?,last_seen_at=?", (now - 2000,) * 3)
        elif state == "revoked":
            connection.execute("UPDATE auth_sessions SET revoked_at=?", (now,))
        elif state == "disabled":
            connection.execute("UPDATE auth_users SET enabled=0")
        elif state == "version":
            connection.execute("UPDATE auth_users SET auth_version=auth_version+1")
        elif state == "mfa_incomplete":
            connection.execute("UPDATE auth_users SET mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL")
    if state == "invalid":
        http.cookies.set(COOKIE, "not-a-session")
    for method, path in BUSINESS_REQUESTS:
        assert http.request(method, path).status_code == 401
    assert "token" not in http.get("/api/works").text


def test_password_challenge_is_not_a_session(http):
    service = http.app.state.auth_service
    user = service.provision_identity(username="test.pending", display_name="Fictional pending MFA", role=UserRole.OWNER, password=TEST_PASSWORD)
    challenge = service.begin_login(user.username, TEST_PASSWORD, client=TrustedClientContext("test-connection"))
    http.cookies.set(COOKIE, challenge.token.get_secret_value())
    assert http.get("/api/works").status_code == 401


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN])
def test_role_permission_matrix_and_legacy_pricing(http, role):
    authenticate(http, role)
    assert http.get("/api/works").status_code == 200
    assert http.get("/").status_code == 200
    assert http.get("/static/index.html").status_code == 200
    payload = dict(registration="TEST-ONLY", customer_description="Fictional work", actor="Legacy label", idempotency_key=str(uuid4()))
    if role == UserRole.IT_ADMIN:
        work = http.app.state.repository.create_work(**payload)
        assert http.get(f"/api/works/{work['id']}").status_code == 200
        before = http.app.state.repository.get_work(work["id"])
        assert http.post("/api/works", json=payload).status_code == 403
        assert http.put(f"/api/works/{work['id']}/labour", json={}).status_code == 403
        assert http.app.state.repository.get_work(work["id"]) == before
    else:
        response = http.post("/api/works", headers=business_headers(http), json=payload)
        assert response.status_code == 201
        work = response.json()
        assert http.get(f"/api/works/{work['id']}").status_code == 200
        response = http.put(f"/api/works/{work['id']}/labour", headers=business_headers(http), json={
            "version": 1, "hours": "1.60", "work_description": "Fictional Autodata operation", "recorded_by": "Legacy label",
        })
        assert response.status_code == 200
        assert response.json()["total_price"] == "144.00"
    if role == UserRole.IT_ADMIN:
        assert not http.app.state.auth_service.require_session(http.cookies.get(COOKIE)).can_approve_labour


def test_openclaw_has_no_human_login_or_business_access(http):
    service = http.app.state.auth_service
    user = service.provision_identity(username="test.openclaw", display_name="Fictional service", role=UserRole.OPENCLAW)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, TEST_PASSWORD, client=TrustedClientContext("test-service"))
    # Even an erroneously inserted service session is rejected by require_session.
    token = "a" * 43
    now = service._now()
    with service.repository.transaction() as connection:
        connection.execute(
            "INSERT INTO auth_sessions VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (str(uuid4()), token_digest(token), str(user.user_id), 1, "TOTP", now, now, now + 100, now),
        )
    http.cookies.set(COOKIE, token)
    for method, path in BUSINESS_REQUESTS:
        assert http.request(method, path).status_code == 401


def test_browser_identity_headers_do_not_authenticate(http):
    response = http.get("/api/works", headers={"X-User-Id": str(uuid4()), "X-Role": "OWNER", "X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 401


def test_browser_role_cannot_elevate_authenticated_admin(http):
    authenticate(http, UserRole.IT_ADMIN)
    assert http.post("/api/works", headers={"X-Role": "OWNER", "X-User-Id": str(uuid4())}, json={}).status_code == 403


def test_service_revocation_is_effective_at_http_boundary(http):
    grant = authenticate(http)
    http.app.state.auth_service.revoke_session(grant.token.get_secret_value())
    assert http.get("/api/works").status_code == 401


@pytest.mark.parametrize("headers,status", [
    ({"Host": "evil.example"}, 400), ({"Host": "testserver.evil.example"}, 400),
    ({"Origin": "https://evil.example"}, 403), ({"Origin": "null"}, 403),
    ({"Origin": "https://testserver"}, 200),
    ({"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"}, 200),
])
def test_exact_origin_host_and_ignored_forwarded_headers(http, headers, status):
    authenticate(http)
    assert http.get("/api/works", headers=headers).status_code == status


def test_plain_http_is_rejected_even_with_valid_session(http):
    authenticate(http)
    assert http.get("http://testserver/api/works").status_code == 400


def test_route_inventory_and_no_unapproved_sensitive_endpoints(http):
    assert len(validate_route_coverage(http.app)) == len(ROUTE_PERMISSIONS)
    assert set(http.app.openapi()["paths"]) == {"/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour"}
    assert http.get("/api/auth/login").status_code == 405
    assert http.get("/api/auth/mfa").status_code == 405
    for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/api/auth/reset", "/api/auth/enroll", "/api/auth/recover", "/api/users", "/api/works/test/approve"):
        assert http.get(path).status_code == 404
        assert http.post(path).status_code == 404


def test_unclassified_route_stops_startup(tmp_path):
    app = create_test_app(tmp_path / "works.db")
    app.get("/api/accidental")(lambda: {"secret": "should not execute"})
    with pytest.raises(ValueError, match="access policy"):
        with TestClient(app):
            pass
    assert not (tmp_path / "works.db").exists()


def test_nested_application_cannot_replace_static_policy(tmp_path):
    app = create_test_app(tmp_path / "works.db")
    mount = next(route for route in app.routes if route.path == "/static")
    mount.app = Starlette()
    with pytest.raises(ValueError, match="mounted application"):
        with TestClient(app):
            pass


def test_route_added_after_startup_is_denied(http):
    http.app.get("/accidental")(lambda: pytest.fail("unclassified handler executed"))
    assert http.get("/accidental").status_code == 403
    authenticate(http)
    assert http.get("/accidental").status_code == 403


def test_missing_environment_fails_before_database_creation(tmp_path, monkeypatch):
    for name in ("CARBIT_AUTH_DATABASE", "CARBIT_ALLOWED_HOSTS", "CARBIT_EXTERNAL_ORIGINS", "CARBIT_TOTP_KEYS", "CARBIT_CSRF_ACTIVE_KEY_ID", "CARBIT_CSRF_KEYS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthConfigurationError, match="missing"):
        with TestClient(create_app(tmp_path / "missing" / "works.db")):
            pass
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("changes", [
    {"database_path": "relative.db"}, {"allowed_hosts": ()}, {"allowed_hosts": ("*",)},
    {"allowed_hosts": ("testserver:443",)}, {"external_origins": ()},
    {"external_origins": ("http://testserver",)}, {"external_origins": ("https://evil.example",)},
    {"external_origins": ("https://testserver:443",)},
    {"external_origins": ("https://testserver/path",)}, {"external_origins": ("https://user:pass@testserver",)},
    {"totp_keys": ()}, {"totp_keys": (SecretStr("fictional-invalid-key"),)},
])
def test_invalid_configuration_rejected(tmp_path, changes):
    with pytest.raises(AuthConfigurationError):
        replace(settings_for(tmp_path / "auth.db"), **changes)


def test_environment_and_secret_masking(tmp_path, monkeypatch):
    settings = settings_for(tmp_path / "auth.db")
    monkeypatch.setenv("CARBIT_AUTH_DATABASE", str(settings.database_path))
    monkeypatch.setenv("CARBIT_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CARBIT_EXTERNAL_ORIGINS", "https://testserver")
    key = settings.totp_keys[0].get_secret_value()
    monkeypatch.setenv("CARBIT_TOTP_KEYS", key)
    monkeypatch.setenv("CARBIT_CSRF_ACTIVE_KEY_ID", settings.csrf_active_key_id)
    monkeypatch.setenv("CARBIT_CSRF_KEYS", json.dumps([
        {"id": item.key_id, "key": item.secret.get_secret_value()} for item in settings.csrf_keys
    ]))
    assert AuthSettings.from_environment() == settings
    assert key not in repr(settings)


def test_auth_initialization_does_not_provision_accounts_and_runs_once(tmp_path, monkeypatch):
    app = create_test_app(tmp_path / "works.db")
    with TestClient(app, base_url="https://testserver") as http:
        service = app.state.auth_service
        with service.repository._connect() as connection:
            assert connection.execute("SELECT count(*) FROM auth_users").fetchone()[0] == 0
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert http.get("/api/works").status_code == 401
        assert app.state.auth_service is service


def test_auth_storage_error_is_controlled_and_does_not_read_business_data(http, monkeypatch):
    authenticate(http)
    def locked(token):
        raise sqlite3.OperationalError("fictional sensitive internal detail")
    monkeypatch.setattr(http.app.state.auth_service, "require_session", locked)
    monkeypatch.setattr(http.app.state.repository, "list_works", lambda: pytest.fail("business data read"))
    response = http.get("/api/works")
    assert response.status_code == 503
    assert "sensitive" not in response.text
