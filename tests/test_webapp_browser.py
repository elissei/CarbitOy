"""Local Chromium regressions; install with python -m playwright install chromium."""

import socket
import threading
import time
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from webapp.main import create_app


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def local_app(tmp_path):
    app = create_app(tmp_path / "browser.db")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local test server did not start")
            threading.Event().wait(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", app.state.repository
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
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
    page.locator('#labour-form [name="recorded_by"]').fill("Testikirjaaja")


def fill_inquiry(page, actor="Testikirjaaja"):
    page.locator("#new-work-button").click()
    page.locator('#new-work-form [name="registration"]').fill("TEST-NEW")
    page.locator('#new-work-form [name="customer_description"]').fill("Keksitty tiedustelu")
    page.locator('#new-work-form [name="actor"]').fill(actor)


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
    fill_inquiry(page, actor="   ")
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_have_text("Kirjaaja: Kenttä ei voi olla tyhjä.")
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
