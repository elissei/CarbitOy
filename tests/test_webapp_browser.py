"""Local Chromium regressions; install with python -m playwright install chromium."""

import socket
import threading
import time
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from http_auth_support import create_test_app as create_app, grant_session, local_tls_files, settings_for
from webapp.auth_models import SessionCookiePolicy


class LocalApp(tuple):
    def __new__(cls, url, repository, token, auth_service):
        result = super().__new__(cls, (url, repository))
        result.token = token
        result.auth_service = auth_service
        return result


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def local_app(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(tmp_path / "browser.db", auth_settings=settings_for(tmp_path / "auth.db", url))
    cert_path, key_path = local_tls_files(tmp_path)
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="critical", proxy_headers=False,
        ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local test server did not start")
            threading.Event().wait(0.01)
        grant = grant_session(app.state.auth_service)
        yield LocalApp(url, app.state.repository, grant.token.get_secret_value(), app.state.auth_service)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


@pytest.fixture
def page(browser, local_app):
    context = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    context.add_cookies([{
        "name": SessionCookiePolicy().name, "value": local_app.token, "url": local_app[0],
        "secure": True, "httpOnly": True, "sameSite": "Strict",
    }])
    page = context.new_page()
    yield page
    page.unroute_all(behavior="ignoreErrors")
    context.close()


def seed(repository, registration):
    return repository.create_work(
        registration=registration,
        customer_description="Keksitty selaintestin työkuvaus",
        actor="Testikirjaaja", idempotency_key=str(uuid4()),
    )


def open_work(page, registration):
    page.locator(".work-row").filter(has_text=registration).click()
    expect(page.locator("#detail-registration")).to_have_text(registration)


def fill_labour(page, description="Keksitty Autodata-työ", hours="1.60"):
    page.locator('#labour-form [name="hours"]').fill(hours)
    page.locator('#labour-form [name="work_description"]').fill(description)


def fill_inquiry(page, description="Keksitty tiedustelu"):
    page.locator("#new-work-button").click()
    page.locator('#new-work-form [name="registration"]').fill("TEST-NEW")
    page.locator('#new-work-form [name="customer_description"]').fill(description)


def wait_for_held(page, held, key):
    deadline = time.monotonic() + 5
    while not (key in held if isinstance(held, dict) else len(held) > key):
        if time.monotonic() >= deadline:
            raise AssertionError("Expected delayed request was not received")
        page.wait_for_timeout(10)


def test_late_save_of_a_does_not_replace_visible_b_or_its_draft(page, local_app):
    url, repository = local_app
    a, b = seed(repository, "TEST-A"), seed(repository, "TEST-B")
    held = []

    def hold_a_save(route):
        held.append((route, route.fetch().json()))

    page.route(f"**/api/works/{a['id']}/labour", hold_a_save)
    page.goto(url, wait_until="networkidle")
    open_work(page, "TEST-A")
    fill_labour(page)
    page.locator('#labour-form button[type="submit"]').click()
    open_work(page, "TEST-B")
    fill_labour(page, "Vain työlle B", "2.00")
    expect(page.locator('#labour-form button[type="submit"]')).to_be_disabled()
    # A synthetic duplicate submit must also be guarded, not just the button.
    page.locator("#labour-form").evaluate(
        "form => form.dispatchEvent(new Event('submit', {cancelable: true}))"
    )
    assert repository.get_work(b["id"])["version"] == 1
    wait_for_held(page, held, 0)
    assert len(held) == 1
    route, response = held[0]
    route.fulfill(json=response)
    expect(page.locator('#labour-form button[type="submit"]')).to_be_enabled()
    expect(page.locator("#detail-registration")).to_have_text("TEST-B")
    expect(page.locator('#labour-form [name="work_description"]')).to_have_value("Vain työlle B")
    expect(page.locator("#labour-form")).to_have_attribute("data-work-id", b["id"])
    expect(page.locator("#labour-form")).to_have_attribute("data-version", "1")
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#price-summary")).to_contain_text("180,00")
    assert repository.get_work(a["id"])["total_price"] == "144.00"
    assert repository.get_work(b["id"])["labour_work_description"] == "Vain työlle B"


def test_out_of_order_open_responses_keep_latest_selection(page, local_app):
    url, repository = local_app
    a, b = seed(repository, "TEST-A"), seed(repository, "TEST-B")
    held = {}

    def hold_open(route):
        key = route.request.url.rsplit("/", 1)[1]
        held[key] = (route, repository.get_work(key))

    page.route("**/api/works/*", hold_open)
    page.goto(url, wait_until="networkidle")
    page.locator(".work-row").filter(has_text="TEST-A").click()
    page.locator(".work-row").filter(has_text="TEST-B").click()
    wait_for_held(page, held, b["id"])
    route, response = held[b["id"]]
    route.fulfill(json=response)
    expect(page.locator("#detail-registration")).to_have_text("TEST-B")
    fill_labour(page, "B:n tallentamaton luonnos")
    wait_for_held(page, held, a["id"])
    route, response = held[a["id"]]
    with page.expect_response(f"**/api/works/{a['id']}"):
        route.fulfill(json=response)
    expect(page.locator("#detail-registration")).to_have_text("TEST-B")
    expect(page.locator('#labour-form [name="work_description"]')).to_have_value("B:n tallentamaton luonnos")
    expect(page.locator("#labour-form")).to_have_attribute("data-work-id", b["id"])


def test_duplicate_create_is_locked_and_lost_response_reuses_key(page, local_app):
    url, repository = local_app
    held, keys = [], []

    def hold_create(route):
        if route.request.method != "POST":
            route.continue_()
            return
        keys.append(route.request.post_data_json["idempotency_key"])
        if len(keys) == 1:
            held.append((route, route.fetch().json()))
        else:
            route.continue_()

    page.route("**/api/works", hold_create)
    page.goto(url, wait_until="networkidle")
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator('#new-work-form button[type="submit"]')).to_be_disabled()
    page.locator("#new-work-form").evaluate(
        "form => form.dispatchEvent(new Event('submit', {cancelable: true}))"
    )
    wait_for_held(page, held, 0)
    assert len(keys) == 1
    route, _ = held[0]
    route.abort()
    expect(page.locator("#new-work-error")).to_be_visible()
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-dialog")).not_to_be_visible()
    expect(page.locator(".work-row")).to_have_count(1)
    assert keys[0] == keys[1]
    assert len(repository.list_works()) == 1
    # An intentional new dialog receives a distinct key for identical content.
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator(".work-row")).to_have_count(2)
    assert keys[2] != keys[0]


def test_validation_errors_are_finnish_and_text_detail_is_preserved(page, local_app):
    url, repository = local_app
    work = seed(repository, "TEST-ERROR")
    page.goto(url, wait_until="networkidle")
    fill_inquiry(page, description="   ")
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_have_text("Asiakkaan alkuperäinen työkuvaus: Kenttä ei voi olla tyhjä.")
    page.locator('#new-work-form [data-close]').first.click()
    open_work(page, "TEST-ERROR")
    fill_labour(page, hours="0")
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#labour-error")).to_have_text("Työaika: Arvon on oltava suurempi kuin 0.")
    fill_labour(page, hours="0.00001")
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#labour-error")).to_contain_text("Työaika: annettu aika ei tuota")
    assert repository.get_work(work["id"])["version"] == 1
    assert "[object Object]" not in page.locator("body").inner_text()


def test_local_flow_price_history_reload_and_mobile(page, local_app, tmp_path):
    url, repository = local_app
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-NEW")
    assert repository.list_works()[0]["labour_hours"] is None
    fill_labour(page)
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#price-summary")).to_contain_text("144,00")
    expect(page.locator("#price-summary")).to_contain_text("114,74")
    expect(page.locator("#price-summary")).to_contain_text("29,26")
    expect(page.locator("#event-list li")).to_have_count(2)
    page.reload(wait_until="networkidle")
    open_work(page, "TEST-NEW")
    expect(page.locator("#price-summary")).to_contain_text("144,00")
    desktop = tmp_path / "desktop.png"
    page.screenshot(path=str(desktop), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    mobile = tmp_path / "mobile.png"
    page.screenshot(path=str(mobile), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []
    print(f"\nDesktop QA: {desktop}\nMobile QA: {mobile}")


def test_currency_formatting_keeps_string_precision(page, local_app):
    page.goto(local_app[0], wait_until="networkidle")
    formatted = page.evaluate("formatMoney('9007199254740993.01')")
    assert formatted.replace("\u00a0", " ") == "9 007 199 254 740 993,01 €"


def test_forms_send_no_actor_fields_and_history_uses_authenticated_snapshot(page, local_app):
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method in {"POST", "PUT"} else None)
    page.goto(local_app[0], wait_until="networkidle")
    assert page.locator('[name="actor"], [name="recorded_by"]').count() == 0
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-NEW")
    fill_labour(page)
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#event-list li")).to_have_count(2)
    user = local_app.auth_service.inspect_session(local_app.token).user
    expect(page.locator("#event-list")).to_contain_text(user.display_name)
    expect(page.locator("#event-list")).to_contain_text(str(user.user_id))
    expect(page.locator("#event-list")).to_contain_text("kirjautunut käyttäjä")
    assert len(writes) == 2 and all("actor" not in body and "recorded_by" not in body for body in writes)


def test_legacy_uuid_label_is_not_rendered_as_authenticated(page, local_app):
    label = str(uuid4())
    work = local_app[1].create_work(registration="TEST-LEGACY", customer_description="Fictional history",
                                  actor=label, idempotency_key=str(uuid4()))
    page.goto(local_app[0], wait_until="networkidle")
    open_work(page, "TEST-LEGACY")
    expect(page.locator("#event-list")).to_contain_text(label + " (vanha itse ilmoitettu kirjaaja)")
    assert local_app[1].get_work(work["id"])["events"] == work["events"]


def test_csrf_is_lazy_and_cached_across_form_writes(page, local_app):
    bootstraps = []
    page.on("request", lambda request: bootstraps.append(request) if request.url.endswith("/api/auth/csrf") else None)
    page.goto(local_app[0], wait_until="networkidle")
    assert bootstraps == []
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-NEW")
    fill_labour(page)
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#price-summary")).to_contain_text("144,00")
    assert len(bootstraps) == 1
    assert bootstraps[0].headers["x-carbit-auth"] == "bootstrap"
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


def test_concurrent_writes_share_one_pending_bootstrap(page, local_app):
    held = []
    page.route("**/api/auth/csrf", lambda route: held.append(route))
    page.goto(local_app[0], wait_until="networkidle")
    payloads = [dict(registration="TEST-" + suffix, customer_description="Fictional concurrent write",
                     actor="Legacy", idempotency_key=str(uuid4())) for suffix in ("A", "B")]
    page.evaluate("""payloads => {
      window.concurrentResults = null;
      Promise.all(payloads.map(payload => api('/api/works', {
        method: 'POST', body: JSON.stringify(payload)
      }))).then(results => { window.concurrentResults = results; });
    }""", payloads)
    wait_for_held(page, held, 0)
    assert len(held) == 1 and local_app[1].list_works() == []
    held[0].fulfill(response=held[0].fetch())
    page.wait_for_function("window.concurrentResults?.length === 2")
    assert len(held) == 1 and len(local_app[1].list_works()) == 2


def test_cache_expiry_refreshes_once_without_background_polling(page, local_app):
    from datetime import timedelta
    bootstraps = []
    page.on("request", lambda request: bootstraps.append(request) if request.url.endswith("/api/auth/csrf") else None)
    page.goto(local_app[0], wait_until="networkidle")
    page.evaluate("sessionProof()")
    assert len(bootstraps) == 1
    later = local_app.auth_service.clock() + timedelta(seconds=601)
    local_app.auth_service.clock = lambda: later
    page.evaluate("""() => {
      const previousNow = Date.now;
      Date.now = () => previousNow() + 601000;
    }""")
    page.evaluate("Promise.all([sessionProof(), sessionProof()])")
    assert len(bootstraps) == 2
    page.evaluate("sessionProof()")
    assert len(bootstraps) == 2


def test_failed_bootstrap_preserves_form_key_and_never_sends_write(page, local_app):
    bootstraps, writes = [], []
    def bootstrap(route):
        bootstraps.append(route)
        if len(bootstraps) == 1:
            route.fulfill(status=429, json={"detail": "Authentication temporarily limited"})
        else:
            route.continue_()
    page.route("**/api/auth/csrf", bootstrap)
    page.on("request", lambda request: writes.append(request) if request.method == "POST" else None)
    page.goto(local_app[0], wait_until="networkidle")
    fill_inquiry(page)
    key = page.evaluate("state.creationKey")
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_have_text("Liian monta yritystä. Odota vähintään viisi minuuttia.")
    expect(page.locator('#new-work-form [name="customer_description"]')).to_have_value("Keksitty tiedustelu")
    assert page.evaluate("state.creationKey") == key
    assert len(bootstraps) == 1 and writes == [] and local_app[1].list_works() == []
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-NEW")
    assert len(bootstraps) == 2 and len(writes) == 1
    assert writes[0].post_data_json["idempotency_key"] == key


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_write_invalidates_cache_without_automatic_retry(page, local_app, status):
    writes, bootstraps = [], []
    def write(route):
        if route.request.method != "POST":
            route.continue_()
            return
        writes.append(route.request)
        if len(writes) == 1:
            route.fulfill(status=status, json={"detail": "Fictional security rejection"})
        else:
            route.continue_()
    page.route("**/api/works", write)
    page.on("request", lambda request: bootstraps.append(request) if request.url.endswith("/api/auth/csrf") else None)
    page.goto(local_app[0], wait_until="networkidle")
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_have_text("Fictional security rejection")
    expect(page.locator('#new-work-form [name="customer_description"]')).to_have_value("Keksitty tiedustelu")
    assert len(writes) == 1 and len(bootstraps) == 1
    assert page.evaluate("sessionCsrf === null")
    if status == 401:
        # A rejected session locks the UI. Only explicit server confirmation
        # can restore this same user's draft; no business write is replayed.
        expect(page.locator("#auth-panel")).to_have_attribute("data-state", "expired")
        page.locator("#auth-check").click()
        expect(page.locator("#new-work-dialog")).to_be_visible()
        assert len(writes) == 1
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-NEW")
    assert len(writes) == 2 and len(bootstraps) == 2
    assert writes[0].post_data_json["idempotency_key"] == writes[1].post_data_json["idempotency_key"]


def test_lost_labour_response_is_not_retried_and_keeps_draft_and_version(page, local_app):
    work = seed(local_app[1], "TEST-LOST")
    writes = []
    def write(route):
        writes.append(route.request)
        if len(writes) == 1:
            route.fetch()  # Commit succeeded; only the response is lost.
            route.abort()
        else:
            route.continue_()
    page.route(f"**/api/works/{work['id']}/labour", write)
    page.goto(local_app[0], wait_until="networkidle")
    open_work(page, "TEST-LOST")
    fill_labour(page, "Tallentamaton vastaus")
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#labour-error")).to_be_visible()
    expect(page.locator('#labour-form [name="work_description"]')).to_have_value("Tallentamaton vastaus")
    expect(page.locator("#labour-form")).to_have_attribute("data-version", "1")
    assert len(writes) == 1 and local_app[1].get_work(work["id"])["version"] == 2
    page.locator('#labour-form button[type="submit"]').click()
    expect(page.locator("#labour-error")).to_contain_text("toisessa selainikkunassa")
    assert len(writes) == 2 and len(local_app[1].get_work(work["id"])["events"]) == 2


def test_non_session_bootstrap_never_sends_business_write(page, local_app):
    writes = []
    page.on("request", lambda request: writes.append(request) if request.method == "POST" else None)
    page.goto(local_app[0], wait_until="networkidle")
    fill_inquiry(page)
    local_app.auth_service.revoke_session(local_app.token)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_have_text("Kirjautuminen on vanhentunut. Kirjaudu uudelleen.")
    expect(page.locator('#new-work-form [name="registration"]')).to_have_value("TEST-NEW")
    assert writes == [] and local_app[1].list_works() == []


def test_invalidated_pending_refresh_cannot_restore_cached_proof(page, local_app):
    held = []
    page.route("**/api/auth/csrf", lambda route: held.append(route))
    page.goto(local_app[0], wait_until="networkidle")
    page.evaluate("""() => {
      window.proofOutcome = 'pending';
      sessionProof().then(() => { window.proofOutcome = 'accepted'; })
        .catch(() => { window.proofOutcome = 'rejected'; });
    }""")
    wait_for_held(page, held, 0)
    page.evaluate("forgetSessionCsrf()")
    held[0].fulfill(response=held[0].fetch())
    page.wait_for_function("window.proofOutcome === 'rejected'")
    assert page.evaluate("sessionCsrf === null && csrfRefresh === null")
    assert len(held) == 1
