"""Exact public file boundary; all business/static routes remain protected."""

import pytest
from fastapi.testclient import TestClient

from http_auth_support import create_test_app
from test_webapp_http_access import BUSINESS_REQUESTS


@pytest.fixture
def http(tmp_path):
    with TestClient(create_test_app(tmp_path / "works.db"), base_url="https://testserver") as client:
        yield client


@pytest.mark.parametrize("path,mime", [("/login", "text/html"), ("/login.js", "text/javascript"), ("/login.css", "text/css")])
def test_only_exact_approved_public_gets_have_safe_headers(http, path, mime, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("public login reached business storage")
    monkeypatch.setattr(http.app.state.repository, "list_works", forbidden)
    response = http.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    assert http.head(path).status_code == 405
    assert http.post(path).status_code == 405
    assert http.get("http://testserver" + path).status_code == 400
    assert http.get(path, headers={"Host": "evil.example"}).status_code == 400
    assert http.get(path, headers=[("Origin", "https://testserver"), ("Origin", "https://testserver")]).status_code == 403
    assert http.get(path, headers={"Cookie": "__Host-carbit-session=a; __Host-carbit-session=b"}).status_code == 400


@pytest.mark.parametrize("method,path", BUSINESS_REQUESTS)
def test_public_login_does_not_open_any_existing_protected_route(http, method, path):
    assert http.get("/login").status_code == 200
    assert http.request(method, path).status_code == 401


@pytest.mark.parametrize("path", ["/login.html", "/login/other", "/browser_auth/login.html", "/api/auth/enroll", "/api/auth/recover"])
def test_no_additional_public_or_enrollment_paths(http, path):
    assert http.get(path).status_code == 404


def test_exact_public_policy_and_private_aliases(http):
    from webapp.http_access import ROUTE_PERMISSIONS
    assert {path for (path, method), policy in ROUTE_PERMISSIONS.items() if policy == "public_login"} == {"/login", "/login.js", "/login.css"}
    for path in ["/static/../browser_auth/login.html", "/static/index.html", "/static/app.js", "/static/styles.css"]:
        assert http.get(path).status_code in {401, 404}


def test_public_files_do_not_refresh_authenticated_session_or_issue_auth_cookies(http):
    from http_auth_support import grant_session
    from webapp.auth_csrf import SESSION_COOKIE
    service = http.app.state.auth_service
    grant = grant_session(service)
    token = grant.token.get_secret_value()
    http.cookies.set(SESSION_COOKIE, token)
    before = service.inspect_session(token)
    for path in ["/login", "/login.js", "/login.css"]:
        response = http.get(path)
        assert response.status_code == 200 and "set-cookie" not in response.headers
        assert token not in response.text and str(grant.user.user_id) not in response.text
    assert service.inspect_session(token) == before


def test_public_files_fail_closed_when_runtime_configuration_missing(http, monkeypatch):
    monkeypatch.setattr(http.app.state, "auth_settings", None)
    for path in ["/login", "/login.js", "/login.css"]:
        assert http.get(path).status_code == 503
