"""Static Web 4D preview; reuse the existing isolated Chromium/app fixtures."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect

from test_webapp_browser import browser, local_app, page  # noqa: F401
from webapp.main import create_app


DEMO_PATH = "/static/labour-review-demo.html"


def open_demo(page, local_app):
    url, _ = local_app
    page.goto(url + DEMO_PATH)
    expect(page.locator("#original-work-rows tr")).to_have_count(2)


def adjust(page, hours, reason="Keksitty mekaanikon perustelu"):
    page.get_by_role("radio", name="Perusteltu ajanmuutos").check()
    page.locator("#preview-final-hours").fill(hours)
    page.locator("#preview-reason").fill(reason)
    page.get_by_role("button", name="Näytä esikatselu").click()


def assert_money(page, field, value):
    expect(page.locator(field)).to_have_text(re.compile(re.escape(value) + r"\s€"))


def test_original_works_sum_and_multi_work_review_are_read_only(page, local_app):
    open_demo(page, local_app)
    rows = page.locator("#original-work-rows")
    expect(rows).to_contain_text("Etuiskunvaimentimien vaihto, molemmat puolet")
    expect(rows).to_contain_text("Etujarrujen vaihto")
    expect(rows).to_contain_text("DEMO-AD-SHOCK-FRONT-PAIR")
    expect(rows).to_contain_text("DEMO-AD-BRAKE-FRONT")
    expect(rows).to_contain_text("2,55 h")
    expect(rows).to_contain_text("1,60 h")
    expect(page.locator("#original-hours-total")).to_have_text("4,15 h")
    expect(page.locator("#review-requirement")).to_contain_text("vaatii ihmisen tarkistuksen")
    expect(rows.locator("input, textarea, [contenteditable=true]")).to_have_count(0)
    expect(page.locator(".demo-badge")).to_have_text("DEMO / ESIKATSELU")
    expect(page.locator("#demo-notice")).to_contain_text("kaikki työ- ja henkilötiedot ovat keksittyjä")


def test_unchanged_choice_previews_original_sum_not_current_adjusted_decision(page, local_app):
    open_demo(page, local_app)
    expect(page.locator("#current-final-hours")).to_have_text("3,60 h")
    page.get_by_role("button", name="Näytä esikatselu").click()
    expect(page.locator("#preview-choice")).to_have_text("Esikatselu: hyväksyntä sellaisenaan · Ei tallennettu")
    expect(page.locator("#preview-hours")).to_have_text("4,15 h")
    assert_money(page, "#preview-gross", "373,50")
    assert_money(page, "#preview-net", "297,61")
    assert_money(page, "#preview-vat", "75,89")
    expect(page.locator("#current-final-hours")).to_have_text("3,60 h")


@pytest.mark.parametrize("hours,formatted,gross,net,vat", [
    ("2,75", "2,75 h", "247,50", "197,21", "50,29"),
    ("4.80", "4,80 h", "432,00", "344,22", "87,78"),
    ("0,00", "0,00 h", "0,00", "0,00", "0,00"),
    ("1.60", "1,60 h", "144,00", "114,74", "29,26"),
    ("0.0005", "0,0005 h", "0,05", "0,04", "0,01"),
    ("1.334999999999999999999999999999999999999999", "1,334999999999999999999999999999999999999999 h",
     "120,15", "95,74", "24,41"),
])
def test_justified_adjustment_zero_and_decimal_preview(page, local_app, hours, formatted, gross, net, vat):
    open_demo(page, local_app)
    adjust(page, hours)
    expect(page.locator("#preview-hours")).to_have_text(formatted)
    expect(page.locator("#preview-choice")).to_contain_text("Perusteltu ajanmuutos", ignore_case=True)
    expect(page.locator("#preview-choice")).to_contain_text("Ei tallennettu")
    expect(page.locator("#preview-reason-summary")).to_contain_text("Keksitty mekaanikon perustelu")
    assert_money(page, "#preview-gross", gross)
    assert_money(page, "#preview-net", net)
    assert_money(page, "#preview-vat", vat)
    expect(page.locator("#original-hours-total")).to_have_text("4,15 h")
    expect(page.locator("#current-final-hours")).to_have_text("3,60 h")
    expect(page.locator("#demo-decision-history li")).to_have_count(3)


def test_adjusted_zero_requires_nonblank_reason(page, local_app):
    open_demo(page, local_app)
    adjust(page, "0,00", reason="   ")
    expect(page.locator("#preview-error")).to_contain_text("Ajanmuutos vaatii perustelun")
    expect(page.locator("#preview-reason")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#preview-result")).to_be_hidden()
    page.locator("#preview-reason").fill("Keksitty takuutyö")
    page.get_by_role("button", name="Näytä esikatselu").click()
    expect(page.locator("#preview-error")).to_be_hidden()
    assert_money(page, "#preview-gross", "0,00")


@pytest.mark.parametrize("width", [1280, 375])
def test_adjustment_reasons_are_bold_and_long_text_wraps(page, local_app, width):
    reason = "Keksitty tarkistettu työaikamuutos.\n" + "pitkäperustelu" * 80
    original = "Keksitty esimerkkiperustelu: mekaanikon tarkistama lopullinen työaika."

    def long_history_fixture(route):
        response = route.fetch()
        script = response.text().replace(original, json.dumps(reason, ensure_ascii=False)[1:-1])
        route.fulfill(response=response, body=script)

    page.route("**/static/labour-review-demo.js", long_history_fixture)
    page.set_viewport_size({"width": width, "height": 900})
    open_demo(page, local_app)
    adjust(page, "0,00", reason=reason)
    preview_reason = page.locator("#preview-reason-summary")
    history_reason = page.locator('#demo-decision-history [data-state="current"] .adjustment-reason')
    expect(preview_reason).to_have_text("Esikatselun perustelu: " + reason)
    expect(history_reason).to_have_text("Perustelu: " + reason)
    for element in (preview_reason, history_reason):
        expect(element).to_have_css("font-weight", "700")
        assert element.evaluate("element => element.scrollWidth <= element.clientWidth")
        assert element.evaluate("""element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return range.getClientRects().length > 2;
        }""")
    expect(page.locator('#demo-decision-history [data-state="stale"] .adjustment-reason')).to_have_css("font-weight", "700")
    expect(page.locator('#demo-decision-history [data-state="previous"] p').first).to_have_css("font-weight", "400")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(page.locator(".demo-badge")).to_have_text("DEMO / ESIKATSELU")
    expect(page.locator("#preview-choice")).to_contain_text("Ei tallennettu")
    page.get_by_role("radio", name="Hyväksyntä sellaisenaan").check()
    page.get_by_role("button", name="Näytä esikatselu").click()
    expect(preview_reason).to_have_css("font-weight", "400")


@pytest.mark.parametrize("hours", ["-0.01", "NaN", "Infinity", "1e2", "", "0,0,0"])
def test_invalid_adjusted_hours_do_not_show_a_price(page, local_app, hours):
    open_demo(page, local_app)
    adjust(page, hours)
    expect(page.locator("#preview-error")).to_contain_text("kelvollisena desimaalilukuna")
    expect(page.locator("#preview-final-hours")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#preview-result")).to_be_hidden()


def test_history_distinguishes_current_previous_and_stale_decisions(page, local_app):
    open_demo(page, local_app)
    current = page.locator('#demo-decision-history [data-state="current"]')
    expect(current).to_have_count(1)
    expect(current).to_contain_text("Voimassa oleva esimerkkipäätös")
    expect(current).to_contain_text("4,15 h")
    expect(current).to_contain_text("3,60 h")
    expect(current).to_contain_text("Keksitty esimerkkiperustelu")
    expect(current).to_contain_text("Esimerkkimekaanikko B (keksitty)")
    expect(current).to_contain_text("demo-employee-b")
    expect(current).to_contain_text("2.7.2026")
    expect(current).to_contain_text("UTC")
    expect(page.locator('#demo-decision-history [data-state="previous"]')).to_contain_text("korvattu uudemmalla")
    stale = page.locator('#demo-decision-history [data-state="stale"]')
    expect(stale).to_have_count(1)
    expect(stale).to_contain_text("suunnitelman sisältö muuttunut")
    expect(stale).to_contain_text("4,10 h")
    expect(stale).to_contain_text("3,50 h")


@pytest.mark.parametrize("scenario,message", [
    ("pending", "suunnitelman sisältö muuttunut"),
    ("rules-changed", "sääntöversio muuttunut"),
])
def test_missing_or_stale_approval_does_not_appear_current(page, local_app, scenario, message):
    open_demo(page, local_app)
    page.locator("#demo-scenario").select_option(scenario)
    expect(page.locator("#current-review-status")).to_contain_text("Ei voimassa olevaa hyväksyntää")
    expect(page.locator("#current-review-details")).to_be_hidden()
    expect(page.locator('#demo-decision-history [data-state="current"]')).to_have_count(0)
    expect(page.locator('#demo-decision-history [data-state="stale"]').first).to_contain_text(message)
    # The separately labelled trial is still possible; it grants no approval.
    page.get_by_role("button", name="Näytä esikatselu").click()
    expect(page.locator("#preview-choice")).to_contain_text("Ei tallennettu")
    expect(page.locator("#current-review-status")).to_contain_text("Ei voimassa olevaa hyväksyntää")


def test_incomplete_plan_does_not_invent_sum_or_allow_approval_preview(page, local_app):
    open_demo(page, local_app)
    page.locator("#demo-scenario").select_option("incomplete")
    expect(page.locator("#original-work-rows")).to_contain_text("Puuttuu")
    expect(page.locator("#original-hours-total")).to_have_text("Ei laskettavissa")
    expect(page.locator("#coverage-notice")).to_contain_text("puuttuvaa aikaa ei arvioida")
    expect(page.get_by_role("button", name="Näytä esikatselu")).to_be_disabled()
    page.locator("#review-preview-form").evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
    expect(page.locator("#preview-result")).to_be_hidden()
    expect(page.locator("#current-review-status")).to_contain_text("Ei voimassa olevaa hyväksyntää")


def test_single_work_does_not_claim_multi_work_review_requirement(page, local_app):
    open_demo(page, local_app)
    page.locator("#demo-scenario").select_option("single")
    expect(page.locator("#original-work-rows tr")).to_have_count(1)
    expect(page.locator("#original-hours-total")).to_have_text("2,55 h")
    expect(page.locator("#review-requirement")).to_contain_text("Yksi korjaustyö")
    expect(page.locator("#review-requirement")).not_to_contain_text("Usean erillisen työn")
    expect(page.locator("#empty-decision-history")).to_be_visible()
    page.get_by_role("button", name="Näytä esikatselu").click()
    assert_money(page, "#preview-gross", "229,50")


def test_edits_invalidate_preview_and_reload_resets_without_network_or_storage_writes(page, local_app):
    url, repository = local_app
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    open_demo(page, local_app)
    original_history = page.locator("#demo-decision-history").inner_text()
    adjust(page, "0.00")
    expect(page.locator("#preview-boundary")).to_contain_text("ei ole tallennettu hyväksyntä")
    expect(page.locator("#preview-boundary")).to_contain_text("auktoritatiivinen lopullinen hinta")
    expect(page.locator("#preview-boundary")).to_contain_text("varaosahinnat säilyvät erillisinä")
    assert page.locator("#demo-decision-history").inner_text() == original_history
    page.locator("#preview-final-hours").fill("1.60")
    expect(page.locator("#preview-result")).to_be_hidden()
    page.get_by_role("radio", name="Hyväksyntä sellaisenaan").check()
    expect(page.locator("#adjustment-fields")).to_be_hidden()
    page.get_by_role("button", name="Näytä esikatselu").click()
    assert_money(page, "#preview-gross", "373,50")
    page.reload()
    expect(page.locator("#preview-result")).to_be_hidden()
    expect(page.get_by_role("radio", name="Hyväksyntä sellaisenaan")).to_be_checked()
    assert all(method == "GET" and "/api/" not in path for method, path in requests)
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    assert repository.list_works() == []
    page.get_by_role("link", name="Palaa nykyiseen työjonoon").click()
    expect(page).to_have_url(url + "/")
    expect(page.locator("#new-work-button")).to_be_visible()


def test_demo_layout_mobile_and_keyboard_with_no_javascript_errors(page, local_app, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_demo(page, local_app)
    page.get_by_role("button", name="Näytä esikatselu").click()
    page.screenshot(path=str(tmp_path / "labour-review-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("radio", name="Perusteltu ajanmuutos").check()
    page.locator("#preview-final-hours").fill("0,00")
    page.locator("#preview-reason").fill("Keksitty testiperustelu")
    page.locator("#preview-reason").press("Tab")
    expect(page.get_by_role("button", name="Näytä esikatselu")).to_be_focused()
    page.keyboard.press("Enter")
    assert_money(page, "#preview-gross", "0,00")
    page.screenshot(path=str(tmp_path / "labour-review-mobile.png"), full_page=True)
    assert errors == []


def test_static_demo_adds_no_public_internal_read_or_write_routes(tmp_path):
    app = create_app(tmp_path / "demo-route-test.db")
    with TestClient(app) as client:
        assert set(app.openapi()["paths"]) == {"/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour"}
        response = client.get(DEMO_PATH)
        assert response.status_code == 200
        assert "DEMO / ESIKATSELU" in response.text
        for method, path in [("GET", "/api/labour-plans"), ("GET", "/api/labour-decisions"),
                             ("POST", "/api/labour-plans/approve")]:
            assert client.request(method, path).status_code == 404
