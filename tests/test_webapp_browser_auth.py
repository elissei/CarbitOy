"""CP4A browser login with real password/MFA and isolated HTTPS databases."""

from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from playwright.sync_api import expect

from http_auth_support import TEST_PASSWORD, grant_session
from webapp.auth_service import AuthenticationError, TrustedClientContext
from test_webapp_browser import browser, local_app, page, seed, open_work, fill_inquiry, fill_labour, wait_for_held
from webapp.auth_models import UserRole
from workshop_support import TEST_WORKSHOP


@pytest.fixture
def anonymous_page(browser, local_app):
    context = browser.new_context(ignore_https_errors=True)
    tab = context.new_page()
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    yield tab
    tab.unroute_all(behavior="ignoreErrors")
    context.close()


def user_for(app, token=None):
    return app.auth_service.inspect_session(token or app.token).user


def code_for(app, user):
    service = app.auth_service
    with service.repository._connect() as connection:
        encrypted = connection.execute("SELECT totp_ciphertext FROM auth_users WHERE user_id=?", (str(user.user_id),)).fetchone()[0]
    return pyotp.TOTP(service.cipher.decrypt(str(user.user_id), encrypted)).at(service.clock())


def password(tab, app, user=None):
    user = user or user_for(app)
    tab.locator('#password-form [name="username"]').fill(user.username)
    tab.locator('#password-form [name="password"]').fill(TEST_PASSWORD)
    tab.locator('#password-form button').click()
    expect(tab.locator('#mfa-form')).to_be_visible()
    assert tab.locator('#password-form [name="password"]').input_value() == ""


def mfa(tab, app, user=None):
    tab.locator('#mfa-form [name="totp_code"]').fill(code_for(app, user or user_for(app)))
    tab.locator('#mfa-form button').click()
    expect(tab.locator('#business-shell')).to_be_visible()


def assert_business_locked(tab):
    shell = tab.locator('#business-shell')
    expect(shell).to_be_hidden()
    assert shell.evaluate('shell => shell.hidden && shell.inert')
    assert not tab.locator('#new-work-dialog').evaluate('dialog => dialog.open')


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_real_login_mfa_logout_desktop_mobile(anonymous_page, local_app, width, height, tmp_path):
    tab = anonymous_page
    tab.set_viewport_size({"width": width, "height": height})
    errors, auth_requests = [], []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.on("request", lambda request: auth_requests.append(request) if "/api/auth/" in request.url else None)
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    expect(tab.get_by_role("heading", name="Carbit – kirjaudu")).to_be_visible()
    assert tab.locator('#business-shell').count() == 0
    assert tab.evaluate("document.documentElement.scrollWidth <= innerWidth")
    tab.screenshot(path=str(tmp_path / "login.png"), full_page=True)
    password(tab, local_app)
    mfa(tab, local_app)
    cookie = next(cookie for cookie in tab.context.cookies() if cookie["name"] == "__Host-carbit-session")
    assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    fill_inquiry(tab)
    tab.locator('#new-work-form button[type="submit"]').click()
    expect(tab.locator('#detail-registration')).to_have_text("TEST-NEW")
    tab.locator('#logout-button').click()
    expect(tab.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    expect(tab.locator('#business-shell')).not_to_be_visible()
    assert tab.locator('#detail-registration').inner_text() == ""
    assert tab.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    assert tab.url == local_app[0] + "/"
    assert errors == []
    with pytest.raises(AuthenticationError):
        local_app.auth_service.inspect_session(cookie["value"])
    assert not any(cookie["name"].startswith("__Host-carbit-") for cookie in tab.context.cookies())
    for request in auth_requests:
        assert "?" not in request.url
        if request.method == "POST":
            assert request.headers.get("x-csrf-token")
    saved = local_app[1].get_work(local_app[1].list_works()[0]["id"])
    assert saved["events"][0]["actor"] == str(user_for(local_app).user_id)


def test_invalid_password_and_mfa_are_generic_and_inputs_cleared(anonymous_page, local_app):
    tab = anonymous_page
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    tab.locator('[name="username"]').fill("unknown.fictional.employee")
    tab.locator('[name="password"]').fill("Wrong fictional password")
    tab.locator('#password-form button').click()
    expect(tab.locator('#auth-message')).to_contain_text("Kirjautuminen tai vahvistus epäonnistui")
    assert "unknown.fictional" not in tab.locator('#auth-message').inner_text()
    assert tab.locator('[name="password"]').input_value() == ""
    password(tab, local_app)
    tab.locator('[name="totp_code"]').fill("000000")
    tab.locator('#mfa-form button').click()
    expect(tab.locator('#auth-message')).to_contain_text("Kirjautuminen tai vahvistus epäonnistui")
    assert tab.locator('[name="totp_code"]').input_value() == ""
    assert not tab.url.endswith("/" )


@pytest.mark.parametrize("operation", ["login", "mfa", "logout"])
def test_lost_auth_response_requires_explicit_confirmation_no_replay(anonymous_page, local_app, operation):
    tab = anonymous_page
    writes = []
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    if operation != "login":
        password(tab, local_app)
    if operation == "logout":
        mfa(tab, local_app)
    def lost(route):
        writes.append(route.request)
        route.fetch()  # Real server operation, but browser receives no confirmation.
        route.abort()
    tab.route("**/api/auth/" + operation, lost)
    if operation == "login":
        tab.locator('[name="username"]').fill(user_for(local_app).username)
        tab.locator('[name="password"]').fill(TEST_PASSWORD)
        tab.locator('#password-form button').click()
    elif operation == "mfa":
        tab.locator('[name="totp_code"]').fill(code_for(local_app, user_for(local_app)))
        tab.locator('#mfa-form button').click()
    else:
        tab.locator('#logout-button').click()
    expect(tab.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    expect(tab.locator('#auth-message')).to_contain_text("Vastausta ei voitu vahvistaa")
    assert len(writes) == 1
    assert tab.locator('#business-shell:visible').count() == 0
    if operation == "logout":
        assert_business_locked(tab)
    assert tab.locator('[name="password"]').input_value() == ""
    assert tab.locator('[name="totp_code"]').input_value() == ""


@pytest.mark.parametrize("status", [403, 429, 503])
def test_bootstrap_failures_are_safe_without_polling(anonymous_page, local_app, status):
    requests = []
    def reject(route):
        requests.append(route.request)
        route.fulfill(status=status, json={"detail": "SENSITIVE-FAKE-SERVER-DETAIL"})
    anonymous_page.route("**/api/auth/csrf", reject)
    anonymous_page.goto(local_app[0] + "/login", wait_until="networkidle")
    expect(anonymous_page.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    assert "SENSITIVE" not in anonymous_page.locator('body').inner_text()
    assert len(requests) == 1
    assert anonymous_page.locator('#password-form').is_hidden()


def test_expired_session_preserves_same_uuid_draft_and_never_replays(page, local_app):
    app = local_app
    user = user_for(app)
    page.goto(app[0], wait_until="networkidle")
    fill_inquiry(page, "Luonnos odottaa samaa työntekijää")
    key = page.evaluate("state.creationKey")
    requests = []
    page.on("request", lambda request: requests.append(request) if request.method == "POST" and request.url.endswith("/api/works") else None)
    app.auth_service.revoke_session(app.token)
    now = datetime.now(timezone.utc) + timedelta(seconds=60)
    app.auth_service.clock = lambda: now
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    assert_business_locked(page)
    page.locator('#auth-check').click()
    expect(page.locator('#password-form')).to_be_visible()
    password(page, app, user)
    mfa(page, app, user)
    expect(page.locator('#new-work-dialog')).to_be_visible()
    expect(page.locator('[name="customer_description"]')).to_have_value("Luonnos odottaa samaa työntekijää")
    assert page.evaluate("state.creationKey") == key
    assert page.evaluate("!document.querySelector('#business-shell').inert")
    assert page.evaluate("state.selected === null")
    assert page.locator('#event-list').inner_text() == ""
    assert requests == [] and app[1].list_works() == []
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator('#new-work-dialog')).not_to_be_visible()
    assert len(requests) == 1 and requests[0].post_data_json["idempotency_key"] == key


def test_identity_change_discards_old_business_data_and_labour_draft(page, local_app):
    app = local_app
    work = seed(app[1], "TEST-OLD")
    page.goto(app[0], wait_until="networkidle")
    open_work(page, "TEST-OLD")
    assert page.locator('#event-list').inner_text()
    fill_labour(page, "Edellisen käyttäjän luonnos")
    fill_inquiry(page, "Edellisen käyttäjän tiedusteluluonnos")
    assert page.evaluate("state.creationKey !== null")
    app.auth_service.revoke_session(app.token)
    app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    other = grant_session(app.auth_service)
    app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=120)
    # Fail the read: no business mutation is attempted.
    page.evaluate("loadWorks().catch(() => {})")
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    assert_business_locked(page)
    page.locator('#auth-check').click()
    password(page, app, other.user)
    mfa(page, app, other.user)
    assert page.evaluate("state.selected === null && state.creationKey === null")
    assert page.locator('#detail-description').inner_text() == ""
    assert page.locator('#labour-form [name="work_description"]').input_value() == ""
    for field in ["detail-registration", "detail-description", "detail-created", "price-summary", "event-list", "toast"]:
        assert page.locator('#' + field).inner_text() == ""
    assert page.locator('#new-work-form [name="registration"]').input_value() == ""
    assert page.locator('#new-work-form [name="customer_description"]').input_value() == ""
    assert page.locator('#labour-form').get_attribute('data-work-id') == ""
    assert page.locator('#labour-form').get_attribute('data-version') == ""
    assert not page.locator('#new-work-dialog').evaluate('dialog => dialog.open')
    assert page.evaluate("!document.querySelector('#business-shell').inert")
    assert app[1].get_work(work["id"])["version"] == 1


def test_same_uuid_labour_draft_keeps_original_version(page, local_app):
    app = local_app
    work = seed(app[1], "TEST-DRAFT")
    user = user_for(app)
    page.goto(app[0], wait_until="networkidle")
    open_work(page, "TEST-DRAFT")
    fill_labour(page, "Tallentamaton työaika", "2.50")
    # Cache a valid proof first so the attempted write really reaches the server.
    page.evaluate("sessionProof()")
    writes = []
    page.on("request", lambda request: writes.append(request) if request.method == "PUT" and request.url.endswith("/labour") else None)
    app.auth_service.revoke_session(app.token)
    app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    assert_business_locked(page)
    assert len(writes) == 1
    page.locator('#auth-check').click()
    password(page, app, user)
    mfa(page, app, user)
    expect(page.locator('#labour-form [name="work_description"]')).to_have_value("Tallentamaton työaika")
    expect(page.locator('#labour-form [name="hours"]')).to_have_value("2.50")
    expect(page.locator('#labour-form')).to_have_attribute("data-version", "1")
    assert len(writes) == 1  # Authentication must not replay the rejected PUT.
    assert page.evaluate("state.creationKey === null")
    assert page.locator('#new-work-form [name="customer_description"]').input_value() == ""
    assert page.evaluate("!document.querySelector('#business-shell').inert")
    assert app[1].get_work(work["id"])["version"] == 1
    assert len(app[1].get_work(work["id"])["events"]) == 1


@pytest.mark.parametrize("role", [UserRole.IT_ADMIN, UserRole.SERVICE_MANAGER])
def test_role_aware_login_controls(anonymous_page, local_app, role):
    app = local_app
    grant = grant_session(app.auth_service, role)
    app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=120)
    anonymous_page.goto(app[0] + "/login", wait_until="networkidle")
    password(anonymous_page, app, grant.user)
    mfa(anonymous_page, app, grant.user)
    if role == UserRole.IT_ADMIN:
        expect(anonymous_page.locator('#session-user')).to_contain_text("Vain luku")
        expect(anonymous_page.locator('#new-work-button')).not_to_be_visible()
        assert anonymous_page.evaluate("CarbitAuth.canWrite() === false")
    else:
        expect(anonymous_page.locator('#new-work-button')).to_be_visible()
        assert anonymous_page.evaluate("CarbitAuth.canWrite() === true")


def test_local_expiry_hides_ui_without_any_auth_polling(page, local_app):
    calls = []
    page.on("request", lambda request: calls.append(request) if "/api/auth/" in request.url else None)
    page.clock.install()
    page.goto(local_app[0], wait_until="networkidle")
    assert len(calls) == 1 and calls[0].url.endswith("/session")
    page.clock.fast_forward(1801000)
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    assert len(calls) == 1
    assert_business_locked(page)


def test_mfa_timeout_never_grants_session(anonymous_page, local_app):
    tab = anonymous_page
    tab.clock.install()
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    password(tab, local_app)
    tab.clock.fast_forward(361000)
    expect(tab.locator('#auth-panel')).to_have_attribute("data-state", "expired")
    assert tab.locator('#business-shell').count() == 0


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.OPENCLAW])
def test_unenrolled_or_nonhuman_account_cannot_enter_browser_app(anonymous_page, local_app, role):
    app = local_app
    user = app.auth_service.provision_identity(username="test.unavailable", display_name="Fictional unavailable user", role=role,
                                               password=TEST_PASSWORD if role == UserRole.OWNER else None)
    anonymous_page.goto(app[0] + "/login", wait_until="networkidle")
    anonymous_page.locator('[name="username"]').fill(user.username)
    anonymous_page.locator('[name="password"]').fill(TEST_PASSWORD)
    anonymous_page.locator('#password-form button').click()
    expect(anonymous_page.locator('#auth-message')).to_contain_text("Kirjautuminen tai vahvistus epäonnistui")
    assert anonymous_page.locator('#mfa-form').is_hidden()
    assert anonymous_page.locator('#business-shell').count() == 0


def test_tampered_login_csrf_is_rejected_without_authenticated_ui(anonymous_page, local_app):
    tab = anonymous_page
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    tab.route("**/api/auth/login", lambda route: route.continue_(headers={**route.request.headers, "x-csrf-token": "invalid-proof"}))
    tab.locator('[name="username"]').fill(user_for(local_app).username)
    tab.locator('[name="password"]').fill(TEST_PASSWORD)
    tab.locator('#password-form button').click()
    expect(tab.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    assert tab.locator('#mfa-form').is_hidden()


def test_role_downgrade_locks_write_then_confirms_read_only_without_replay(page, local_app):
    app = local_app
    page.goto(app[0], wait_until="networkidle")
    fill_inquiry(page, "Luonnos säilyy mutta ei tallennu")
    with app.auth_service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET role='IT_ADMIN'")
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "denied")
    assert app[1].list_works() == []
    page.locator('#auth-check').click()
    expect(page.locator('#business-shell')).to_be_visible()
    expect(page.locator('#session-user')).to_contain_text("Vain luku")
    assert page.locator('#new-work-form [name="customer_description"]').input_value() == "Luonnos säilyy mutta ei tallennu"
    assert not page.evaluate("CarbitAuth.canWrite()")


def test_stale_business_response_cannot_restore_previous_user_state(page, local_app):
    from webapp.auth_csrf import SESSION_COOKIE
    app = local_app
    work = seed(app[1], "TEST-STALE")
    page.goto(app[0], wait_until="networkidle")
    held = []
    page.route("**/api/works/" + work["id"], lambda route: held.append((route, route.fetch().json())))
    page.locator('.work-row').filter(has_text="TEST-STALE").click()
    wait_for_held(page, held, 0)
    other = grant_session(app.auth_service)
    page.context.add_cookies([{"name": SESSION_COOKIE, "value": other.token.get_secret_value(), "url": app[0],
                              "secure": True, "httpOnly": True, "sameSite": "Strict"}])
    page.evaluate("CarbitAuth.lock()")
    page.locator('#auth-check').click()
    expect(page.locator('#business-shell')).to_be_visible()
    route, body = held[0]
    route.fulfill(json=body)
    page.wait_for_timeout(100)
    assert page.evaluate("state.selected === null")
    assert page.locator('#detail-description').inner_text() == ""


def test_existing_single_use_recovery_code_can_complete_mfa(anonymous_page, local_app):
    app = local_app
    service = app.auth_service
    user = service.provision_identity(username="test.recovery", display_name="Fictional recovery user", role=UserRole.OWNER, password=TEST_PASSWORD)
    service.workshop_directory.grant_membership(user.user_id, TEST_WORKSHOP,
        operator_reference="Explicit recovery-browser fixture membership")
    client = TrustedClientContext("offline-test-enrollment")
    challenge = service.begin_login(user.username, TEST_PASSWORD, client=client)
    token = challenge.token.get_secret_value()
    enrollment = service.begin_mfa_enrollment(token)
    grant = service.confirm_mfa_enrollment(token, pyotp.TOTP(enrollment.secret.get_secret_value()).at(service.clock()), client=client)
    tab = anonymous_page
    tab.goto(app[0] + "/login", wait_until="networkidle")
    password(tab, app, user)
    tab.locator('#mfa-method').select_option("recovery_code")
    tab.locator('[name="recovery_code"]').fill(grant.recovery_codes[0].get_secret_value())
    tab.locator('#mfa-form button').click()
    expect(tab.locator('#business-shell')).to_be_visible()
    with service.repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM auth_recovery_codes WHERE user_id=? AND consumed_at IS NOT NULL", (str(user.user_id),)).fetchone()[0] == 1
    assert tab.locator('[name="totp_code"]').input_value() == ""


def test_mfa_success_without_session_confirmation_does_not_open_app(anonymous_page, local_app):
    tab = anonymous_page
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    password(tab, local_app)
    tab.route("**/api/auth/session", lambda route: route.fulfill(status=503, json={"detail": "SENSITIVE-FAKE-DETAIL"}))
    tab.locator('[name="totp_code"]').fill(code_for(local_app, user_for(local_app)))
    tab.locator('#mfa-form button').click()
    expect(tab.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    assert tab.locator('#business-shell').count() == 0
    assert "SENSITIVE" not in tab.locator('body').inner_text()


def test_pending_mfa_response_cannot_override_a_locked_generation(anonymous_page, local_app):
    tab = anonymous_page
    tab.goto(local_app[0] + "/login", wait_until="networkidle")
    password(tab, local_app)
    held, confirmations = [], []
    tab.route("**/api/auth/mfa", lambda route: held.append((route, route.fetch())))
    tab.on("request", lambda request: confirmations.append(request) if request.url.endswith("/api/auth/session") else None)
    tab.locator('[name="totp_code"]').fill(code_for(local_app, user_for(local_app)))
    tab.locator('#mfa-form button').click()
    wait_for_held(tab, held, 0)
    tab.evaluate("CarbitAuth.lock('uncertain')")
    route, response = held[0]
    route.fulfill(response=response)
    expect(tab.locator('#auth-message')).to_contain_text("Vastausta ei voitu vahvistaa")
    assert confirmations == []
    assert tab.locator('#business-shell').count() == 0


def test_other_tab_logout_locks_draft_without_business_replay(page, local_app):
    page.goto(local_app[0], wait_until="networkidle")
    fill_inquiry(page, "Välilehden tallentamaton luonnos")
    other = page.context.new_page()
    other.goto(local_app[0], wait_until="networkidle")
    other.locator('#logout-button').click()
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    expect(page.locator('#business-shell')).not_to_be_visible()
    assert_business_locked(page)
    assert page.locator('#new-work-form [name="customer_description"]').input_value() == "Välilehden tallentamaton luonnos"
    assert local_app[1].list_works() == []
    other.close()


def test_explicit_reconciliation_recovers_initial_business_read_failure(page, local_app):
    seed(local_app[1], "TEST-RETRY-READ")
    reads, writes = [], []
    def unavailable_once(route):
        if route.request.method != "GET":
            writes.append(route.request)
            route.continue_()
            return
        reads.append(route.request)
        if len(reads) == 1:
            route.fulfill(status=503, json={"detail": "SENSITIVE-FAKE-STORAGE-DETAIL"})
        else:
            route.continue_()
    page.route("**/api/works", unavailable_once)
    page.goto(local_app[0], wait_until="networkidle")
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    assert "SENSITIVE" not in page.locator('#auth-message').inner_text()
    assert len(reads) == 1
    page.locator('#auth-check').click()
    expect(page.locator('.work-row')).to_contain_text("TEST-RETRY-READ")
    assert len(reads) == 2 and writes == []


@pytest.mark.parametrize("logout_result", ["rejected", "lost_before_server", "lost_after_server"])
def test_uncertain_logout_keeps_business_inert_until_authoritative_confirmation(page, local_app, logout_result):
    app = local_app
    user = user_for(app)
    work = seed(app[1], "TEST-LOGOUT")
    page.goto(app[0], wait_until="networkidle")
    open_work(page, "TEST-LOGOUT")
    history = page.locator('#event-list').text_content()
    assert history
    fill_labour(page, "Uloskirjautumisen tallentamaton luonnos", "2.50")
    logout_requests, writes, held = [], [], []
    page.on("request", lambda request: writes.append(request) if request.method in {"POST", "PUT"} and "/api/works" in request.url else None)

    def uncertain_logout(route):
        logout_requests.append(route.request)
        if logout_result == "rejected":
            route.fulfill(status=503, json={"detail": "SENSITIVE-FAKE-DETAIL"})
        else:
            if logout_result == "lost_after_server":
                response = route.fetch()
                assert response.status == 204
            route.abort()

    page.route("**/api/auth/logout", uncertain_logout)
    page.locator('#logout-button').click()
    expect(page.locator('#auth-panel')).to_have_attribute("data-state", "uncertain")
    assert_business_locked(page)
    assert "SENSITIVE" not in page.locator('#auth-message').inner_text()
    assert len(logout_requests) == 1 and writes == []
    # Retained DOM is inaccessible while locked; neither bootstrap nor MFA success
    # alone may unlock it before the separate authoritative session response.
    page.route("**/api/auth/session", lambda route: held.append((route, route.fetch())))
    if logout_result == "lost_after_server":
        with pytest.raises(AuthenticationError):
            app.auth_service.inspect_session(app.token)
        app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    page.locator('#auth-check').click()
    if logout_result == "lost_after_server":
        expect(page.locator('#password-form')).to_be_visible()
        assert_business_locked(page)
        password(page, app, user)
        assert_business_locked(page)
        page.locator('#mfa-form [name="totp_code"]').fill(code_for(app, user))
        page.locator('#mfa-form button').click()
    wait_for_held(page, held, 0)
    assert_business_locked(page)
    assert page.locator('#detail-registration').inner_text() == "TEST-LOGOUT"
    assert page.locator('#event-list').text_content() == history
    assert writes == [] and len(logout_requests) == 1
    route, response = held[0]
    assert response.status == 200
    assert response.json()["user"]["user_id"] == str(user.user_id)
    route.fulfill(response=response)
    expect(page.locator('#business-shell')).to_be_visible()
    assert page.evaluate("!document.querySelector('#business-shell').inert")
    expect(page.locator('#labour-form [name="work_description"]')).to_have_value("Uloskirjautumisen tallentamaton luonnos")
    expect(page.locator('#labour-form [name="hours"]')).to_have_value("2.50")
    expect(page.locator('#labour-form')).to_have_attribute("data-version", "1")
    assert writes == [] and len(logout_requests) == 1
    assert app[1].get_work(work["id"])["version"] == 1
    assert len(app[1].get_work(work["id"])["events"]) == 1
