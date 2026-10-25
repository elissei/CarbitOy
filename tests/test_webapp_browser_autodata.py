"""Autodata 1B-4 T1–T38, exclusively fresh synthetic HTTPS databases and fake.

Evidence: A = real browser/HTTP/service/repository/fake chain; B = real write
then damaged/held response; C = fully substituted response; D = fixture setup
through a service/repository or settings change. No real vendor is contacted.
All contexts block non-loopback traffic, and assert its count is zero (T24).
"""

import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError, expect

from http_auth_support import create_test_app, grant_session, local_tls_files, settings_for
from integrations.autodata.fake_adapter import FakeOperation, FakeVariant, FakeVehicle
from integrations.autodata.models import LookupOutcome
from integrations.autodata.runner import LookupRunner
from test_webapp_autodata_http import ENABLED, DISABLED, FAKE, choice, counting_fake
from test_webapp_browser import LocalApp, browser, fill_inquiry, fill_labour, open_work, seed, wait_for_held
from test_webapp_browser_auth import password, mfa, user_for, assert_business_locked
from webapp import autodata_service, http_autodata
from webapp.auth_models import SessionCookiePolicy, UserRole
from webapp.autodata_migrations import CLOSE_CONFIRMATION
from webapp.autodata_records import LookupCommand
from webapp.autodata_service import AutodataAcceptanceService, AutodataLookupService, LookupQuota
from webapp.workshop_context import WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context


VARIANTS = (FakeVariant(choice("Vakio"), "0,8 h"), FakeVariant(choice("Vaihtoehto"), "1,1 h"))
OPERATIONS = tuple(FakeOperation(choice("Jarrut", label), VARIANTS) for label in
                   ("Etujarrupalat, vaihto", "Takajarrupalat, vaihto"))
FLEET = tuple(FakeVehicle(choice("Testimerkki", label), OPERATIONS) for label in ("Malli A", "Malli B"))
SINGLE = (FakeVehicle(choice("Testimerkki", "Malli A"), (
    FakeOperation(choice("Jarrut", "Etujarrupalat, vaihto"), (VARIANTS[0],)),)),)
QUERY = '#autodata-form [name="work_query"]'
SEARCH = '#autodata-form button[type="submit"]'
ACCEPT = '#autodata-accept'
CONFIRM = '#autodata-confirm'
RETRY_LOOKUP = '[data-autodata-retry="lookup"]'
RETRY_ACCEPT = '[data-autodata-retry="accept"]'


@pytest.fixture(autouse=True)
def isolated_quota(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(autodata_service, "PROCESS_LOOKUP_QUOTA", LookupQuota())


@pytest.fixture
def local_app(tmp_path):
    # Paths are checked before create_app/lifespan initializes any database.
    root = Path(__file__).resolve().parents[1]
    for path in (tmp_path / "works.db", tmp_path / "auth.db"):
        assert path.is_relative_to(tmp_path) and not path.exists()
        assert not path.resolve().is_relative_to(root / "var")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    adapter = counting_fake(SINGLE)
    runner = LookupRunner(adapter, FAKE)
    app = create_test_app(tmp_path / "works.db", auth_settings=settings_for(tmp_path / "auth.db", url),
                          autodata_settings=ENABLED, autodata_runner=runner)
    cert, key = local_tls_files(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", proxy_headers=False,
                                         ssl_certfile=str(cert), ssl_keyfile=str(key),
                                         timeout_keep_alive=1, timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Synthetic test server did not start")
            threading.Event().wait(0.01)
        grant = grant_session(app.state.auth_service)
        env = LocalApp(url, app.state.repository, grant.token.get_secret_value(), app.state.auth_service)
        env.app, env.adapter, env.runner = app, adapter, runner
        yield env
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


class ManagedRoute:
    """Keep deferred routes owned until an explicit terminal route operation."""

    def __init__(self, route, ledger):
        self.route, self.ledger = route, ledger

    @property
    def request(self):
        return self.route.request

    def fetch(self, **kwargs):
        kwargs.setdefault('timeout', 30000)
        return self.route.fetch(**kwargs)

    def finish(self, method, *args, **kwargs):
        try:
            result = getattr(self.route, method)(*args, **kwargs)
        except PlaywrightError as problem:
            if not self.ledger.closing or 'Route is already handled' not in str(problem):
                raise
            # Chromium may already have ended a client-aborted request. Settle
            # the SDK's still waiting route future through its public fallback.
            # This is completion, not ignoreErrors or warning suppression.
            result = self.route.fallback()
            self.ledger.already_handled += 1
        self.ledger.pending.discard(self)
        return result

    def abort(self, *args, **kwargs):
        return self.finish('abort', *args, **kwargs)

    def fulfill(self, *args, **kwargs):
        return self.finish('fulfill', *args, **kwargs)

    def continue_(self, *args, **kwargs):
        return self.finish('continue_', *args, **kwargs)

    def fallback(self, *args, **kwargs):
        return self.finish('fallback', *args, **kwargs)


class RouteLedger:
    """Public-API cleanup, including expired times=1 and deferred handlers.

    Playwright's ignoreErrors does not wait for a route's handled future. A
    callback which merely stores a route leaves that future pending even after
    the Python callback returns. Abort those routes before unroute_all(wait).
    """

    def __init__(self):
        self.pending, self.running = set(), set()
        self.surfaces, self.pages = [], []
        self.closing = False
        self.aborted = 0
        self.already_handled = 0

    def install(self, surface, *, page=False):
        if surface in self.surfaces:
            return surface
        native_route, native_unroute = surface.route, surface.unroute
        handlers = []

        def register(url, handler, *, times=None):
            def wrapped(raw):
                route = ManagedRoute(raw, self)
                self.pending.add(route)
                self.running.add(route)
                try:
                    if self.closing:
                        route.abort()
                        self.aborted += 1
                    else:
                        handler(route)
                finally:
                    self.running.discard(route)

            handlers.append((url, handler, wrapped))
            native_route(url, wrapped, times=times)

        def unroute(url, handler=None):
            wrapped = next((proxy for registered, original, proxy in reversed(handlers)
                            if original is handler and registered == url), handler)
            native_unroute(url, wrapped)

        surface.route, surface.unroute = register, unroute
        self.surfaces.append(surface)
        if page:
            self.pages.append(surface)
            # Keep page interception installed while a times=1 callback may
            # still own a deferred route. Remove it only after drain settles
            # those routes; unrelated unroute calls must not end interception.
            register('**/*', lambda route: route.fallback())
        return surface

    def drain(self):
        self.closing = True
        live = [page for page in self.pages if not page.is_closed()]
        # Stop application follow-up reads without revoking server-side state.
        for page in live:
            page.evaluate('window.CarbitAuth?.lock()')
        deadline = time.monotonic() + 10
        while self.pending or self.running:
            for route in tuple(self.pending):
                if route not in self.running:
                    route.abort()
                    self.aborted += 1
            if time.monotonic() >= deadline:
                raise AssertionError('Synthetic route cleanup did not finish within 10 seconds')
            if live:
                live[0].wait_for_timeout(10)
            else:
                assert not self.pending and not self.running
        for surface in self.surfaces:
            surface.unroute_all(behavior='wait')
        # Let handled futures and Page._on_route finally tasks finish while the
        # driver/event loop still exist, including already expired handlers.
        for page in live:
            page.wait_for_timeout(10)
        assert not self.pending and not self.running


@pytest.fixture
def page(browser, local_app):
    ledger = RouteLedger()
    context = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    ledger.install(context)
    context.add_cookies([{"name": SessionCookiePolicy().name, "value": local_app.token,
                         "url": local_app[0], "secure": True, "httpOnly": True, "sameSite": "Strict"}])
    external = []

    def loopback_only(route):
        if route.request.url.startswith(local_app[0] + "/"):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    context.route("**/*", loopback_only)
    tab = ledger.install(context.new_page(), page=True)
    tab.route_ledger = ledger
    context.on('page', lambda surface: ledger.install(surface, page=True) if surface not in ledger.pages else None)
    extra_contexts = []

    def staff_page():
        grant = grant_session(local_app.auth_service)
        staff_context = ledger.install(browser.new_context(ignore_https_errors=True))
        extra_contexts.append(staff_context)
        staff_context.add_cookies([{'name': SessionCookiePolicy().name, 'value': grant.token.get_secret_value(),
                                   'url': local_app[0], 'secure': True, 'httpOnly': True, 'sameSite': 'Strict'}])
        staff_context.route('**/*', loopback_only)
        staff = ledger.install(staff_context.new_page(), page=True)
        staff.test_user = grant.user
        return staff

    tab.new_staff_page = staff_page
    tab.writes = []
    tab.responses = []
    tab.on("request", lambda req: tab.writes.append(req) if "/autodata/" in req.url and req.method == "POST" else None)
    tab.on("response", lambda response: tab.responses.append(response) if "/autodata/" in response.url else None)
    try:
        yield tab
    finally:
        try:
            ledger.drain()
        finally:
            for staff_context in extra_contexts:
                staff_context.close()
            context.close()
        assert external == []  # T24 applies to every case, including second tabs.


def start(tab, env, registration="TEST-AD"):
    work = seed(env[1], registration)
    tab.goto(env[0], wait_until="networkidle")
    open_work(tab, registration)
    expect(tab.locator(SEARCH)).to_be_visible()
    return work


def search(tab, query="etujarrupalat"):
    tab.locator(QUERY).fill(query)
    tab.locator(SEARCH).click()
    expect(tab.locator('#autodata-result h4')).to_be_visible()


def found(tab, env):
    work = start(tab, env)
    search(tab)
    expect(tab.locator('#autodata-check-status')).to_have_text("Hyväksyttävissä")
    # Finish the post-lookup history read before a caller revokes the session
    # or installs a response route intended for the next explicit operation.
    expect(tab.locator('[data-lookup-id]')).to_have_count(1)
    return work


def accept(tab):
    tab.locator(CONFIRM).check()
    expect(tab.locator(ACCEPT)).to_be_enabled()
    tab.locator(ACCEPT).click()


def accepted(tab):
    expect(tab.locator('#price-summary')).to_contain_text("72,00")
    expect(tab.locator('#event-list')).to_contain_text("Autodata-haun työaika hyväksytty")
    expect(tab.locator('#autodata-decisions')).to_contain_text("Fictional HTTP test user")


def ctx(env):
    return resolve_workshop_context(env.auth_service, env.token,
                                    required_permission=WorkshopPermission.APPROVE_LABOUR_PLAN)


def records(env, work):
    repo = env.app.state.autodata_repository
    return repo.list_lookups(ctx(env), inquiry_id=work['id'], allow_synthetic=True), repo.list_decisions(ctx(env), inquiry_id=work['id'])


def lookup_id(tab):
    return next(req.url.split('/lookups/')[1].split('/')[0] for req in reversed(tab.writes)
                if req.url.endswith('/accept')) if any(req.url.endswith('/accept') for req in tab.writes) else (
        next(response.json()['lookup']['lookup_id'] for response in reversed(tab.responses)
             if response.request.method == 'POST' and response.status in (200, 201)))


def second(tab, env, registration):
    other = tab.context.new_page()
    other.goto(env[0], wait_until="networkidle")
    open_work(other, registration)
    return other


def history_open(tab, id):
    tab.locator(f'[data-lookup-id="{id}"]').click()


def update_manual(tab, hours="2", description="Toisen välilehden työ"):
    fill_labour(tab, description, hours)
    tab.locator('#labour-form button[type="submit"]').click()
    expect(tab.locator('#price-summary')).to_contain_text("180,00" if hours == "2" else "270,00")


def rewire(env, settings):
    app = env.app
    app.state.autodata_settings = settings
    app.state.autodata_lookups = AutodataLookupService(app.state.autodata_repository, settings,
                                                      env.runner if settings.enabled else None)
    app.state.autodata_acceptance = AutodataAcceptanceService(app.state.autodata_repository, settings)


def redact(env, work, id):
    env.app.state.autodata_repository.redact_lookup(ctx(env), inquiry_id=work['id'], lookup_id=id,
                                                   reason_code="ENTERED_IN_ERROR")


def close(env, work):
    env.app.state.autodata_repository.close_autodata(ctx(env), inquiry_id=work['id'],
                                                    reason_code="RETENTION", confirmation=CLOSE_CONFIRMATION)


def direct_accept(tab, work, id):
    return tab.evaluate("""async ({work, id}) => {
      const response = await fetch(`/api/works/${work.id}/autodata/lookups/${id}/accept`, {
        method: 'POST', headers: {'Content-Type':'application/json', 'X-CSRF-Token': await sessionProof()},
        body: JSON.stringify({request_id: crypto.randomUUID(), expected_version: state.selected.version,
          confirmation:'VEHICLE_OPERATION_VARIANT_TIME'})});
      return {status: response.status, body: await response.json()};
    }""", {"work": work, "id": id})


def lose_response(tab, kind):
    pattern = '**/autodata/lookups' if kind == 'lookup' else '**/autodata/lookups/*/accept'
    held = []

    def lose(route):
        if route.request.method != 'POST':
            route.fallback()
            return
        body = route.request.post_data
        response = route.fetch()
        held.append((body, response.json()))
        route.abort()

    tab.route(pattern, lose, times=1)
    return held


def retry(tab, kind):
    selector = RETRY_LOOKUP if kind == 'lookup' else RETRY_ACCEPT
    expect(tab.locator(selector)).to_be_enabled()
    tab.locator(selector).click()


def test_t01_desktop_real_login_full_chain(page, local_app):
    """T1 A/R: UI creation, three choices, exact time, acceptance, reload."""
    local_app.adapter.vehicles = FLEET
    page.context.clear_cookies()
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    page.goto(local_app[0] + '/login', wait_until="networkidle")
    password(page, local_app)
    mfa(page, local_app)
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator('#detail-registration')).to_have_text('TEST-NEW')
    expect(page.locator(SEARCH)).to_be_visible()
    search(page, 'jarrupalat')
    for legend in ('Valitse ajoneuvo', 'Valitse työ', 'Valitse variantti'):
        expect(page.locator('#autodata-result legend')).to_have_text(legend)
        page.locator('#autodata-result input[type=radio]').first.check()
        page.locator('#autodata-continue').click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    expect(page.locator(ACCEPT)).to_be_disabled()
    for text in ('SYNTEETTINEN – ei Autodata-aineistoa', 'Lähteen teksti: 0,8 h', 'Työaika: 0,8 h', 'fake-1', 'Voimassa'):
        expect(page.locator('#autodata-result')).to_contain_text(text)
    accept(page)
    accepted(page)
    expect(page.locator('#event-list')).to_contain_text('(kirjautunut käyttäjä)')
    expect(page.locator('#labour-form [name=hours]')).to_have_value('')
    expect(page.locator('#labour-notice')).to_contain_text('korvaa nykyisen Autodata-ajan')
    assert page.locator('#autodata-panel [name=hours]').count() == 0
    work = local_app[1].get_work(local_app[1].list_works()[0]['id'])
    assert work['labour_source'] == 'AUTODATA' and work['total_price'] == '72.00'
    rows, decisions = records(local_app, work)
    assert len(rows['items']) == 4 and len(decisions['items']) == 1
    assert local_app.adapter.calls == 4
    initial = page.writes[0].post_data_json
    assert set(initial['vehicle_facts'].values()) == {None}
    assert 'registration' not in initial['vehicle_facts']
    assert page.writes[2].post_data_json['vehicle_selection'] is not None
    assert page.writes[3].post_data_json['operation_selection'] is not None
    page.reload(wait_until='networkidle')
    open_work(page, 'TEST-NEW')
    accepted(page)


def test_t02_mobile_keyboard_and_targets(page, local_app):
    """T2 A: native radio keyboard navigation and 390px layout."""
    page.set_viewport_size({'width': 390, 'height': 844})
    local_app.adapter.vehicles = FLEET
    start(page, local_app)
    page.locator(QUERY).fill('jarrupalat')
    page.locator(QUERY).press('Tab')
    assert page.locator(SEARCH).evaluate('node => node === document.activeElement')
    page.keyboard.press('Enter')
    for _ in range(3):
        expect(page.locator('#autodata-result fieldset')).to_be_visible()
        expect(page.locator('#autodata-result h4')).to_be_focused()
        page.keyboard.press('Tab')
        page.keyboard.press('Space')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('ArrowUp')
        page.keyboard.press('Tab')
        page.keyboard.press('Enter')
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#autodata-status').get_attribute('role') == 'status'
    assert page.locator('#autodata-error').get_attribute('role') == 'alert'
    for box in page.locator('#autodata-panel button:visible, #autodata-panel label:visible').all():
        assert box.bounding_box()['height'] >= 44
    page.locator(CONFIRM).focus()
    page.keyboard.press('Space')
    page.keyboard.press('Tab')
    page.keyboard.press('Enter')
    accepted(page)


def test_t03_only_candidate(page, local_app):
    """T3 A."""
    found(page, local_app)
    assert page.locator('#autodata-result').inner_text().count('ainoa vaihtoehto – tarkista') == 3


def test_t04_change_clears_card_confirmation_and_lookup_retry(page, local_app):
    """T4 A/B: new search key and cleared lookup replay, accept replay survives."""
    found(page, local_app)
    first = page.writes[0].post_data_json['request_id']
    page.locator(CONFIRM).check()
    page.locator('#autodata-form [name=make]').fill('Tarkennus')
    assert page.locator(ACCEPT).count() == 0
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert page.writes[-1].post_data_json['request_id'] != first
    held = lose_response(page, 'accept')
    accept(page)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    page.locator(QUERY).fill('muuttunut')
    expect(page.locator(RETRY_ACCEPT)).to_be_visible()
    assert held
    # The retained acceptance body is independent of search inputs.
    retry(page, 'accept')
    accepted(page)


def test_t04_edit_discards_uncertain_lookup_envelope(page, local_app):
    """T4 B+A: edit is possible after lost response and abandons only lookup."""
    start(page, local_app)
    lost = lose_response(page, 'lookup')
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator(RETRY_LOOKUP)).to_be_enabled()
    page.locator('#autodata-form [name=model]').fill('Tarkennus')
    expect(page.locator(RETRY_LOOKUP)).to_have_count(0)
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert page.writes[-1].post_data != lost[0][0]
    assert local_app.adapter.calls == 2


def test_t05_double_submit_guards_and_releases(page, local_app):
    """T5 A: duplicate events while each genuine request is held."""
    work = start(page, local_app)
    held = []

    def hold(route):
        if route.request.method == 'POST':
            held.append((route, route.fetch()))
        else:
            route.fallback()

    page.route('**/autodata/lookups', hold)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    page.locator('#autodata-form').evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
    wait_for_held(page, held, 0)
    assert len(page.writes) == 1
    route, response = held.pop()
    route.fulfill(response=response)
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    page.unroute('**/autodata/lookups', hold)
    page.route('**/autodata/lookups/*/accept', hold)
    accept(page)
    page.locator(ACCEPT).evaluate("node => { node.dispatchEvent(new MouseEvent('click')); node.dispatchEvent(new MouseEvent('click')); }")
    wait_for_held(page, held, 0)
    assert len(page.writes) == 2
    route, response = held.pop()
    route.fulfill(response=response)
    accepted(page)
    assert len(records(local_app, work)[1]['items']) == 1
    # A new deliberate search proves the lookup lock was released.
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert len(page.writes) == 3
    page.unroute('**/autodata/lookups/*/accept', hold)
    accept(page)
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '3')
    assert len(page.writes) == 4


@pytest.mark.parametrize('kind', ['lookup', 'accept'])
def test_t06_t07_lost_response_explicit_identical_replay(page, local_app, kind):
    """T6/T7 B; also T5 retry double-click and 2s no automatic POST."""
    work = start(page, local_app) if kind == 'lookup' else found(page, local_app)
    fill_labour(page, 'Säilyvä luonnos', '1.25')
    held = lose_response(page, kind)
    if kind == 'lookup':
        page.locator(QUERY).fill('etujarrupalat')
        page.locator(SEARCH).click()
    else:
        accept(page)
    selector = RETRY_LOOKUP if kind == 'lookup' else RETRY_ACCEPT
    expect(page.locator(selector)).to_be_enabled()
    size = len(page.writes)
    page.wait_for_timeout(2000)
    assert len(page.writes) == size
    # Hold replay to prove the handler guard, independent of disabled paint.
    replayed = []
    pattern = '**/autodata/lookups' if kind == 'lookup' else '**/autodata/lookups/*/accept'

    def hold(route):
        if route.request.method == 'POST':
            replayed.append((route, route.fetch()))
        else:
            route.fallback()

    page.route(pattern, hold)
    page.locator(selector).click()
    page.locator(selector).evaluate('node => { node.disabled=false; node.click(); node.click(); }')
    wait_for_held(page, replayed, 0)
    assert len(replayed) == 1 and len(page.writes) == size + 1
    route, response = replayed[0]
    assert route.request.post_data == held[0][0]
    assert response.json()['replayed'] is True
    route.fulfill(response=response)
    expect(page.locator(selector)).to_have_count(0)
    assert local_app.adapter.calls == 1
    if kind == 'accept':
        accepted(page)
        expect(page.locator('#autodata-status')).to_contain_text('Hyväksyntä oli jo tallennettu')
        assert len(records(local_app, work)[1]['items']) == 1
        expect(page.locator('#labour-form')).to_have_attribute('data-version', '2')
    else:
        expect(page.locator('#autodata-status')).to_contain_text('Haku oli jo tallennettu')
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Säilyvä luonnos')


def test_t08_real_busy_then_same_request(page, local_app):
    """T8 A: fake-clock event holds A inside real process lookup lock."""
    start(page, local_app)
    other = second(page, local_app, 'TEST-AD')
    expect(other.locator(SEARCH)).to_be_visible()
    bodies = []
    other.on('request', lambda req: bodies.append(req.post_data) if req.method == 'POST' and '/autodata/' in req.url else None)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(10)

    local_app.adapter.during = block
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    try:
        deadline = time.monotonic() + 5
        while not entered.is_set() and time.monotonic() < deadline:
            page.wait_for_timeout(10)  # Pump Playwright's loopback route callbacks.
        assert entered.is_set()
        other.locator(QUERY).fill('etujarrupalat')
        other.locator(SEARCH).click()
        expect(other.locator('#autodata-error')).to_be_visible()
        expect(other.locator(RETRY_LOOKUP)).to_be_enabled()
        before = len(local_app.adapter.requests)
        other.wait_for_timeout(300)
        assert len(local_app.adapter.requests) == before == 1
    finally:
        local_app.adapter.during = None
        release.set()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    with other.expect_response('**/autodata/lookups') as response:
        retry(other, 'lookup')
    assert response.value.status == 201
    expect(other.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def test_t09_real_quota_retry_after(page, local_app, monkeypatch):
    """T9 A: real 429, no automatic adapter call."""
    monkeypatch.setattr(autodata_service, 'PROCESS_LOOKUP_QUOTA', LookupQuota(limit=1))
    found(page, local_app)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-retries')).to_contain_text('Uusi haku mahdollinen noin klo')
    expect(page.locator(RETRY_LOOKUP)).to_be_disabled()
    size = len(page.writes)
    page.wait_for_timeout(300)
    assert local_app.adapter.calls == 1 and len(page.writes) == size


def test_t10_stale_view_preserves_original_manual_version(page, local_app):
    """T10 A: external manual PUT, explicit GET, original draft binding."""
    work = start(page, local_app)
    fill_labour(page, 'Oma tallentamaton', '1.25')
    other = second(page, local_app, 'TEST-AD')
    update_manual(other)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_contain_text('muuttunut toisaalla')
    assert page.evaluate('state.selected.version') == 1
    page.locator('#autodata-reload').click()
    expect(page.locator('#labour-notice')).to_contain_text('Lomake on tehty versiolle 1')
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '1')
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Oma tallentamaton')
    puts = []
    page.on('request', lambda req: puts.append(req) if req.method == 'PUT' else None)
    page.locator('#labour-form button[type=submit]').click()
    assert puts == [] and local_app[1].get_work(work['id'])['labour_hours'] == '2'
    page.locator('#labour-reset').click()
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Toisen välilehden työ')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')


def test_t11_stale_lookup_server_authority(page, local_app):
    """T11 A: fresh check plus forced HTTP refusal, explicit reload gate."""
    work = found(page, local_app)
    id = lookup_id(page)
    other = second(page, local_app, 'TEST-AD')
    update_manual(other)
    page.locator('#autodata-check').click()
    expect(page.locator('#autodata-check-status')).to_contain_text('muuttunut haun jälkeen')
    expect(page.get_by_role('button', name='Tee uusi haku', exact=True)).to_be_disabled()
    response = direct_accept(page, work, id)
    assert response['status'] == 409 and response['body']['code'] == 'STALE_LOOKUP'
    page.locator('#autodata-reload').click()
    expect(page.get_by_role('button', name='Tee uusi haku', exact=True)).to_be_enabled()


@pytest.mark.parametrize('checked_first', [False, True])
def test_t12_expired_server_authority(page, local_app, checked_first):
    """T12 A: injected repository clock; also stale precheck accepted by server? no."""
    work = found(page, local_app)
    if checked_first:
        page.locator(CONFIRM).check()
    local_app.app.state.autodata_repository.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
    if checked_first:
        page.locator(ACCEPT).click()
        expect(page.locator('#autodata-error')).to_contain_text('Vanhentunut')
    else:
        page.locator('#autodata-check').click()
    expect(page.locator('#autodata-check-status')).to_contain_text('Vanhentunut')
    assert page.locator(ACCEPT).count() == 0
    assert records(local_app, work)[1]['items'] == []


def test_t13_two_tabs_accept_stale_lookup(page, local_app):
    """T13 A: version is checked before already-accepted; exactly one decision."""
    work = found(page, local_app)
    id = lookup_id(page)
    other = second(page, local_app, 'TEST-AD')
    history_open(other, id)
    expect(other.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    other.locator(CONFIRM).check()
    accept(page)
    accepted(page)
    with other.expect_response('**/accept') as response:
        other.locator(ACCEPT).click()
    assert response.value.status == 409 and response.value.json()['code'] == 'STALE_LOOKUP'
    expect(other.locator('#autodata-decisions')).to_contain_text('hyväksyjä')
    expect(other.locator('#autodata-check-status')).to_have_text('Haku on jo hyväksytty.')
    assert other.evaluate('state.selected.version') == 1
    expect(other.get_by_role('button', name='Tee uusi haku', exact=True)).to_be_disabled()
    assert len(records(local_app, work)[1]['items']) == 1


@pytest.mark.parametrize('failure,message', [
    ('LOGIN_REQUIRED', 'vaatii kirjautumisen'), ('BLOCKED', 'pysäytettiin'),
    ('SITE_CHANGED', 'sisältöä ei voitu käyttää'), ('TIMEOUT', 'aikakatkaistiin'),
    ('ERROR', 'epäonnistui'), ('VEHICLE_NOT_FOUND', 'Ajoneuvoa ei löytynyt'),
    ('WORK_NOT_FOUND', 'Työtä ei löytynyt'), ('TIME_MISSING', 'ei ollut työaikaa'),
    ('UNPARSEABLE', 'ei voitu tulkita')])
def test_t14_failures_never_invent_hours(page, local_app, failure, message):
    """T14 A: source-level fake results, never substituted HTTP."""
    if failure in ('LOGIN_REQUIRED', 'BLOCKED', 'SITE_CHANGED', 'TIMEOUT', 'ERROR'):
        local_app.adapter.failure = LookupOutcome(failure)
    elif failure == 'VEHICLE_NOT_FOUND':
        local_app.adapter.vehicles = ()
    elif failure in ('TIME_MISSING', 'UNPARSEABLE'):
        local_app.adapter.vehicles = (FakeVehicle(choice('Testi'), (FakeOperation(choice('etujarrupalat'), (
            FakeVariant(choice('Vakio'), None if failure == 'TIME_MISSING' else '01:30'),)),)),)
    work = start(page, local_app)
    search(page, 'ei-osumia' if failure == 'WORK_NOT_FOUND' else 'etujarrupalat')
    expect(page.locator('#autodata-result')).to_contain_text(message)
    expect(page.locator('#autodata-result')).to_contain_text('Voit kirjata työajan käsin alla')
    assert page.locator(ACCEPT).count() == 0 and 'Työaika:' not in page.locator('#autodata-result').inner_text()
    assert local_app[1].get_work(work['id'])['total_price'] is None
    fill_labour(page)
    expect(page.locator('#labour-form button[type=submit]')).to_be_enabled()
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-result h4')).to_be_visible()
    assert page.writes[0].post_data_json['request_id'] != page.writes[-1].post_data_json['request_id']


def test_t15_identifier_rejected_before_adapter(page, local_app):
    """T15 A."""
    start(page, local_app)
    page.locator(QUERY).fill('TEST-AD etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_contain_text('Hakuteksti ei saa sisältää')
    assert local_app.adapter.calls == 0


def test_t16_disabled_card_and_manual_path(page, local_app):
    """T16 D+A: same databases, settings switch, real disabled POST."""
    work = found(page, local_app)
    id = lookup_id(page)
    rewire(local_app, DISABLED)
    page.locator('#autodata-refresh').click()
    expect(page.locator('#autodata-status')).to_contain_text('ei ole käytössä')
    assert page.locator(CONFIRM).count() == page.locator(ACCEPT).count() == 0
    history_open(page, id)
    # Disabled settings cannot allow synthetic acceptance; real fake rows have
    # SYNTHETIC status. The ACCEPTABLE+disabled UI combination is tested below.
    expect(page.locator('#autodata-check-status')).to_have_text('SYNTHETIC')
    assert page.locator(CONFIRM).count() == page.locator(ACCEPT).count() == 0
    expect(page.locator('#autodata-form')).to_be_hidden()
    response = direct_accept(page, work, id)
    assert response['status'] == 503 and response['body']['code'] == 'AUTODATA_DISABLED'
    update_manual(page)
    assert local_app[1].get_work(work['id'])['labour_source'] == 'MANUAL_AUTODATA'


def test_t17_it_admin_readonly(page, local_app):
    """T17 A: existing data prepared through genuine staff browser lookup."""
    work = found(page, local_app)
    grant = grant_session(local_app.auth_service, UserRole.IT_ADMIN)
    page.context.add_cookies([{'name': SessionCookiePolicy().name, 'value': grant.token.get_secret_value(),
                              'url': local_app[0], 'secure': True, 'httpOnly': True, 'sameSite': 'Strict'}])
    page.reload(wait_until='networkidle')
    open_work(page, 'TEST-AD')
    expect(page.locator('#autodata-lookups button')).to_have_count(1)
    page.locator('#autodata-lookups button').click()
    expect(page.locator('#autodata-result')).to_contain_text('0,8')
    expect(page.locator('#autodata-form')).to_be_hidden()
    assert page.locator(CONFIRM).count() == page.locator(ACCEPT).count() == 0
    assert len(page.writes) == 1 and records(local_app, work)[1]['items'] == []


def test_t16_disabled_even_when_list_says_acceptable(page, local_app):
    """T16 C supplement: enabled=false blocks an otherwise ACCEPTABLE card."""
    found(page, local_app)
    id = lookup_id(page)
    page.route('**/autodata/lookups', lambda route: route.fulfill(json={
        'items': [{'lookup_id': id, 'acceptability': 'ACCEPTABLE'}],
        'next': None, 'autodata_enabled': False}), times=1)
    page.locator(CONFIRM).check()
    page.locator('#autodata-check').click()
    expect(page.locator('#autodata-status')).to_contain_text('ei ole käytössä')
    expect(page.locator('#autodata-form')).to_be_hidden()
    assert page.locator(CONFIRM).count() == page.locator(ACCEPT).count() == 0


def test_t18_401_preserves_dom_and_same_user_retry(page, local_app):
    """T18 A/R: rejected before write, real same-user password/MFA."""
    work = found(page, local_app)
    user = user_for(local_app)
    fill_labour(page, 'Säilyvä', '1.2')
    page.evaluate('sessionProof()')
    original = page.locator('#autodata-result').text_content()
    local_app.auth_service.revoke_session(local_app.token)
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    with page.expect_response('**/accept') as rejected:
        accept(page)
    assert rejected.value.status == 401
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
    assert_business_locked(page)
    assert page.locator('#autodata-result').text_content() == original
    first = page.writes[-1].post_data
    assert local_app[1].get_work(work['id'])['version'] == 1
    page.locator('#auth-check').click()
    password(page, local_app, user)
    mfa(page, local_app, user)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    size = len(page.writes)
    page.wait_for_timeout(200)
    assert len(page.writes) == size
    retry(page, 'accept')
    accepted(page)
    assert page.writes[-1].post_data == first
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Säilyvä')


def test_t19_identity_change_discards_references_and_dom(page, local_app):
    """T19 A/R: uncertain acceptance is also removed with old identity."""
    found(page, local_app)
    lose_response(page, 'accept')
    accept(page)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    other = grant_session(local_app.auth_service)
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=120)
    local_app.auth_service.revoke_session(local_app.token)
    page.evaluate('loadWorks().catch(() => {})')
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
    page.locator('#auth-check').click()
    password(page, local_app, other.user)
    mfa(page, local_app, other.user)
    assert page.locator('#autodata-panel').text_content() == ''
    assert page.evaluate('state.selected === null')
    open_work(page, 'TEST-AD')
    expect(page.locator(RETRY_ACCEPT)).to_have_count(0)


def test_t20_redacted_card_and_decision(page, local_app):
    """T20 D+A: repository fixture action, no redaction route exists."""
    work = found(page, local_app)
    id = lookup_id(page)
    accept(page)
    accepted(page)
    redact(local_app, work, id)
    page.locator('#autodata-refresh').click()
    expect(page.locator('#autodata-decisions')).to_contain_text('nimiöt poistettu')
    history_open(page, id)
    expect(page.locator('#autodata-result')).to_contain_text('Haun nimiöt ja lähdeteksti on poistettu.')
    assert 'Lähteen teksti:' not in page.locator('#autodata-result').inner_text()
    assert 'Etujarrupalat' not in page.locator('#autodata-result').inner_text()
    assert page.locator(ACCEPT).count() == 0


def test_t21_closed_missing_check_404_and_persistent_close(page, local_app):
    """T21 D+A: old history 404, empty lists, permanently hidden new search."""
    work = found(page, local_app)
    id = lookup_id(page)
    expect(page.locator(f'[data-lookup-id="{id}"]')).to_be_visible()
    close(local_app, work)
    page.locator('#autodata-check').click()
    expect(page.locator('#autodata-check-status')).to_contain_text('Haun tilaa ei löytynyt')
    expect(page.locator(ACCEPT)).to_be_disabled()
    history_open(page, id)
    expect(page.locator('#autodata-status')).to_contain_text('Hakua ei löytynyt')
    expect(page.locator('#autodata-status')).to_be_focused()
    assert page.locator('#autodata-result').text_content() == ''
    page.locator('#autodata-refresh').click()
    expect(page.locator('#autodata-lookups')).to_contain_text('Ei Autodata-hakuja.')
    assert page.locator('#autodata-decisions p').count() == 0
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_contain_text('Autodata-aineisto on suljettu')
    expect(page.locator('#autodata-form')).to_be_hidden()
    seed(local_app[1], 'TEST-OTHER')
    page.evaluate('loadWorks()')
    open_work(page, 'TEST-OTHER')
    open_work(page, 'TEST-AD')
    expect(page.locator('#autodata-form')).to_be_hidden()


def test_t22_external_text_literal_and_wrapped(page, local_app):
    """T22 A: malicious-looking fixture text travels through the real chain."""
    prefix = '<img src=x onerror=alert(1)><script>alert(2)</script>\u202e'
    text = prefix + 'x' * (200 - len(prefix))
    assert len(text) == 200
    local_app.adapter.vehicles = (FakeVehicle(choice(text), (FakeOperation(choice('etujarrupalat'), (
        FakeVariant(choice('Vakio'), '0,8 h'),)),)),)
    page.set_viewport_size({'width': 390, 'height': 844})
    errors, dialogs = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    found(page, local_app)
    assert text in page.locator('#autodata-result').text_content()
    assert page.locator('#autodata-panel img, #autodata-panel script').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert errors == dialogs == []


def test_t23_no_browser_storage_url_logs_and_logout(page, local_app):
    """T23 A: no-store on real responses and confirmed logout clears DOM."""
    console = []
    page.on('console', lambda msg: console.append(msg.text))
    work = found(page, local_app)
    accept(page)
    accepted(page)
    assert page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
    assert page.evaluate('indexedDB.databases()') == []
    assert page.evaluate('caches.keys()') == []
    assert page.evaluate('navigator.serviceWorker.getRegistrations().then(values => values.length)') == 0
    assert page.url == local_app[0] + '/'
    assert not any('Etujarrupalat' in line for line in console)
    assert all(response.headers.get('cache-control') == 'no-store' for response in page.responses)
    with page.expect_response(f"**/api/works/{work['id']}") as response:
        page.evaluate("api('/api/works/' + state.selected.id)")
    assert response.value.headers['cache-control'] == 'no-store'
    page.locator('#logout-button').click()
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
    assert page.locator('#autodata-panel').text_content() == ''


def test_t23_logout_cancels_late_reads_before_confirmed_response(page, local_app):
    """T23 A: real logout held after commit; late real read 401 cannot change epoch."""
    found(page, local_app)
    expect(page.locator('[data-lookup-id]')).to_have_count(1)
    held, logout = [], []
    page.route('**/autodata/*', lambda route: held.append(route))
    page.locator('#autodata-refresh').click()
    wait_for_held(page, held, 1)
    page.route('**/api/auth/logout', lambda route: logout.append((route, route.fetch())), times=1)
    epoch = page.evaluate('CarbitAuth.epoch()')
    page.locator('#logout-button').click()
    wait_for_held(page, logout, 0)
    assert logout[0][1].status == 204
    for route in held:
        response = route.fetch()
        assert response.status == 401
        route.fulfill(response=response)
    page.wait_for_timeout(100)
    assert page.evaluate('CarbitAuth.epoch()') == epoch
    logout[0][0].fulfill(response=logout[0][1])
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
    assert page.locator('#autodata-panel').text_content() == ''


def test_t25_manual_after_acceptance(page, local_app):
    """T25 A; existing manual/auth browser regressions are run separately."""
    work = found(page, local_app)
    accept(page)
    accepted(page)
    update_manual(page)
    saved = local_app[1].get_work(work['id'])
    assert saved['labour_source'] == 'MANUAL_AUTODATA'
    expect(page.locator('#labour-form [name=hours]')).to_have_value('2')


@pytest.mark.parametrize('shape,locked', [
    ('module', False), ('unknown', True), ('missing', True), ('nonjson', True), ('route', True)])
def test_t26_substituted_403_matrix(page, local_app, shape, locked):
    """T26a/d/e + absent code/route C: only exact module code is local."""
    start(page, local_app)

    def deny(route):
        if shape == 'nonjson':
            route.fulfill(status=403, body='not-json')
        else:
            body = {'detail': 'Ei oikeutta.' if shape != 'route' else 'HTTP route not permitted'}
            if shape != 'missing':
                body['code'] = 'PERMISSION_DENIED' if shape == 'module' else 'OTHER_CODE'
            route.fulfill(status=403, json=body)

    page.route('**/autodata/lookups', deny)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    if locked:
        expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'denied')
        assert_business_locked(page)
    else:
        expect(page.locator('#autodata-error')).to_have_text('Ei oikeutta.')
        expect(page.locator('#business-shell')).to_be_visible()
        assert page.locator(RETRY_LOOKUP).count() == 0


@pytest.mark.parametrize('operation', ['autodata', 'manual'])
def test_t26_real_csrf_and_manual_403_regression(page, local_app, operation):
    """T26c/f A with modified request, real gate response, differing UI policy."""
    start(page, local_app)
    pattern = '**/autodata/lookups' if operation == 'autodata' else '**/labour'

    def invalid(route):
        headers = {**route.request.headers, 'x-csrf-token': 'invalid'}
        route.continue_(headers=headers)

    page.route(pattern, invalid)
    if operation == 'autodata':
        page.locator(QUERY).fill('etujarrupalat')
        page.locator(SEARCH).click()
        expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'denied')
    else:
        fill_labour(page)
        page.locator('#labour-form button[type=submit]').click()
        expect(page.locator('#labour-error')).to_have_text('Invalid CSRF security context')
        expect(page.locator('#business-shell')).to_be_visible()


def test_t26_real_role_downgrade_locks(page, local_app):
    """T26b A: real server permission change, no substituted response."""
    start(page, local_app)
    page.evaluate('sessionProof()')
    user = user_for(local_app)
    with local_app.auth_service.repository._connect() as connection:
        connection.execute("UPDATE auth_users SET role='IT_ADMIN' WHERE user_id=?", (str(user.user_id),))
        connection.commit()
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'denied')


def test_t27_selection_invalid_choices(page, local_app):
    """T27 C then A: substituted successful result, new real choice command."""
    local_app.adapter.vehicles = FLEET
    start(page, local_app)

    def invalid(route):
        if route.request.method != 'POST':
            route.fallback()
            return
        # Fully substituted response, not counted as a real server write.
        route.fulfill(status=201, json={'replayed': False, 'lookup': {
            'lookup_id': str(uuid4()), 'inquiry_id': page.evaluate('state.selected.id'),
            'outcome': 'SELECTION_INVALID', 'result': {'level': 'VEHICLE', 'candidates': [
                vehicle.choice.model_dump(mode='json') for vehicle in FLEET]}}})

    page.route('**/autodata/lookups', invalid, times=1)
    search(page, 'jarrupalat')
    expect(page.locator('#autodata-result')).to_contain_text('Aiempi valinta ei kelpaa')
    page.locator('#autodata-result input[type=radio]').first.check()
    page.locator('#autodata-continue').click()
    expect(page.locator('#autodata-result legend')).to_have_text('Valitse työ')
    assert page.writes[0].post_data_json['request_id'] != page.writes[-1].post_data_json['request_id']


def test_t28_acceptability_paginates_to_old_lookup(page, local_app):
    """T28 D+A: service creates 22 rows; browser reads genuine second page."""
    work = seed(local_app[1], 'TEST-OLD')
    ids = []
    for _ in range(22):
        response = local_app.app.state.autodata_lookups.lookup(ctx(local_app), inquiry_id=work['id'], command=LookupCommand(
            request_id=str(uuid4()), expected_version=1, vehicle_facts={}, work_query='etujarrupalat'))
        ids.append(response['lookup']['lookup_id'])
    page.goto(local_app[0], wait_until='networkidle')
    open_work(page, 'TEST-OLD')
    page.get_by_role('button', name='Näytä vanhemmat haut').click()
    expect(page.locator('[data-lookup-id]')).to_have_count(22)
    reads = []
    page.on('request', lambda req: reads.append(req.url) if '/autodata/lookups' in req.url and req.method == 'GET' else None)
    history_open(page, ids[0])
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert any('?before=' in url for url in reads)
    assert len([url for url in reads if '/lookups/' not in url]) == 2
    accept(page)
    accepted(page)


def test_t29_pending_check_blocks_checked_confirmation(page, local_app):
    """T29 A supplement: even checked confirmation waits for fresh list result."""
    found(page, local_app)
    held = []
    page.route('**/autodata/lookups', lambda route: held.append((route, route.fetch())), times=1)
    page.locator('#autodata-check').click()
    wait_for_held(page, held, 0)
    page.locator(CONFIRM).check()
    expect(page.locator(ACCEPT)).to_be_disabled()
    assert len(page.writes) == 1
    held[0][0].fulfill(response=held[0][1])
    expect(page.locator(ACCEPT)).to_be_enabled()


@pytest.mark.parametrize('failure', ['missing', 'abort', 'invalid', 'cursor', 'page-limit'])
def test_t29_acceptability_fail_closed(page, local_app, failure):
    """T29 C: no historical status reused, explicit recheck only."""
    found(page, local_app)
    checks = []

    def corrupt(route):
        checks.append(route.request.url)
        if failure == 'abort':
            route.abort()
        elif failure == 'invalid':
            route.fulfill(json={'items': {}, 'next': None, 'autodata_enabled': True})
        else:
            cursor = None if failure == 'missing' else 'same' if failure == 'cursor' else str(len(checks))
            route.fulfill(json={'items': [], 'next': cursor, 'autodata_enabled': True})

    page.route('**/autodata/lookups*', corrupt)
    page.locator('#autodata-check').click()
    expected = 'Haun tilaa ei löytynyt' if failure == 'missing' else 'Hyväksyttävyyttä ei voitu tarkistaa'
    expect(page.locator('#autodata-check-status')).to_contain_text(expected)
    page.locator(CONFIRM).check()
    expect(page.locator(ACCEPT)).to_be_disabled()
    count = len(checks)
    page.wait_for_timeout(200)
    assert len(checks) == count
    assert count == (2 if failure == 'cursor' else 20 if failure == 'page-limit' else 1)
    page.unroute('**/autodata/lookups*', corrupt)
    page.locator('#autodata-check').click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    expect(page.locator(ACCEPT)).to_be_enabled()


@pytest.mark.parametrize('kind', ['lookup', 'accept'])
@pytest.mark.parametrize('damage', ['truncated', 'nonjson', 'missing', 'body-read'])
def test_t30_t31_damaged_success_body_replays_once(page, local_app, kind, damage):
    """T30/T31 B: real stored write, broken JSON/shape/read, same receipt."""
    work = start(page, local_app) if kind == 'lookup' else found(page, local_app)
    pattern = '**/autodata/lookups' if kind == 'lookup' else '**/autodata/lookups/*/accept'
    written = []

    def damage_response(route):
        if route.request.method != 'POST':
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        written.append((route.request.post_data, json.loads(json.dumps(body))))
        if damage == 'body-read':
            route.fulfill(response=response)
        elif damage == 'missing':
            if kind == 'lookup':
                del body['lookup']['lookup_id']
            else:
                del body['decision']['decision_id']
            route.fulfill(status=response.status, json=body)
        else:
            route.fulfill(status=response.status, content_type='application/json',
                          body='{"broken":' if damage == 'truncated' else 'not-json')

    if damage == 'body-read':
        page.evaluate("""kind => {
          const original = window.fetch;
          window.fetch = async (...args) => {
            const response = await original(...args);
            if (args[1]?.method === 'POST' && String(args[0]).includes('/autodata/')
                && (kind === 'lookup' || String(args[0]).endsWith('/accept'))) {
              window.fetch = original;
              response.json = async () => { throw new TypeError('Synthetic body read interrupted'); };
            }
            return response;
          };
        }""", kind)
    page.route(pattern, damage_response, times=1)
    if kind == 'lookup':
        page.locator(QUERY).fill('etujarrupalat')
        page.locator(SEARCH).click()
    else:
        accept(page)
    expect(page.locator(RETRY_LOOKUP if kind == 'lookup' else RETRY_ACCEPT)).to_be_enabled()
    expect(page.locator('#autodata-error')).to_contain_text('Vastausta ei voitu vahvistaa')
    with page.expect_response(pattern) as response:
        retry(page, kind)
    body = response.value.json()
    assert body['replayed'] is True and page.writes[-1].post_data == written[0][0]
    assert local_app.adapter.calls == 1
    if kind == 'accept':
        accepted(page)
        assert body['decision'] == written[0][1]['decision']
        assert len(records(local_app, work)[1]['items']) == 1


def test_t31_epoch_real_write_old_response_ignored_then_same_receipt(page, local_app):
    """T31-epoch B: write -> held 200 -> lock/epoch -> reject -> same receipt."""
    work = found(page, local_app)
    fill_labour(page, 'Epoch-luonnos', '1.5')
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route('**/autodata/lookups/*/accept', hold, times=1)
    before = {id: page.locator('#' + id).text_content() for id in ('price-summary', 'event-list', 'autodata-result')}
    accept(page)
    wait_for_held(page, held, 0)
    assert local_app[1].get_work(work['id'])['version'] == 2
    route, response = held[0]
    first_body, receipt = route.request.post_data, response.json()['decision']
    old_epoch = page.evaluate('CarbitAuth.epoch()')
    page.evaluate('CarbitAuth.lock("uncertain", "Synteettinen epoch-testi")')
    assert page.evaluate('CarbitAuth.epoch()') > old_epoch
    route.fulfill(response=response)
    page.wait_for_timeout(200)
    assert_business_locked(page)
    for id, text in before.items():
        assert page.locator('#' + id).text_content() == text
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '1')
    assert len(page.writes) == 2
    page.locator('#auth-check').click()
    expect(page.locator('#business-shell')).to_be_visible()
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    assert len(page.writes) == 2
    with page.expect_response('**/accept') as response:
        retry(page, 'accept')
    body = response.value.json()
    assert body['replayed'] is True and body['decision'] == receipt
    assert page.writes[-1].post_data == first_body
    accepted(page)
    assert len(records(local_app, work)[1]['items']) == 1
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '2')


def test_t32_projection_500_after_stored_lookup(page, local_app, monkeypatch):
    """T32 B: server response projection fails after actual stored lookup."""
    start(page, local_app)
    original = http_autodata.LookupResponse.model_validate
    count = []

    def fail_once(data):
        if not count:
            count.append(True)
            raise ValueError('Synthetic projection failure')
        return original(data)

    monkeypatch.setattr(http_autodata.LookupResponse, 'model_validate', fail_once)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator(RETRY_LOOKUP)).to_be_enabled()
    assert local_app.adapter.calls == 1
    with page.expect_response('**/autodata/lookups') as response:
        retry(page, 'lookup')
    assert response.value.json()['replayed'] is True
    assert local_app.adapter.calls == 1


def test_t33_local_render_error_is_not_network_uncertainty(page, local_app):
    """T33 A: throw once in local rendering after confirmed server result."""
    start(page, local_app)
    page.evaluate("""() => {
      const original = Intl.DateTimeFormat;
      Intl.DateTimeFormat = function(...args) {
        Intl.DateTimeFormat = original;
        throw new Error('Synthetic local rendering failure');
      };
    }""")
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_contain_text('Näkymän päivitys epäonnistui')
    assert page.locator(RETRY_LOOKUP).count() == 0
    first = page.writes[-1].post_data_json['request_id']
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert page.writes[-1].post_data_json['request_id'] != first


def test_t34_real_dataset_rebind_then_manual_put_200(page, local_app):
    """T34 A: actual DOM dataset string 1 -> 2, then real PUT(version=2)."""
    work = found(page, local_app)
    assert page.locator('#labour-form').evaluate('f => f.dataset.version') == '1'
    fill_labour(page, 'Oma manuaalinen', '1.5')
    page.locator('#labour-form [name=autodata_reference]').fill('Oma viite')
    accept(page)
    accepted(page)
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '2')
    expect(page.locator('#labour-notice')).to_contain_text('Lähettämättömät arvosi säilyivät')
    page.locator('#autodata-refresh').click()
    expect(page.locator('#autodata-decisions')).to_contain_text('hyväksyjä')
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Oma manuaalinen')
    expect(page.locator('#labour-form [name=autodata_reference]')).to_have_value('Oma viite')
    with page.expect_response('**/labour') as response:
        page.locator('#labour-form button[type=submit]').click()
    assert response.value.status == 200
    assert response.value.request.post_data_json['version'] == 2
    assert response.value.json()['labour_source'] == 'MANUAL_AUTODATA'
    assert local_app[1].get_work(work['id'])['version'] == 3


@pytest.mark.parametrize('dataset', ['', 'abc', '01', '1.5', '9007199254740992', None])
def test_t34_invalid_dataset_never_rebinds(page, local_app, dataset):
    """T34 A with DOM fault: blank, missing, invalid, unsafe integer rejected."""
    found(page, local_app)
    fill_labour(page)
    page.locator('#labour-form').evaluate("(form, value) => { if (value === null) delete form.dataset.version; else form.dataset.version=value; }", dataset)
    accept(page)
    accepted(page)
    assert page.locator('#labour-form').get_attribute('data-version') == dataset
    expect(page.locator('#labour-notice')).to_contain_text('Tallennusta ei lähetetä')
    puts = []
    page.on('request', lambda req: puts.append(req) if req.method == 'PUT' else None)
    page.locator('#labour-form button[type=submit]').click()
    assert puts == []


def test_t34_receipt_after_external_version_preserves_conflict(page, local_app):
    """T34 B+A: result version 2 vs work version 3 must not rebind draft."""
    work = found(page, local_app)
    fill_labour(page, 'Pidä tämä luonnos', '1.25')
    lose_response(page, 'accept')
    accept(page)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    other = second(page, local_app, 'TEST-AD')
    update_manual(other, '3')
    retry(page, 'accept')
    expect(page.locator('#autodata-status')).to_contain_text('Hyväksyntä oli jo tallennettu')
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '1')
    expect(page.locator('#labour-notice')).to_contain_text('nyt versiossa 3')
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Pidä tämä luonnos')
    page.locator('#labour-reset').click()
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '3')
    update_manual(page)
    assert local_app[1].get_work(work['id'])['version'] == 4


def test_t34_reset_after_autodata_clears_manual_values(page, local_app):
    """T34 A: explicit reset uses AUTODATA source to empty all manual fields."""
    found(page, local_app)
    fill_labour(page, 'Palautettava luonnos', '1.5')
    page.locator('#labour-form [name=autodata_reference]').fill('Oma viite')
    accept(page)
    accepted(page)
    page.locator('#labour-reset').click()
    for field in ('hours', 'work_description', 'autodata_reference'):
        expect(page.locator(f'#labour-form [name={field}]')).to_have_value('')
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '2')
    expect(page.locator('#labour-notice')).to_contain_text('korvaa nykyisen Autodata-ajan')


@pytest.mark.parametrize('switch', ['inquiry', 'card'])
def test_t35_late_card_is_bound_to_visible_target(page, local_app, switch):
    """T35a/b A: real GET responses held and delivered in reverse order."""
    work = found(page, local_app)
    id_a = lookup_id(page)
    local_app.adapter.vehicles = (FakeVehicle(choice('Toinen ajoneuvo'), (
        FakeOperation(choice('etujarrupalat'), (VARIANTS[1],)),)),)
    search(page)
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    id_b = lookup_id(page)
    other_work = seed(local_app[1], 'TEST-BIND')
    page.evaluate('loadWorks()')
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route(f'**/autodata/lookups/{id_a}', hold, times=1)
    history_open(page, id_a)
    wait_for_held(page, held, 0)
    if switch == 'inquiry':
        open_work(page, 'TEST-BIND')
    else:
        history_open(page, id_b)
        expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    before = page.locator('#autodata-result').text_content()
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    assert page.locator('#autodata-result').text_content() == before
    assert page.evaluate('state.selected.id') == (other_work['id'] if switch == 'inquiry' else work['id'])


def test_t35_late_check_does_not_change_new_card(page, local_app):
    """T35c A: A's real check arrives after B's check; B stays acceptable."""
    found(page, local_app)
    first = lookup_id(page)
    search(page)
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    second_id = lookup_id(page)
    held = []

    def hold(route):
        repo = local_app.app.state.autodata_repository
        clock = repo.clock
        repo.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
        try:
            response = route.fetch()
            assert response.json()['items'][0]['acceptability'] == 'EXPIRED'
            held.append((route, response))
        finally:
            repo.clock = clock

    page.route('**/autodata/lookups', hold, times=1)
    history_open(page, first)
    wait_for_held(page, held, 0)
    history_open(page, second_id)
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    page.locator(CONFIRM).check()
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    expect(page.locator(ACCEPT)).to_be_enabled()
    assert page.locator(CONFIRM).is_checked()


def test_t35_late_reload_cannot_roll_back_newer_version(page, local_app):
    """T35 A supplement: two genuine explicit reloads delivered v3 then v2."""
    work = found(page, local_app)
    other = second(page, local_app, 'TEST-AD')
    update_manual(other)
    page.locator('#autodata-check').click()
    expect(page.locator('#autodata-reload')).to_be_visible()
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route(f"**/api/works/{work['id']}", hold)
    page.locator('#autodata-reload').click()
    wait_for_held(page, held, 0)
    update_manual(other, '3')
    page.locator('#autodata-reload').click()
    wait_for_held(page, held, 1)
    assert held[0][1].json()['version'] == 2 and held[1][1].json()['version'] == 3
    held[1][0].fulfill(response=held[1][1])
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '3')
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    assert page.evaluate('state.selected.version') == 3


def test_t35_late_accept_different_inquiry_clears_command(page, local_app):
    """T35d A: confirmed write is consumed but never renders into Y."""
    work = found(page, local_app)
    seed(local_app[1], 'TEST-Y')
    page.evaluate('loadWorks()')
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route('**/accept', hold, times=1)
    accept(page)
    wait_for_held(page, held, 0)
    epoch = page.evaluate('CarbitAuth.epoch()')
    open_work(page, 'TEST-Y')
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    assert page.evaluate('CarbitAuth.epoch()') == epoch
    assert page.evaluate('state.selected.version') == 1
    assert page.locator('#price-summary').is_hidden()
    open_work(page, 'TEST-AD')
    accepted(page)
    assert page.locator(RETRY_ACCEPT).count() == 0
    assert len(records(local_app, work)[1]['items']) == 1


@pytest.mark.parametrize('after', ['disabled', 'redacted', 'closed', 'closed-reopen'])
def test_t36_receipt_replay_without_active_lookup(page, local_app, after):
    """T36 B+D: real lost acceptance, fixture state change, real same receipt."""
    work = found(page, local_app)
    lost = lose_response(page, 'accept')
    accept(page)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    body, first = lost[0]
    id = first['decision']['lookup_id']
    if after == 'disabled':
        rewire(local_app, DISABLED)
    elif after == 'redacted':
        redact(local_app, work, id)
    else:
        close(local_app, work)
    if after == 'closed-reopen':
        seed(local_app[1], 'TEST-AWAY')
        page.evaluate('loadWorks()')
        open_work(page, 'TEST-AWAY')
        open_work(page, 'TEST-AD')
        expect(page.locator('#autodata-status')).to_contain_text('Hakua ei löytynyt')
        assert page.locator('#autodata-result').text_content() == ''
    else:
        page.locator('#autodata-refresh').click()
    with page.expect_response('**/accept') as response:
        retry(page, 'accept')
    result = response.value.json()
    assert result['replayed'] is True and result['decision'] == first['decision']
    assert page.writes[-1].post_data == body
    expect(page.locator('#autodata-status')).to_contain_text('Hyväksyntä oli jo tallennettu')
    expect(page.locator('#price-summary')).to_contain_text('72,00')
    if after.startswith('closed'):
        expect(page.locator('#autodata-lookups')).to_contain_text('Ei Autodata-hakuja.')
        assert records(local_app, work)[1]['items'] == []
    else:
        assert len(records(local_app, work)[1]['items']) == 1


def test_t37_real_retry_after_expiry_never_resends(page, local_app, monkeypatch):
    """T37a A: real short quota window, timer only enables a button."""
    monkeypatch.setattr(autodata_service, 'PROCESS_LOOKUP_QUOTA', LookupQuota(limit=1, window_seconds=3))
    found(page, local_app)
    page.locator(SEARCH).click()
    expect(page.locator(RETRY_LOOKUP)).to_be_disabled()
    count = len(page.writes)
    expect(page.locator(RETRY_LOOKUP)).to_be_enabled(timeout=5000)
    assert len(page.writes) == count and local_app.adapter.calls == 1
    retry(page, 'lookup')
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    assert local_app.adapter.calls == 2


@pytest.mark.parametrize('header', ['abc', '0', '-5', '999999', '1.5', None])
def test_t37_bad_retry_after_no_time_or_auto_send(page, local_app, header):
    """T37b C: invalid headers do not schedule writes or hide retry."""
    start(page, local_app)

    def rate_limit(route):
        route.fulfill(status=429, headers={} if header is None else {'Retry-After': header},
                      json={'code': 'LOOKUP_RATE_LIMITED', 'detail': 'Kiintiö täynnä.'})

    page.route('**/autodata/lookups', rate_limit, times=1)
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator(RETRY_LOOKUP)).to_be_enabled()
    assert 'Uusi haku mahdollinen noin klo' not in page.locator('#autodata-retries').inner_text()
    count = len(page.writes)
    page.wait_for_timeout(200)
    assert len(page.writes) == count


def test_t38_focus_error_card_and_disappearing_button(page, local_app):
    """T38 A: error alert, new heading, stale accept removed -> status focus."""
    found(page, local_app)
    page.locator(QUERY).fill('TEST-AD')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_be_focused()
    search(page)
    expect(page.locator('#autodata-result h4')).to_be_focused()
    other = second(page, local_app, 'TEST-AD')
    update_manual(other)
    page.locator(CONFIRM).check()
    page.locator(ACCEPT).click()
    expect(page.locator('#autodata-check-status')).to_contain_text('muuttunut haun jälkeen')
    assert page.locator(ACCEPT).count() == 0
    assert page.evaluate("['autodata-error','autodata-status','autodata-title'].includes(document.activeElement.id)")


def test_r2_p21_a_logout_blocks_new_history_and_keeps_command_until_204(page, local_app):
    """P2-1 A / T23 A+B: real lost acceptance, real logout held after commit."""
    found(page, local_app)
    user = user_for(local_app)
    lose_response(page, 'accept')
    accept(page)
    expect(page.locator(RETRY_ACCEPT)).to_be_enabled()
    logout, operations = [], []
    page.route('**/api/auth/logout', lambda route: logout.append((route, route.fetch())), times=1)
    page.on('request', lambda req: operations.append(req) if '/autodata/' in req.url else None)
    epoch = page.evaluate('CarbitAuth.epoch()')
    page.locator('#logout-button').click()
    wait_for_held(page, logout, 0)
    assert logout[0][1].status == 204
    expect(page.locator('#autodata-refresh')).to_be_disabled()
    assert page.locator('#autodata-panel').evaluate('panel => panel.inert')
    assert page.locator(RETRY_ACCEPT).count() == 1
    page.locator('#autodata-refresh').evaluate("node => node.dispatchEvent(new MouseEvent('click'))")
    page.locator(RETRY_ACCEPT).evaluate("node => node.dispatchEvent(new MouseEvent('click'))")
    page.locator('#autodata-form').evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
    page.wait_for_timeout(150)
    assert operations == [] and page.evaluate('CarbitAuth.epoch()') == epoch
    logout[0][0].fulfill(response=logout[0][1])
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
    assert page.locator('#autodata-panel').text_content() == ''
    assert page.evaluate('state.selected === null')
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    page.locator('#auth-check').click()
    password(page, local_app, user)
    mfa(page, local_app, user)
    open_work(page, 'TEST-AD')
    expect(page.locator(RETRY_ACCEPT)).to_have_count(0)


@pytest.mark.parametrize('kind', ['lookup', 'accept'])
@pytest.mark.parametrize('logout_result', ['confirmed', 'lost', 'other-user'])
def test_r2_p21_b_late_post_401_cannot_invalidate_logout(page, local_app, kind, logout_result):
    """P2-1 B / T18,T23 A: genuine POST 401 after real logout, before held 204."""
    work = found(page, local_app)
    user = user_for(local_app)
    held, logout = [], []
    pattern = '**/autodata/lookups' if kind == 'lookup' else '**/autodata/lookups/*/accept'
    def hold_post(route):
        if route.request.method == 'POST':
            held.append(route)
        else:
            route.fallback()

    page.route(pattern, hold_post)
    if kind == 'lookup':
        page.locator(QUERY).fill('etujarrupalat')
        page.locator(SEARCH).click()
    else:
        accept(page)
    wait_for_held(page, held, 0)
    assert held[0].request.method == 'POST'
    body = held[0].request.post_data
    page.route('**/api/auth/logout', lambda route: logout.append((route, route.fetch())), times=1)
    epoch = page.evaluate('CarbitAuth.epoch()')
    page.locator('#logout-button').click()
    wait_for_held(page, logout, 0)
    assert logout[0][1].status == 204
    assert page.evaluate('CarbitAuth.epoch()') == epoch
    response = held[0].fetch()
    assert response.status == 401 and response.json()['detail'] == 'Authentication required'
    with page.expect_response(lambda response: response.request.method == 'POST'
                              and response.url == held[0].request.url) as rejected:
        held[0].fulfill(response=response)
    assert rejected.value.status == 401
    page.unroute(pattern, hold_post)
    page.wait_for_timeout(100)
    assert page.evaluate('CarbitAuth.epoch()') == epoch, page.evaluate("({state:document.querySelector('#auth-panel').dataset.state,message:document.querySelector('#auth-message').textContent})")
    selector = RETRY_LOOKUP if kind == 'lookup' else RETRY_ACCEPT
    expect(page.locator(selector)).to_have_count(1)
    assert local_app[1].get_work(work['id'])['version'] == 1
    if logout_result == 'confirmed':
        logout[0][0].fulfill(response=logout[0][1])
        expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'expired')
        assert page.locator('#autodata-panel').text_content() == ''
        assert page.evaluate('state.selected === null')
        return
    logout[0][0].abort()
    expect(page.locator('#auth-panel')).to_have_attribute('data-state', 'uncertain')
    assert page.locator(selector).count() == 1  # Preserved in the hidden/inert DOM.
    local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    if logout_result == 'other-user':
        user = grant_session(local_app.auth_service).user
        local_app.auth_service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=120)
    page.locator('#auth-check').click()
    password(page, local_app, user)
    mfa(page, local_app, user)
    count = len(page.writes)
    page.wait_for_timeout(100)
    assert len(page.writes) == count  # No automatic retransmission on recovery.
    if logout_result == 'other-user':
        assert page.locator('#autodata-panel').text_content() == ''
        open_work(page, 'TEST-AD')
        expect(page.locator(selector)).to_have_count(0)
    else:
        expect(page.locator(selector)).to_be_enabled()
        with page.expect_response(lambda response: response.request.method == 'POST'
                                  and response.url == held[0].request.url) as replay:
            retry(page, kind)
        assert replay.value.status in (200, 201)
        assert replay.value.request.post_data == body
        if kind == 'accept':
            accepted(page)
        else:
            expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')


CONTEXT_FACTS = {'vin': 'FAKE0000000000001', 'make': 'Synteettinen merkki', 'model': 'Synteettinen malli',
                 'model_year': 2020, 'engine_code': 'TEST', 'fuel': 'Testipolttoaine',
                 'power_kw': 100, 'displacement_cc': 1600}


def fill_search_facts(page, facts):
    for name, value in facts.items():
        page.locator(f'#autodata-form [name="{name}"]').fill('' if value is None else str(value))


def ambiguous_with_context(page, env, *, lose=False):
    env.adapter.vehicles = FLEET
    work = start(page, env)
    fill_search_facts(page, CONTEXT_FACTS)
    lost = lose_response(page, 'lookup') if lose else None
    page.locator(QUERY).fill('jarrupalat')
    page.locator(SEARCH).click()
    if lose:
        expect(page.locator(RETRY_LOOKUP)).to_be_enabled()
        id = lost[0][1]['lookup']['lookup_id']
    else:
        expect(page.locator('#autodata-result legend')).to_have_text('Valitse ajoneuvo')
        expect(page.locator('[data-lookup-id]')).to_have_count(1)
        id = lookup_id(page)
    original = page.writes[0].post_data_json
    assert original['vehicle_facts'] == CONTEXT_FACTS
    return work, id, original


def continue_first_choice(page):
    page.locator('#autodata-result input[type=radio]').first.check()
    page.locator('#autodata-continue').click()


def test_r2_p22_a_history_keeps_known_context_and_reload_is_readonly(page, local_app):
    """P2-2 A / T4,T27 A: known memory context; HTTP-only history cannot reconstruct it."""
    work, id, original = ambiguous_with_context(page, local_app)
    seed(local_app[1], 'TEST-AWAY')
    page.evaluate('loadWorks()')
    open_work(page, 'TEST-AWAY')
    open_work(page, 'TEST-AD')
    history_open(page, id)
    for legend in ('Valitse ajoneuvo', 'Valitse työ', 'Valitse variantti'):
        expect(page.locator('#autodata-result legend')).to_have_text(legend)
        continue_first_choice(page)
    expect(page.locator('#autodata-check-status')).to_have_text('Hyväksyttävissä')
    for request in page.writes[1:]:
        sent = request.post_data_json
        assert sent['work_query'] == original['work_query'] and sent['vehicle_facts'] == original['vehicle_facts']
        assert sent['request_id'] != original['request_id']
    assert page.writes[-1].post_data_json['vehicle_selection'] is not None
    assert page.writes[-1].post_data_json['operation_selection'] is not None
    page.reload(wait_until='networkidle')
    open_work(page, 'TEST-AD')
    history_open(page, id)
    expect(page.locator('#autodata-result')).to_contain_text('Tulos on vain lukua varten')
    expect(page.locator('#autodata-continue')).to_have_count(0)
    for radio in page.locator('#autodata-result input[type=radio]').all():
        expect(radio).to_be_disabled()
    count = len(page.writes)
    page.wait_for_timeout(100)
    assert len(page.writes) == count
    page.get_by_role('button', name='Tee uusi haku', exact=True).click()
    expect(page.locator(QUERY)).to_be_focused()
    assert local_app[1].get_work(work['id'])['version'] == 1


def test_r2_p22_b_lost_lookup_replay_restores_original_context(page, local_app):
    """P2-2 B / T6 A+B: same bytes replay after inquiry switch, then original facts."""
    _, _, original = ambiguous_with_context(page, local_app, lose=True)
    first = page.writes[0].post_data
    seed(local_app[1], 'TEST-AWAY')
    page.evaluate('loadWorks()')
    open_work(page, 'TEST-AWAY')
    open_work(page, 'TEST-AD')
    expect(page.locator(QUERY)).to_have_value('')
    with page.expect_response(lambda response: response.request.method == 'POST'
                              and response.url.endswith('/autodata/lookups')) as replay:
        retry(page, 'lookup')
    assert replay.value.json()['replayed'] is True and replay.value.request.post_data == first
    expect(page.locator('#autodata-result legend')).to_have_text('Valitse ajoneuvo')
    continue_first_choice(page)
    expect(page.locator('#autodata-result legend')).to_have_text('Valitse työ')
    sent = page.writes[-1].post_data_json
    assert sent['work_query'] == original['work_query'] and sent['vehicle_facts'] == original['vehicle_facts']
    assert sent['request_id'] != original['request_id'] and local_app.adapter.calls == 2


def test_r2_p22_c_current_form_cannot_replace_card_context(page, local_app):
    """P2-2 C / T4 A: all changed visible facts are excluded from an earlier card's continuation."""
    _, id, original = ambiguous_with_context(page, local_app)
    changed = {**CONTEXT_FACTS, 'vin': None, 'make': 'Toinen merkki', 'model': 'Toinen malli',
               'model_year': 2025, 'engine_code': 'OTHER', 'fuel': 'Toinen', 'power_kw': 222, 'displacement_cc': 2222}
    fill_search_facts(page, changed)
    page.locator(QUERY).fill('ei-osumia')
    history_open(page, id)
    continue_first_choice(page)
    expect(page.locator('#autodata-result legend')).to_have_text('Valitse työ')
    sent = page.writes[-1].post_data_json
    assert sent['work_query'] == original['work_query'] == 'jarrupalat'
    assert sent['vehicle_facts'] == original['vehicle_facts'] == CONTEXT_FACTS
    assert sent['vehicle_facts'] != changed
    expect(page.locator(QUERY)).to_have_value('ei-osumia')


def test_r2_p23_old_manual_put_cannot_roll_back_refreshed_work_or_dirty_form(page, local_app):
    """P2-3 / T34,T35 A: held genuine v2 PUT, other staff v3, explicit GET, late v2."""
    work = start(page, local_app)
    held = []
    page.route('**/labour', lambda route: held.append((route, route.fetch())), times=1)
    fill_labour(page, 'Ensimmäinen tallennus', '1')
    page.locator('#labour-form button[type=submit]').click()
    wait_for_held(page, held, 0)
    assert held[0][1].json()['version'] == 2
    staff = page.new_staff_page()
    staff.goto(local_app[0], wait_until='networkidle')
    open_work(staff, 'TEST-AD')
    update_manual(staff, '3')
    assert str(staff.test_user.user_id) != str(user_for(local_app).user_id)
    current = local_app[1].get_work(work['id'])
    assert current['version'] == 3 and current['events'][0]['actor'] == str(staff.test_user.user_id)
    fill_labour(page, 'Nykyinen lähettämätön luonnos', '4.25')
    page.locator('#labour-form [name=autodata_reference]').fill('Nykyinen viite')
    page.locator(QUERY).fill('etujarrupalat')
    page.locator(SEARCH).click()
    expect(page.locator('#autodata-error')).to_contain_text('Tiedustelu on muuttunut toisaalla')
    page.locator('#autodata-reload').click()
    expect(page.locator('#price-summary')).to_contain_text('270,00')
    assert page.evaluate('state.selected.version') == 3
    before = page.evaluate('JSON.stringify(state.selected)')
    events = page.locator('#event-list').text_content()
    held[0][0].fulfill(response=held[0][1])
    expect(page.locator('#labour-form button[type=submit]')).to_be_enabled()
    assert page.evaluate('JSON.stringify(state.selected)') == before
    assert page.locator('#event-list').text_content() == events
    expect(page.locator('#price-summary')).to_contain_text('270,00')
    expect(page.locator('#labour-form')).to_have_attribute('data-dirty-work', work['id'])
    expect(page.locator('#labour-form')).to_have_attribute('data-version', '1')
    expect(page.locator('#labour-form [name=hours]')).to_have_value('4.25')
    expect(page.locator('#labour-form [name=work_description]')).to_have_value('Nykyinen lähettämätön luonnos')
    expect(page.locator('#labour-form [name=autodata_reference]')).to_have_value('Nykyinen viite')
    assert local_app[1].get_work(work['id'])['version'] == 3


@pytest.mark.parametrize('times', [None, 1])
def test_r2_p3_cleanup_settles_held_routes_without_waiting_for_test_release(page, local_app, times):
    """P3 A: simulate failure leaving a live/expired handler held; public cleanup is bounded."""
    start(page, local_app)
    held = []
    page.route('**/api/works/*', lambda route: held.append(route), times=times)
    page.evaluate("""() => {
      window.cleanupResult='pending';
      fetch('/api/works/' + state.selected.id).then(() => window.cleanupResult='response')
        .catch(() => window.cleanupResult='aborted');
    }""")
    wait_for_held(page, held, 0)
    assert len(page.route_ledger.pending) == 1
    started = time.monotonic()
    with pytest.raises(AssertionError, match='synthetic failed test'):
        try:
            raise AssertionError('synthetic failed test')
        finally:
            page.route_ledger.drain()
    assert time.monotonic() - started < 10
    assert not page.route_ledger.pending and not page.route_ledger.running
    page.wait_for_function("window.cleanupResult === 'aborted'")


def test_r2_p3_cleanup_completes_client_aborted_deferred_route(page, local_app):
    """P3 A: canceled Chromium request and a deferred SDK route are both settled."""
    start(page, local_app)
    held = []
    page.route('**/api/works/*', lambda route: held.append(route), times=1)
    page.evaluate("""() => {
      window.cleanupController = new AbortController(); window.cleanupResult = 'pending';
      fetch('/api/works/' + state.selected.id, {signal: cleanupController.signal})
        .then(() => window.cleanupResult='response').catch(() => window.cleanupResult='aborted');
    }""")
    wait_for_held(page, held, 0)
    page.evaluate('cleanupController.abort()')
    page.wait_for_function("cleanupResult === 'aborted'")
    page.route_ledger.drain()
    assert not page.route_ledger.pending and not page.route_ledger.running
