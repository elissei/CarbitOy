"""Manual inquiry drafts through Chromium, real HTTPS/auth/CSRF and disposable SQLite."""

import json
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect

from http_auth_support import grant_session, local_tls_files, settings_for
from test_webapp_browser import LocalApp, browser, page
from test_webapp_browser_auth import password, mfa, user_for
from webapp.auth_models import SessionCookiePolicy, UserRole
from webapp.draft_keys import DraftKeyring
from webapp.draft_settings import DraftApiSettings
from webapp.main import create_app
from workshop_support import seed_test_scope


@pytest.fixture
def local_app(tmp_path, request):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    enabled = getattr(request, "param", True)
    options = {"draft_settings": DraftApiSettings(enabled=True, keyring=DraftKeyring(
        active_key_id="fictional-browser", keys={"fictional-browser": bytes(range(32))},
    ))} if enabled else {}  # Disabled test exercises the real absent-setting default.
    app = create_app(tmp_path / "business.db", auth_settings=settings_for(tmp_path / "auth.db", url), **options)
    cert, key = local_tls_files(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", proxy_headers=False,
        ssl_certfile=str(cert), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Isolated HTTPS app did not start")
            threading.Event().wait(0.01)
        seed_test_scope(app.state.repository)
        grant = grant_session(app.state.auth_service)
        fixture = LocalApp(url, app.state.repository, grant.token.get_secret_value(), app.state.auth_service)
        fixture.app = app
        yield fixture
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def form(tab, name):
    return tab.locator(f'#new-work-form [name="{name}"]')


def start(tab, app, description="Keskeneräinen fiktiivinen tiedustelu", registration=""):
    tab.goto(app[0], wait_until="networkidle")
    tab.locator("#new-work-button").click()
    form(tab, "registration").fill(registration)
    form(tab, "customer_description").fill(description)


def save(tab):
    with tab.expect_response(lambda r: "/api/drafts" in r.url and r.request.method in ("POST", "PUT")) as response:
        tab.locator("#draft-save").click()
    assert response.value.ok, response.value.text()
    expect(tab.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    return response.value.json()["draft_id"]


def close(tab):
    tab.locator('#new-work-form [data-close]').last.click()


def open_saved(tab, draft_id):
    if tab.locator("#new-work-dialog").evaluate("d => d.open"):
        close(tab)
    with tab.expect_response(lambda r: "/api/drafts?limit=100" in r.url):
        tab.locator("#draft-list-refresh").click()
    tab.locator(f'[data-draft-id="{draft_id}"]').click()
    expect(tab.locator("#draft-message")).to_contain_text("Palvelimen luonnos avattu")


def stored(app, draft_id):
    with app[1]._connect() as connection:
        row = connection.execute("SELECT payload_json,revision,expires_at FROM draft_contents WHERE draft_id=?", (draft_id,)).fetchone()
    return None if row is None else (json.loads(row[0]), row[1], row[2])


def counts(app):
    with app[1]._connect() as connection:
        return tuple(connection.execute("SELECT count(*) FROM " + name).fetchone()[0]
                     for name in ("draft_registry", "work_inquiries"))


def mutation(tab, path, method, body):
    # Auxiliary competing operation still passes through the real API/CSRF/DB chain.
    return tab.evaluate("async ({path,method,body}) => api(path,{method,body:JSON.stringify(body)})",
                        {"path": path, "method": method, "body": body})


@pytest.mark.parametrize("width", [1280, 390])
def test_manual_create_list_open_update_reload_and_real_relogin(page, local_app, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    start(page, local_app)
    # Empty required final-inquiry registration must not block a draft.
    draft_id = save(page)
    assert counts(local_app) == (1, 0)
    expect(page.locator('#new-work-form button[type="submit"]')).to_be_disabled()
    page.reload(wait_until="networkidle")
    open_saved(page, draft_id)
    expect(form(page, "registration")).to_have_value("")
    expect(form(page, "customer_description")).to_have_value("Keskeneräinen fiktiivinen tiedustelu")
    form(page, "customer_description").fill("Muokattu fiktiivinen kuvaus")
    assert save(page) == draft_id
    assert stored(local_app, draft_id)[0]["customer_description"] == "Muokattu fiktiivinen kuvaus"
    assert stored(local_app, draft_id)[1] == 2
    close(page)
    user = user_for(local_app)
    page.locator("#logout-button").click()
    expect(page.locator("#business-shell")).to_be_hidden()
    assert form(page, "customer_description").input_value() == ""
    assert page.locator("#draft-list").inner_text() == ""
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    page.locator("#auth-check").click()
    password(page, local_app, user)
    mfa(page, local_app, user)
    open_saved(page, draft_id)
    expect(form(page, "customer_description")).to_have_value("Muokattu fiktiivinen kuvaus")
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"manual-draft-{width}.png"), full_page=True)
    assert counts(local_app) == (1, 0)
    assert errors == []


def test_two_tabs_conflict_preserves_loser_and_requires_explicit_server_open(page, local_app):
    start(page, local_app, registration="TEST-RACE")
    draft_id = save(page)
    other = page.context.new_page()
    try:
        other.goto(local_app[0], wait_until="networkidle")
        open_saved(other, draft_id)
        form(page, "customer_description").fill("Ensimmäinen voittaja")
        save(page)
        form(other, "customer_description").fill("Toisen välilehden oma teksti")
        other.locator("#draft-save").click()
        expect(other.locator("#draft-message")).to_contain_text("Versioristiriita")
        expect(form(other, "customer_description")).to_have_value("Toisen välilehden oma teksti")
        expect(other.locator("#draft-save")).to_be_disabled()
        assert stored(local_app, draft_id)[0]["customer_description"] == "Ensimmäinen voittaja"
        other.once("dialog", lambda dialog: dialog.dismiss())
        other.locator("#draft-reload").click()
        expect(form(other, "customer_description")).to_have_value("Toisen välilehden oma teksti")
        other.once("dialog", lambda dialog: dialog.accept())
        other.locator("#draft-reload").click()
        expect(form(other, "customer_description")).to_have_value("Ensimmäinen voittaja")
        form(other, "customer_description").fill("Tietoisesti uusi muutos")
        save(other)
        assert stored(local_app, draft_id)[1] == 3
        assert counts(local_app) == (1, 0)
    finally:
        other.close()


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_lost_committed_response_retries_exact_envelope_and_keeps_later_edits(page, local_app, method):
    start(page, local_app)
    original_id = save(page) if method == "PUT" else None
    form(page, "customer_description").fill("Alkuperäisen pyynnön teksti")
    requests = []

    def lose_response(route):
        if route.request.method != method:
            route.continue_()
            return
        requests.append(route.request.post_data_json)
        response = route.fetch()
        assert response.ok
        route.abort("failed")  # Real server committed; only delivery is lost.

    page.route("**/api/drafts**", lose_response)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("tulos jäi epäselväksi")
    assert counts(local_app) == (1, 0)
    form(page, "customer_description").fill("Myöhempi vielä tallentamaton teksti")
    page.unroute("**/api/drafts**", lose_response)
    with page.expect_response(lambda r: r.request.method == method and "/api/drafts" in r.url) as response:
        page.locator("#draft-retry").click()
    expect(page.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    assert response.value.request.post_data_json == requests[0]
    draft_id = response.value.json()["draft_id"]
    assert original_id is None or original_id == draft_id
    expect(form(page, "customer_description")).to_have_value("Myöhempi vielä tallentamaton teksti")
    assert stored(local_app, draft_id)[0]["customer_description"] == "Alkuperäisen pyynnön teksti"
    save(page)
    assert stored(local_app, draft_id)[0]["customer_description"] == "Myöhempi vielä tallentamaton teksti"
    assert counts(local_app) == (1, 0)


def test_real_storage_failure_rolls_back_and_explicit_retry_recovers(page, local_app, monkeypatch):
    start(page, local_app)
    draft_id = save(page)
    before = stored(local_app, draft_id)
    form(page, "customer_description").fill("Tietokantavirheessä säilyvä teksti")
    import sqlite3
    repository = local_app.app.state.draft_service.repository
    original = repository._record

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Synthetic isolated storage outage")

    # Fail after the content UPDATE, inside the real business transaction.
    monkeypatch.setattr(repository, "_record", fail)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("poissa käytöstä")
    expect(form(page, "customer_description")).to_have_value("Tietokantavirheessä säilyvä teksti")
    assert stored(local_app, draft_id) == before
    monkeypatch.setattr(repository, "_record", original)
    page.locator("#draft-retry").click()
    expect(page.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    assert stored(local_app, draft_id)[1] == before[1] + 1


def test_switching_drafts_requires_choice_and_late_save_does_not_replace_input(page, local_app):
    start(page, local_app, "Ensimmäinen", "TEST-A")
    first = save(page)
    close(page)
    page.locator("#new-work-button").click()
    form(page, "customer_description").fill("Toinen")
    second = save(page)
    form(page, "customer_description").fill("Säilytä oma kirjoitus")
    page.once("dialog", lambda dialog: dialog.dismiss())
    open_saved_attempt(page, first)
    expect(form(page, "customer_description")).to_have_value("Säilytä oma kirjoitus")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(f'[data-draft-id="{first}"]').click()
    expect(form(page, "customer_description")).to_have_value("Ensimmäinen")
    held = []

    def hold(route):
        if route.request.method == "PUT":
            held.append((route, route.fetch()))
            page.evaluate("window.draftResponseHeld = true")
        else:
            route.continue_()

    page.route("**/api/drafts/**", hold)
    form(page, "customer_description").fill("Lähetetty teksti")
    page.locator("#draft-save").click()
    page.wait_for_function("window.draftResponseHeld === true")
    # Event-driven browser request delivery, not a sleep-based server race.
    expect(page.locator("#draft-save")).to_be_disabled()
    form(page, "customer_description").fill("Kirjoitettu odotuksen aikana")
    assert len(held) == 1
    held[0][0].fulfill(response=held[0][1])
    expect(page.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    expect(form(page, "customer_description")).to_have_value("Kirjoitettu odotuksen aikana")
    assert stored(local_app, first)[0]["customer_description"] == "Lähetetty teksti"
    assert stored(local_app, second)[0]["customer_description"] == "Toinen"


@pytest.mark.parametrize("ending", ["discard", "expiry"])
def test_ended_draft_preserves_local_text_and_does_not_create_replacement(page, local_app, monkeypatch, ending):
    from contextlib import nullcontext
    from test_webapp_draft_service import business_clock

    start(page, local_app)
    draft_id = save(page)
    expires = stored(local_app, draft_id)[2]
    if ending == "discard":
        mutation(page, f"/api/drafts/{draft_id}/discard", "POST", {"request_id": str(uuid4()), "expected_revision": 1})
    clock = business_clock(local_app[1], monkeypatch, [expires]) if ending == "expiry" else nullcontext()
    with clock:
        form(page, "customer_description").fill("Päättymisen jälkeen oma teksti")
        page.locator("#draft-save").click()
        expect(page.locator("#draft-message")).to_contain_text("Luonnos ei ole saatavilla")
        expect(form(page, "customer_description")).to_have_value("Päättymisen jälkeen oma teksti")
        expect(page.locator("#draft-save")).to_be_disabled()
        assert counts(local_app) == (1, 0)
        response = page.request.get(local_app[0] + "/api/drafts/" + draft_id)
        assert response.status == 404


def test_missing_null_fields_and_unchanged_save_keep_revision_and_expiry(page, local_app):
    page.goto(local_app[0], wait_until="networkidle")
    ack = mutation(page, "/api/drafts", "POST", {"draft_type": "WORK_INQUIRY", "request_id": str(uuid4()),
        "payload": {"customer_description": None}})
    draft_id = ack["draft_id"]
    before = stored(local_app, draft_id)
    open_saved(page, draft_id)
    save(page)
    assert stored(local_app, draft_id) == before
    assert before[0] == {"customer_description": None}


def test_list_filters_labour_and_html_is_inert(page, local_app):
    start(page, local_app, "<img src=x onerror=alert(1)>", "TEST-TEXT")
    inquiry_id = save(page)
    work = mutation(page, "/api/works", "POST", {"registration": "TEST-WORK", "customer_description": "Fiktiivinen työ",
        "idempotency_key": str(uuid4())})
    labour = mutation(page, "/api/drafts", "POST", {"draft_type": "LABOUR_ENTRY", "request_id": str(uuid4()),
        "work_id": work["id"], "original_work_version": work["version"], "payload": {"hours": "kesken"}})
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(1)
    expect(page.locator(".draft-row")).to_contain_text("<img src=x onerror=alert(1)>")
    assert page.locator("#draft-list img").count() == 0
    assert page.locator(f'[data-draft-id="{labour["draft_id"]}"]').count() == 0
    assert page.locator(f'[data-draft-id="{inquiry_id}"]').count() == 1


def test_late_committed_response_after_logout_cannot_restore_private_content(page, local_app):
    start(page, local_app, "Uloskirjautumisen yksityinen teksti")
    held = []

    def hold(route):
        if route.request.method == "POST":
            held.append((route, route.fetch()))
            page.evaluate("window.draftResponseHeld = true")
        else:
            route.continue_()

    page.route("**/api/drafts", hold)
    page.locator("#draft-save").click()
    page.wait_for_function("window.draftResponseHeld === true")
    # Logout is outside the modal; exercise the real handler after a programmatic click.
    page.locator("#logout-button").evaluate("button => button.click()")
    expect(page.locator("#business-shell")).to_be_hidden()
    assert form(page, "customer_description").input_value() == ""
    held[0][0].fulfill(response=held[0][1])
    # Roundtrip makes stale promise handlers settle without a time-based sleep.
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert form(page, "customer_description").input_value() == ""
    assert "Uloskirjautumisen yksityinen teksti" not in page.locator("body").inner_text()
    assert counts(local_app) == (1, 0)


def open_saved_attempt(tab, draft_id):
    close(tab)
    tab.locator("#draft-list-refresh").click()
    tab.locator(f'[data-draft-id="{draft_id}"]').click()


@pytest.mark.parametrize("local_app", [False], indirect=True)
def test_real_default_disabled_is_understandable_and_existing_submit_still_works(page, local_app):
    start(page, local_app, registration="TEST-LEGACY")
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("Luonnospalvelu ei ole käytössä")
    expect(form(page, "customer_description")).to_have_value("Keskeneräinen fiktiivinen tiedustelu")
    assert counts(local_app) == (0, 0)
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator("#draft-list-message")).to_contain_text("Luonnospalvelu ei ole käytössä")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#new-work-button").click()
    form(page, "registration").fill("TEST-SUBMIT")
    form(page, "customer_description").fill("Tavallinen tiedustelu edelleen toimii")
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#detail-registration")).to_have_text("TEST-SUBMIT")
    assert counts(local_app) == (0, 1)


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN])
def test_user_change_clears_private_editor_list_and_enforces_roles(page, local_app, role):
    start(page, local_app, "Edellisen käyttäjän yksityinen teksti", "TEST-PRIVATE")
    draft_id = save(page)
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(1)
    other = grant_session(local_app.auth_service, role=role)
    page.context.add_cookies([{"name": SessionCookiePolicy().name, "value": other.token.get_secret_value(),
        "url": local_app[0], "secure": True, "httpOnly": True, "sameSite": "Strict"}])
    page.evaluate("CarbitAuth.lock()")
    page.locator("#auth-check").click()
    expect(page.locator("#business-shell")).to_be_visible()
    assert form(page, "customer_description").input_value() == ""
    assert page.locator("#draft-list").inner_text() == ""
    assert "Edellisen käyttäjän" not in page.locator("body").inner_text()
    response = page.request.get(local_app[0] + "/api/drafts/" + draft_id)
    assert response.status == (403 if role == UserRole.IT_ADMIN else 200)
    if role == UserRole.IT_ADMIN:
        expect(page.locator("#inquiry-drafts")).to_be_hidden()
        expect(page.locator("#new-work-button")).to_be_hidden()
    else:
        page.locator("#draft-list-refresh").click()
        expect(page.locator(f'[data-draft-id="{draft_id}"]')).to_be_visible()
        page.locator("#new-work-button").click()
        form(page, "customer_description").fill("Uuden käyttäjän oma luonnos")
        save(page)
        assert counts(local_app) == (2, 0)


def test_historical_receipt_after_discard_never_recreates_content(page, local_app):
    start(page, local_app)
    held = []

    def lose(route):
        if route.request.method == "POST":
            response = route.fetch()
            held.append(response.json())
            route.abort("failed")
        else:
            route.continue_()

    page.route("**/api/drafts", lose)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("tulos jäi epäselväksi")
    page.unroute("**/api/drafts", lose)
    draft_id = held[0]["draft_id"]
    mutation(page, f"/api/drafts/{draft_id}/discard", "POST", {"request_id": str(uuid4()), "expected_revision": 1})
    page.locator("#draft-retry").click()
    expect(page.locator("#draft-message")).to_contain_text("luonnos on jo päättynyt")
    expect(form(page, "customer_description")).to_have_value("Keskeneräinen fiktiivinen tiedustelu")
    assert stored(local_app, draft_id) is None
    assert counts(local_app) == (1, 0)
    expect(page.locator("#draft-save")).to_be_disabled()


def test_historical_ack_does_not_adopt_new_revision_for_stale_local_text(page, local_app):
    start(page, local_app)
    receipts = []

    def lose(route):
        if route.request.method == "POST":
            response = route.fetch()
            receipts.append(response.json())
            route.abort("failed")
        else:
            route.continue_()

    page.route("**/api/drafts", lose)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("tulos jäi epäselväksi")
    page.unroute("**/api/drafts", lose)
    draft_id = receipts[0]["draft_id"]
    mutation(page, f"/api/drafts/{draft_id}", "PUT", {"request_id": str(uuid4()), "expected_revision": 1,
        "payload": {"customer_description": "Toisessa välilehdessä jo tallennettu"}})
    page.locator("#draft-retry").click()
    expect(page.locator("#draft-message")).to_contain_text("Palvelimella on toinen sisältöversio")
    expect(form(page, "customer_description")).to_have_value("Keskeneräinen fiktiivinen tiedustelu")
    expect(page.locator("#draft-save")).to_be_disabled()
    assert stored(local_app, draft_id)[1] == 2
    assert stored(local_app, draft_id)[0]["customer_description"] == "Toisessa välilehdessä jo tallennettu"


@pytest.mark.parametrize("failure", ["discard", "service", "network"])
def test_failed_open_is_visible_outside_closed_dialog_and_preserves_form(page, local_app, monkeypatch, failure):
    start(page, local_app, "Avausvirheessä säilyvä teksti")
    draft_id = save(page)
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(1)
    if failure == "discard":
        mutation(page, f"/api/drafts/{draft_id}/discard", "POST", {"request_id": str(uuid4()), "expected_revision": 1})
        expected = "Luonnos ei ole saatavilla"
    elif failure == "service":
        monkeypatch.setattr(local_app.app.state, "draft_service", None)
        expected = "Luonnospalvelu ei ole käytössä"
    else:
        page.route(f"**/api/drafts/{draft_id}", lambda route: route.abort("failed"))
        expected = "Luonnosta ei voitu lukea"
    page.locator(".draft-row").click()
    expect(page.locator("#draft-list-message")).to_contain_text(expected)
    assert form(page, "customer_description").input_value() == "Avausvirheessä säilyvä teksti"
    assert counts(local_app) == (1, 0)


# Allowed editor text stored by another client; Chromium shows it normalized (CR/LF).
CRLF_PAYLOAD = {"registration": "TEST\r\nCR\rLF", "customer_description": "Rivi 1\r\nRivi 2\rRivi 3\nRivi 4"}
SHOWN_CRLF = {"registration": "TESTCRLF", "customer_description": "Rivi 1\nRivi 2\nRivi 3\nRivi 4"}
WEEK = 7 * 24 * 60 * 60


def seed_inquiry(tab, payload):
    return mutation(tab, "/api/drafts", "POST", {"draft_type": "WORK_INQUIRY", "request_id": str(uuid4()),
        "payload": payload})["draft_id"]


def saved_payload(tab):
    with tab.expect_response(lambda r: "/api/drafts" in r.url and r.request.method in ("POST", "PUT")) as response:
        tab.locator("#draft-save").click()
    assert response.value.ok, response.value.text()
    expect(tab.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    return response.value.request.post_data_json["payload"]


def open_crlf_draft(page, local_app):
    page.goto(local_app[0], wait_until="networkidle")
    draft_id = seed_inquiry(page, CRLF_PAYLOAD)
    before = stored(local_app, draft_id)
    assert before[0] == CRLF_PAYLOAD
    open_saved(page, draft_id)
    for name, value in SHOWN_CRLF.items():
        assert form(page, name).input_value() == value
    # Server time one minute later: a content change would also renew retention.
    return draft_id, before, [before[2] - WEEK + 60]


def test_crlf_draft_unchanged_save_keeps_content_revision_and_expiry(page, local_app, monkeypatch):
    from test_webapp_draft_service import business_clock

    draft_id, before, later = open_crlf_draft(page, local_app)
    with business_clock(local_app[1], monkeypatch, later):
        assert saved_payload(page) == CRLF_PAYLOAD
        assert stored(local_app, draft_id) == before
    assert counts(local_app) == (1, 0)


def test_real_edit_is_saved_while_untouched_crlf_field_keeps_original(page, local_app, monkeypatch):
    from test_webapp_draft_service import business_clock

    draft_id, before, later = open_crlf_draft(page, local_app)
    edited = {"registration": "TEST-UUSI", "customer_description": CRLF_PAYLOAD["customer_description"]}
    with business_clock(local_app[1], monkeypatch, later):
        form(page, "registration").fill("TEST-UUSI")
        assert saved_payload(page) == edited
        assert stored(local_app, draft_id) == (edited, before[1] + 1, later[0] + WEEK)
        # The text shown at the confirmed save is the new start value.
        assert saved_payload(page) == edited
        assert stored(local_app, draft_id) == (edited, before[1] + 1, later[0] + WEEK)
        form(page, "customer_description").fill("Uusi rivi 1\nUusi rivi 2")
        typed = {"registration": "TEST-UUSI", "customer_description": "Uusi rivi 1\nUusi rivi 2"}
        assert saved_payload(page) == typed
        assert stored(local_app, draft_id) == (typed, before[1] + 2, later[0] + WEEK)


def test_edit_restored_to_shown_start_value_keeps_original_content(page, local_app, monkeypatch):
    from test_webapp_draft_service import business_clock

    draft_id, before, later = open_crlf_draft(page, local_app)
    form(page, "registration").fill("TEST-VALI")
    form(page, "customer_description").fill("Väliaikainen muutos")
    expect(page.locator("#draft-message")).to_contain_text("Tallentamattomia muutoksia.")
    for name, value in SHOWN_CRLF.items():
        form(page, name).fill(value)
    expect(page.locator("#draft-message")).to_contain_text("Luonnos tallentuu vain Tallenna luonnos -painikkeella.")
    with business_clock(local_app[1], monkeypatch, later):
        assert saved_payload(page) == CRLF_PAYLOAD
        assert stored(local_app, draft_id) == before


def hold_reads(tab, draft_id):
    """Hold the real GET response in the browser route; record client-side request failures."""
    held, failures = [], []

    def hold(route):
        if route.request.method != "GET":
            route.continue_()
            return
        held.append((route, route.fetch()))
        tab.evaluate("window.draftReadsHeld = (window.draftReadsHeld || 0) + 1")

    tab.on("requestfailed", lambda request: failures.append(request.failure)
           if request.method == "GET" and request.url.endswith("/api/drafts/" + draft_id) else None)
    tab.route(f"**/api/drafts/{draft_id}", hold)
    return held, failures


def test_stalled_server_version_open_times_out_keeps_text_and_allows_retry(page, local_app):
    page.clock.install()
    start(page, local_app, "Ensimmäinen tallennettu versio")
    draft_id = save(page)
    mutation(page, f"/api/drafts/{draft_id}", "PUT", {"request_id": str(uuid4()), "expected_revision": 1,
        "payload": {"customer_description": "Toisen välilehden versio"}})
    form(page, "customer_description").fill("Oma tallentamaton teksti")
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("Versioristiriita")
    held, failures = hold_reads(page, draft_id)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#draft-reload").click()
    page.wait_for_function("window.draftReadsHeld === 1")
    expect(form(page, "customer_description")).to_be_disabled()
    page.clock.fast_forward(15000)  # Controlled browser time, no sleep-based race.
    expect(page.locator("#draft-message")).to_contain_text("Luonnoksen avaus keskeytettiin")
    expect(form(page, "customer_description")).to_be_enabled()
    expect(form(page, "customer_description")).to_have_value("Oma tallentamaton teksti")
    assert failures == ["net::ERR_ABORTED"]
    form(page, "customer_description").fill("Uudempi oma teksti")
    held[0][0].fulfill(response=held[0][1])  # The aborted request can no longer reach the page.
    expect(form(page, "customer_description")).to_have_value("Uudempi oma teksti")
    expect(page.locator("#draft-message")).not_to_contain_text("Palvelimen luonnos avattu")
    page.unroute(f"**/api/drafts/{draft_id}")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#draft-reload").click()
    expect(page.locator("#draft-message")).to_contain_text("Palvelimen luonnos avattu")
    expect(form(page, "customer_description")).to_have_value("Toisen välilehden versio")
    page.keyboard.press("Escape")
    expect(page.locator("#new-work-dialog")).not_to_be_visible()
    assert stored(local_app, draft_id)[:2] == ({"customer_description": "Toisen välilehden versio"}, 2)


def test_stalled_list_open_times_out_and_late_response_cannot_replace_other_draft(page, local_app):
    page.clock.install()
    start(page, local_app, "Luonnos A", "TEST-A")
    first = save(page)
    close(page)
    page.locator("#new-work-button").click()
    form(page, "registration").fill("TEST-B")
    form(page, "customer_description").fill("Luonnos B")
    second = save(page)
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(2)
    held, failures = hold_reads(page, first)
    page.locator(f'[data-draft-id="{first}"]').click()
    page.wait_for_function("window.draftReadsHeld === 1")
    page.clock.fast_forward(15000)
    expect(page.locator("#draft-list-message")).to_contain_text("Luonnoksen avaus keskeytettiin")
    assert failures == ["net::ERR_ABORTED"]
    expect(page.locator("#new-work-dialog")).not_to_be_visible()
    assert form(page, "customer_description").input_value() == "Luonnos B"
    page.locator(f'[data-draft-id="{second}"]').click()
    expect(page.locator("#draft-message")).to_contain_text("Palvelimen luonnos avattu")
    held[0][0].fulfill(response=held[0][1])
    expect(form(page, "registration")).to_have_value("TEST-B")
    expect(form(page, "customer_description")).to_have_value("Luonnos B")
    expect(page.locator("#draft-message")).to_contain_text("Palvelimen luonnos avattu")
    assert stored(local_app, first)[:2] == ({"registration": "TEST-A", "customer_description": "Luonnos A"}, 1)
    assert counts(local_app) == (2, 0)


def test_late_open_response_after_logout_cannot_restore_cleared_editor(page, local_app):
    start(page, local_app, "Uloskirjautumisen jälkeen piilossa")
    draft_id = save(page)
    close(page)
    page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(1)
    held, failures = hold_reads(page, draft_id)
    page.locator(".draft-row").click()
    page.wait_for_function("window.draftReadsHeld === 1")
    page.locator("#logout-button").evaluate("button => button.click()")
    expect(page.locator("#business-shell")).to_be_hidden()
    assert form(page, "customer_description").input_value() == ""
    # Real late delivery to the page, before any open timeout.
    with page.expect_response(lambda r: r.url.endswith("/api/drafts/" + draft_id)) as late:
        held[0][0].fulfill(response=held[0][1])
    assert late.value.ok
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert failures == []
    assert form(page, "customer_description").input_value() == ""
    assert not page.locator("#new-work-dialog").evaluate("d => d.open")
    assert "Uloskirjautumisen jälkeen piilossa" not in page.locator("body").inner_text()


@pytest.fixture
def other_staff(browser,local_app):
    grant=grant_session(local_app.auth_service,role=UserRole.SERVICE_MANAGER)
    context=browser.new_context(viewport={'width':1280,'height':900},ignore_https_errors=True)
    context.add_cookies([{'name':SessionCookiePolicy().name,'value':grant.token.get_secret_value(),
        'url':local_app[0],'secure':True,'httpOnly':True,'sameSite':'Strict'}])
    tab=context.new_page()
    try:yield tab,grant
    finally:
        tab.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('creator_role',['OWNER','SERVICE_MANAGER'])
def test_two_staff_real_browser_share_edit_conflict_noop_and_existing_discard(page,local_app,other_staff,creator_role,tmp_path):
    from test_webapp_draft_service import authors
    second_tab,staff=other_staff
    first,second=(page,second_tab) if creator_role=='OWNER' else (second_tab,page)
    owner=user_for(local_app)
    creator_id=str(owner.user_id if creator_role=='OWNER' else staff.user.user_id)
    editor_id=str(staff.user.user_id if creator_role=='OWNER' else owner.user_id)
    start(first,local_app,'Yhteinen alkuperäinen','TEST-SHARED')
    did=save(first)
    second.goto(local_app[0],wait_until='networkidle')
    open_saved(second,did)
    expect(second.locator('#draft-list-title')).to_have_text('Korjaamon tiedusteluluonnokset')
    expect(form(second,'customer_description')).to_have_value('Yhteinen alkuperäinen')
    form(second,'customer_description').fill('Kollegan tallennettu muutos')
    save(second)
    assert authors(local_app[1],did)==(creator_id,editor_id)
    form(first,'customer_description').fill('Konfliktissa säilyvä oma teksti')
    first.locator('#draft-save').click()
    expect(first.locator('#draft-message')).to_contain_text('Versioristiriita')
    expect(form(first,'customer_description')).to_have_value('Konfliktissa säilyvä oma teksti')
    assert stored(local_app,did)[0]['customer_description']=='Kollegan tallennettu muutos'
    first.once('dialog',lambda dialog:dialog.accept())
    first.locator('#draft-reload').click()
    expect(form(first,'customer_description')).to_have_value('Kollegan tallennettu muutos')
    form(first,'customer_description').fill('Luojan seuraava tietoinen muutos')
    save(first)
    open_saved(second,did)
    before=stored(local_app,did)
    save(second)  # Another staff member's no-op must not become the editor.
    assert stored(local_app,did)==before
    assert authors(local_app[1],did)==(creator_id,creator_id)
    second.screenshot(path=str(tmp_path/'shared-workshop-draft.png'),full_page=True)
    result=mutation(second,f'/api/drafts/{did}/discard','POST',{'request_id':str(uuid4()),'expected_revision':3})
    assert result['state']=='DISCARDED' and stored(local_app,did) is None
    assert authors(local_app[1],did)==(creator_id,creator_id)
    close(first)
    with first.expect_response(lambda r:'/api/drafts?limit=100' in r.url):first.locator('#draft-list-refresh').click()
    expect(first.locator('.draft-row')).to_have_count(0)
    for tab in (first,second):assert tab.evaluate('localStorage.length===0 && sessionStorage.length===0')


def test_lost_original_save_after_colleague_edit_keeps_new_content_author_and_local_text(page,local_app,other_staff):
    from test_webapp_draft_service import authors
    other,staff=other_staff
    start(page,local_app,'Alku','TEST-REPLAY')
    did=save(page);path=f'/api/drafts/{did}';lost=[]
    def lose(route):
        if route.request.method=='PUT':
            response=route.fetch();assert response.ok
            lost.append(route.request.post_data_json);route.abort('failed')
        else:route.continue_()
    page.route('**'+path,lose)
    form(page,'customer_description').fill('Alkuperäinen epäselvä tallennus')
    page.locator('#draft-save').click()
    expect(page.locator('#draft-message')).to_contain_text('tulos jäi epäselväksi')
    page.unroute('**'+path,lose)
    form(page,'customer_description').fill('Myöhempi oma tallentamaton teksti')
    other.goto(local_app[0],wait_until='networkidle');open_saved(other,did)
    expect(form(other,'customer_description')).to_have_value('Alkuperäinen epäselvä tallennus')
    form(other,'customer_description').fill('Kollegan uudempi sisältö')
    save(other)
    before=stored(local_app,did)
    with local_app[1]._connect() as c:receipt_count=c.execute('SELECT count(*) FROM draft_request_receipts').fetchone()[0]
    with page.expect_response(lambda r:r.request.method=='PUT' and path in r.url) as response:
        page.locator('#draft-retry').click()
    assert response.value.json()['revision']==2
    assert response.value.request.post_data_json==lost[0]
    expect(page.locator('#draft-message')).to_contain_text('Palvelimella on toinen sisältöversio')
    expect(form(page,'customer_description')).to_have_value('Myöhempi oma tallentamaton teksti')
    expect(page.locator('#draft-save')).to_be_disabled()
    assert stored(local_app,did)==before and before[1]==3
    assert authors(local_app[1],did)[1]==str(staff.user.user_id)
    with local_app[1]._connect() as c:assert c.execute('SELECT count(*) FROM draft_request_receipts').fetchone()[0]==receipt_count


def test_departed_creator_is_denied_while_colleague_can_continue_and_discard(page,local_app,other_staff):
    from test_webapp_draft_service import authors
    other,staff=other_staff
    start(page,local_app,'Säilyy korjaamolla','TEST-DEPART')
    did=save(page);creator=user_for(local_app)
    directory=local_app.auth_service.workshop_directory
    workshop=directory.resolve(creator.user_id)['workshop_id']
    directory.revoke_membership(creator.user_id,workshop,operator_reference='Fictional browser staff departure')
    assert page.request.get(local_app[0]+f'/api/drafts/{did}').status==403
    other.goto(local_app[0],wait_until='networkidle');open_saved(other,did)
    form(other,'customer_description').fill('Aktiivisen kollegan jatkama')
    save(other)
    assert authors(local_app[1],did)==(str(creator.user_id),str(staff.user.user_id))
    assert mutation(other,f'/api/drafts/{did}/discard','POST',{'request_id':str(uuid4()),'expected_revision':2})['state']=='DISCARDED'
    assert stored(local_app,did) is None
    assert authors(local_app[1],did)==(str(creator.user_id),str(staff.user.user_id))


def test_identity_switch_clears_unknown_request_but_new_staff_can_fetch_shared_saved_text(page,local_app,other_staff):
    other,staff=other_staff
    requests=[]
    def lose(route):
        if route.request.method=='POST':
            response=route.fetch();assert response.ok
            requests.append((route.request.post_data_json,response.json()));route.abort('failed')
        else:route.continue_()
    start(page,local_app,'Vain tämä teksti tallentui','TEST-CHANGE')
    page.route('**/api/drafts',lose)
    page.locator('#draft-save').click()
    expect(page.locator('#draft-message')).to_contain_text('tulos jäi epäselväksi')
    page.unroute('**/api/drafts',lose)
    form(page,'customer_description').fill('Edellisen käyttäjän tallentamaton salainen teksti')
    writes=[]
    page.on('request',lambda r:writes.append(r.post_data) if r.method in ('POST','PUT') and '/api/drafts' in r.url else None)
    page.context.add_cookies([{'name':SessionCookiePolicy().name,'value':staff.token.get_secret_value(),
        'url':local_app[0],'secure':True,'httpOnly':True,'sameSite':'Strict'}])
    page.evaluate('CarbitAuth.lock()');page.locator('#auth-check').click()
    expect(page.locator('#business-shell')).to_be_visible()
    assert form(page,'customer_description').input_value()==''
    assert page.locator('#draft-list').inner_text()==''
    expect(page.locator('#draft-retry')).to_be_hidden()
    assert counts(local_app)==(1,0) and writes==[]
    open_saved(page,requests[0][1]['draft_id'])
    expect(form(page,'customer_description')).to_have_value('Vain tämä teksti tallentui')
    assert 'Edellisen käyttäjän tallentamaton' not in page.locator('body').inner_text()
    close(page)  # The real modal must close before the header logout is clickable.
    page.locator('#logout-button').click()
    expect(page.locator('#business-shell')).to_be_hidden()
    assert form(page,'customer_description').input_value()==''
    assert page.locator('#draft-list').inner_text()==''
    assert writes==[]


DAY_MS = 86_400_000


def browser_ms(value):
    """Playwright clock times as datetimes; a bare number is not interpreted as epoch milliseconds."""
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def hold_lists(tab):
    """Hold real list GET responses in the browser route; record aborted list requests."""
    held, failures = [], []
    listing = lambda url: "/api/drafts?limit=100" in url

    def hold(route):
        if held:  # Only the first list request is held; later ones reach the real server.
            route.continue_()
            return
        held.append((route, route.fetch()))
        tab.evaluate("window.draftListsHeld = (window.draftListsHeld || 0) + 1")

    tab.on("requestfailed", lambda request: failures.append(request.failure)
           if listing(request.url) else None)
    tab.route(listing, hold)
    return held, failures, listing


def settle(tab):
    tab.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def test_stalled_list_refresh_times_out_keeps_form_and_stale_list_cannot_replace_newer(page, local_app):
    page.clock.install()
    start(page, local_app, "Luonnos A", "TEST-A")
    save(page)
    # Edited after the save and left unsaved: the list timeout must not drop or "clean" it.
    form(page, "customer_description").fill("Tallennuksen jälkeen muokattu, tallentamaton A")
    unsaved = lambda: page.locator("#draft-message").text_content()
    assert "Tallentamattomia muutoksia" in unsaved()
    close(page)
    held, failures, listing = hold_lists(page)
    page.locator("#draft-list-refresh").click()
    page.wait_for_function("window.draftListsHeld === 1")
    expect(page.locator("#draft-list-refresh")).to_be_disabled()
    page.clock.fast_forward(15000)  # Controlled browser time, no sleep-based race.
    expect(page.locator("#draft-list-message")).to_contain_text("Luonnosluettelon haku keskeytettiin")
    expect(page.locator("#draft-list-refresh")).to_be_enabled()
    assert failures == ["net::ERR_ABORTED"]
    assert form(page, "customer_description").input_value() == "Tallennuksen jälkeen muokattu, tallentamaton A"
    assert "Tallentamattomia muutoksia" in unsaved()
    mutation(page, "/api/drafts", "POST", {"draft_type": "WORK_INQUIRY", "request_id": str(uuid4()),
        "payload": {"registration": "TEST-B", "customer_description": "Luonnos B"}})
    with page.expect_response(lambda r: "/api/drafts?limit=100" in r.url):
        page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(2)
    held[0][0].fulfill(response=held[0][1])  # The aborted one-row response can no longer reach the page.
    settle(page)
    page.unroute(listing)
    expect(page.locator(".draft-row")).to_have_count(2)
    expect(page.locator("#draft-list-message")).to_contain_text("Valitse luonnos jatkaaksesi")
    assert form(page, "customer_description").input_value() == "Tallennuksen jälkeen muokattu, tallentamaton A"
    assert "Tallentamattomia muutoksia" in unsaved()
    assert counts(local_app) == (2, 0)


def test_late_list_response_after_identity_switch_cannot_fill_new_users_view(page, local_app, other_staff):
    _, staff = other_staff
    start(page, local_app, "Edellisen käyttäjän listarivi", "TEST-LIST")
    save(page)
    close(page)
    held, failures, listing = hold_lists(page)
    page.locator("#draft-list-refresh").click()
    page.wait_for_function("window.draftListsHeld === 1")
    page.context.add_cookies([{"name": SessionCookiePolicy().name, "value": staff.token.get_secret_value(),
        "url": local_app[0], "secure": True, "httpOnly": True, "sameSite": "Strict"}])
    page.evaluate("CarbitAuth.lock()")
    page.locator("#auth-check").click()
    expect(page.locator("#business-shell")).to_be_visible()
    held[0][0].fulfill(response=held[0][1])  # Real late delivery, before any list timeout.
    settle(page)
    assert failures == []
    expect(page.locator(".draft-row")).to_have_count(0)
    assert page.locator("#draft-list-message").inner_text() == ""
    expect(page.locator("#draft-list-refresh")).to_be_enabled()
    assert form(page, "customer_description").input_value() == ""
    page.unroute(listing)


def lose_first_create(tab):
    """Commit a real POST on the server but lose its response; return the sent envelopes."""
    lost = []

    def lose(route):
        if route.request.method == "POST" and not lost:
            response = route.fetch()
            assert response.ok
            lost.append(route.request.post_data_json)
            route.abort("failed")
        else:
            route.continue_()

    tab.route("**/api/drafts", lose)
    return lost, lose


def relogin_same_user(tab, app, user, server_shift):
    """End the browser session and log the same person in again with password and MFA."""
    tab.context.clear_cookies()
    tab.evaluate("CarbitAuth.lock()")
    app.auth_service.clock = lambda: datetime.now(timezone.utc) + server_shift
    tab.locator("#auth-check").click()
    password(tab, app, user)
    mfa(tab, app, user)


def test_same_user_relogin_keeps_unknown_save_and_retries_exact_envelope(page, local_app):
    user = user_for(local_app)
    start(page, local_app, "Ennen uudelleenkirjautumista lähetetty", "TEST-RELOGIN")
    lost, lose = lose_first_create(page)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("tulos jäi epäselväksi")
    page.unroute("**/api/drafts", lose)
    form(page, "customer_description").fill("Myöhempi tallentamaton teksti")
    relogin_same_user(page, local_app, user, timedelta(seconds=60))
    expect(page.locator("#new-work-dialog")).to_be_visible()
    expect(form(page, "customer_description")).to_have_value("Myöhempi tallentamaton teksti")
    expect(page.locator("#draft-retry")).to_be_visible()
    with page.expect_response(lambda r: r.request.method == "POST" and "/api/drafts" in r.url) as response:
        page.locator("#draft-retry").click()
    expect(page.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
    assert response.value.request.post_data_json == lost[0]
    draft_id = response.value.json()["draft_id"]
    assert stored(local_app, draft_id)[0]["customer_description"] == "Ennen uudelleenkirjautumista lähetetty"
    expect(form(page, "customer_description")).to_have_value("Myöhempi tallentamaton teksti")
    assert counts(local_app) == (1, 0)
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


@pytest.mark.parametrize("age_ms,retried", [(DAY_MS - 1, True), (DAY_MS, False)])
def test_unknown_save_day_limit_boundary_after_same_user_relogin(page, local_app, age_ms, retried):
    user = user_for(local_app)
    page.clock.install()
    start(page, local_app, "Vuorokauden rajan teksti", "TEST-DAY")
    started = page.evaluate("Date.now()") + 1000
    page.clock.pause_at(browser_ms(started))  # Date.now() is exactly `started` when the operation is created.
    lost, lose = lose_first_create(page)
    page.locator("#draft-save").click()
    expect(page.locator("#draft-message")).to_contain_text("tulos jäi epäselväksi")
    page.unroute("**/api/drafts", lose)
    page.clock.resume()
    # A real session cannot live a day: the same person logs in again in server time one day later.
    relogin_same_user(page, local_app, user, timedelta(milliseconds=DAY_MS, seconds=60))
    expect(page.locator("#draft-retry")).to_be_visible()
    page.clock.pause_at(browser_ms(started + age_ms))
    writes = []
    page.on("request", lambda r: writes.append(r.post_data_json)
            if r.method in ("POST", "PUT") and "/api/drafts" in r.url else None)
    page.locator("#draft-retry").click()
    if retried:
        expect(page.locator("#draft-message")).to_contain_text("Luonnos tallennettu.")
        assert writes == lost
    else:
        expect(page.locator("#draft-message")).to_contain_text("liian vanha uusittavaksi")
        expect(page.locator("#draft-retry")).to_be_hidden()
        expect(page.locator("#draft-save")).to_be_disabled()
        assert writes == []
    expect(form(page, "customer_description")).to_have_value("Vuorokauden rajan teksti")
    assert counts(local_app) == (1, 0)  # The original commit exists either way; nothing is duplicated.


def test_list_warns_exactly_at_server_limit_and_labour_drafts_count_toward_it(page, local_app):
    start(page, local_app)
    close(page)
    created = page.evaluate("""async () => {
        for (let i = 0; i < 99; i += 1) {
            await api('/api/drafts', {method: 'POST', body: JSON.stringify({draft_type: 'WORK_INQUIRY',
                request_id: crypto.randomUUID(), payload: {registration: `TEST-${i}`}})});
        }
        return 99;
    }""")
    assert counts(local_app) == (created, 0)
    with page.expect_response(lambda r: "/api/drafts?limit=100" in r.url):
        page.locator("#draft-list-refresh").click()
    expect(page.locator(".draft-row")).to_have_count(99)
    expect(page.locator("#draft-list-message")).to_contain_text("Valitse luonnos jatkaaksesi")
    work = mutation(page, "/api/works", "POST", {"registration": "TEST-WORK", "customer_description": "Fiktiivinen työ",
        "idempotency_key": str(uuid4())})
    mutation(page, "/api/drafts", "POST", {"draft_type": "LABOUR_ENTRY", "request_id": str(uuid4()),
        "work_id": work["id"], "original_work_version": work["version"], "payload": {"hours": "kesken"}})
    with page.expect_response(lambda r: "/api/drafts?limit=100" in r.url) as response:
        page.locator("#draft-list-refresh").click()
    assert len(response.value.json()) == 100
    expect(page.locator(".draft-row")).to_have_count(99)
    expect(page.locator("#draft-list-message")).to_contain_text("Lista on rajattu palvelimen 100 luonnokseen")
    expect(page.locator("#draft-list-message")).to_contain_text("työaikaluonnokset lasketaan rajaan")


def discard_receipts(app, draft_id):
    with app[1]._connect() as connection:
        return connection.execute("SELECT count(*) FROM draft_request_receipts WHERE draft_id=? AND operation='DISCARD'",
                                  (draft_id,)).fetchone()[0]


def termination(app, draft_id):
    with app[1]._connect() as connection:
        row = connection.execute("SELECT reason FROM draft_terminations WHERE draft_id=?", (draft_id,)).fetchone()
    return row and row[0]


def confirm_discard(tab, accept=True):
    prompts = []

    def answer(dialog):
        prompts.append(dialog.message)
        dialog.accept() if accept else dialog.dismiss()

    tab.once("dialog", answer)
    tab.locator("#draft-discard").click()
    return prompts


def lose_discard(tab, draft_id):
    """Commit a real server discard but lose its response; return the sent envelopes."""
    lost = []

    def lose(route):
        response = route.fetch()
        assert response.ok
        lost.append(route.request.post_data_json)
        route.abort("failed")

    pattern = f"**/api/drafts/{draft_id}/discard"
    tab.route(pattern, lose)
    return lost, lambda: tab.unroute(pattern, lose)


def test_discard_requires_confirmation_ends_shared_draft_and_clears_editor(page, local_app):
    start(page, local_app, "Hylättävä fiktiivinen luonnos", "TEST-DISCARD")
    did = save(page)
    open_saved(page, did)
    expect(page.locator(f'[data-draft-id="{did}"]')).to_have_count(1)
    form(page, "customer_description").fill("Tallentamaton lisäys")
    writes = []
    page.on("request", lambda r: writes.append(r.url) if r.method == "POST" and "/discard" in r.url else None)
    prompts = confirm_discard(page, accept=False)
    assert "korjaamon yhteinen luonnos" in prompts[0] and "tallentamattomat muutokset" in prompts[0]
    expect(form(page, "customer_description")).to_have_value("Tallentamaton lisäys")
    assert writes == [] and stored(local_app, did) is not None
    with page.expect_response(lambda r: r.request.method == "POST" and "/discard" in r.url) as response:
        confirm_discard(page)
    assert response.value.ok and response.value.json()["state"] == "DISCARDED"
    expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
    expect(form(page, "customer_description")).to_have_value("")
    expect(page.locator("#draft-discard")).to_be_hidden()
    expect(page.locator(f'[data-draft-id="{did}"]')).to_have_count(0)
    expect(page.locator("#draft-list-message")).to_contain_text("Luonnos hylätty")
    assert stored(local_app, did) is None and termination(local_app, did) == "DISCARDED"
    assert discard_receipts(local_app, did) == 1 and counts(local_app) == (1, 0)
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


@pytest.mark.parametrize("loss", ["lost-response", "timeout"])
def test_unknown_discard_after_server_commit_retries_exact_envelope_once(page, local_app, loss):
    if loss == "timeout":
        page.clock.install()
    start(page, local_app, "Epäselvästi hylätty", "TEST-UNKNOWN")
    did = save(page)
    request = {}
    if loss == "lost-response":
        lost, release = lose_discard(page, did)
    else:
        lost, held = [], []

        def hold(route):
            held.append(route.fetch())  # The server commits; the browser never gets the answer.
            lost.append(route.request.post_data_json)
            page.evaluate("window.discardHeld = true")

        page.route(f"**/api/drafts/{did}/discard", hold)
    confirm_discard(page)
    if loss == "timeout":
        page.wait_for_function("window.discardHeld === true")
        page.clock.fast_forward(15000)
    expect(page.locator("#draft-message")).to_contain_text("Hylkäyksen tulos jäi epäselväksi")
    expect(page.locator("#draft-retry")).to_have_text("Uusi sama hylkäys")
    expect(form(page, "customer_description")).to_be_disabled()
    expect(page.locator("#draft-save")).to_be_disabled()
    assert stored(local_app, did) is None and discard_receipts(local_app, did) == 1
    if loss == "lost-response":
        release()
    else:
        page.unroute_all(behavior="ignoreErrors")
    with page.expect_response(lambda r: r.request.method == "POST" and "/discard" in r.url) as response:
        page.locator("#draft-retry").click()
    request["body"] = response.value.request.post_data_json
    assert request["body"] == lost[0]
    expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
    expect(page.locator("#draft-retry")).to_be_hidden()
    assert discard_receipts(local_app, did) == 1 and termination(local_app, did) == "DISCARDED"


def test_discard_after_colleague_save_conflicts_keeps_text_and_needs_server_version(page, local_app, other_staff):
    other, _ = other_staff
    start(page, local_app, "Alkuperäinen yhteinen", "TEST-DCONFLICT")
    did = save(page)
    other.goto(local_app[0], wait_until="networkidle")
    open_saved(other, did)
    form(other, "customer_description").fill("Kollegan uudempi versio")
    save(other)
    form(page, "customer_description").fill("Oma tallentamaton teksti")
    confirm_discard(page)
    expect(page.locator("#draft-message")).to_contain_text("Versioristiriita: luonnosta ei hylätty")
    expect(form(page, "customer_description")).to_have_value("Oma tallentamaton teksti")
    expect(page.locator("#draft-retry")).to_be_hidden()
    assert stored(local_app, did)[0]["customer_description"] == "Kollegan uudempi versio"
    assert discard_receipts(local_app, did) == 0
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#draft-reload").click()
    expect(form(page, "customer_description")).to_have_value("Kollegan uudempi versio")
    confirm_discard(page)
    expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
    assert stored(local_app, did) is None


def test_colleague_editor_after_discard_cannot_recreate_and_keeps_text(page, local_app, other_staff):
    other, _ = other_staff
    start(page, local_app, "Kollegan auki pitämä", "TEST-DGONE")
    did = save(page)
    other.goto(local_app[0], wait_until="networkidle")
    open_saved(other, did)
    confirm_discard(page)
    expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
    form(other, "customer_description").fill("Kollegan myöhäinen teksti")
    other.locator("#draft-save").click()
    # An ended draft may surface as unavailable or as a conflict; both keep the text and write nothing.
    expect(other.locator("#draft-message")).to_contain_text(re.compile("ei ole saatavilla|Versioristiriita"))
    expect(form(other, "customer_description")).to_have_value("Kollegan myöhäinen teksti")
    expect(other.locator("#draft-save")).to_be_disabled()
    assert stored(local_app, did) is None and counts(local_app) == (1, 0)


@pytest.mark.parametrize("identity", ["same-user", "other-staff"])
def test_unknown_discard_survives_same_user_relogin_but_not_identity_change(page, local_app, other_staff, identity):
    _, staff = other_staff
    user = user_for(local_app)
    start(page, local_app, "Uudelleenkirjautumisen yli", "TEST-DRELOGIN")
    did = save(page)
    lost, release = lose_discard(page, did)
    confirm_discard(page)
    expect(page.locator("#draft-message")).to_contain_text("Hylkäyksen tulos jäi epäselväksi")
    release()
    writes = []
    page.on("request", lambda r: writes.append(r.post_data_json) if r.method == "POST" and "/discard" in r.url else None)
    if identity == "same-user":
        relogin_same_user(page, local_app, user, timedelta(seconds=60))
        expect(page.locator("#draft-retry")).to_have_text("Uusi sama hylkäys")
        page.locator("#draft-retry").click()
        expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
        assert writes == lost
    else:
        page.context.add_cookies([{"name": SessionCookiePolicy().name, "value": staff.token.get_secret_value(),
            "url": local_app[0], "secure": True, "httpOnly": True, "sameSite": "Strict"}])
        page.evaluate("CarbitAuth.lock()")
        page.locator("#auth-check").click()
        expect(page.locator("#business-shell")).to_be_visible()
        expect(page.locator("#draft-retry")).to_be_hidden()
        expect(page.locator("#draft-discard")).to_be_hidden()
        assert form(page, "customer_description").input_value() == ""
        assert writes == []
    assert discard_receipts(local_app, did) == 1 and stored(local_app, did) is None


def test_list_request_started_before_discard_cannot_restore_the_discarded_row(page, local_app):
    start(page, local_app, "Hylätään listahaun aikana", "TEST-DRACE")
    did = save(page)
    open_saved(page, did)
    close(page)
    held, failures, listing = hold_lists(page)
    page.locator("#draft-list-refresh").click()
    page.wait_for_function("window.draftListsHeld === 1")
    expect(page.locator("#draft-list-refresh")).to_be_disabled()
    page.locator(f'[data-draft-id="{did}"]').click()  # The row from the earlier list stays clickable.
    expect(page.locator("#draft-message")).to_contain_text("Palvelimen luonnos avattu")
    confirm_discard(page)
    expect(page.locator("#draft-message")).to_contain_text("Luonnos hylätty.")
    expect(page.locator(f'[data-draft-id="{did}"]')).to_have_count(0)
    expect(page.locator("#draft-list-refresh")).to_be_enabled()
    held[0][0].fulfill(response=held[0][1])  # The pre-discard list, which still contains the row.
    settle(page)
    page.unroute(listing)
    expect(page.locator(f'[data-draft-id="{did}"]')).to_have_count(0)
    expect(page.locator("#draft-list-message")).to_contain_text("Luonnos hylätty")
    assert failures == [] and stored(local_app, did) is None
