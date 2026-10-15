"""SEC-1.1 browser confirmation and bounded Retry-After messaging."""

from dataclasses import replace

import pytest
from playwright.sync_api import expect

from test_webapp_browser import browser, fill_inquiry, local_app, page


@pytest.mark.parametrize("status", [403, 429, 503])
def test_rejected_logout_never_claims_revocation_or_replays(page, local_app, status):
    attempts = []

    def reject(route):
        attempts.append(route.request)
        route.fulfill(status=status, headers={"Retry-After": "17"},
                      json={"detail": "Fictional rejected logout"})

    page.goto(local_app[0], wait_until="networkidle")
    expect(page.locator("#business-shell")).to_be_visible()
    page.route("**/api/auth/logout", reject)
    page.locator("#logout-button").click()
    expect(page.locator("#auth-panel")).to_have_attribute("data-state", "uncertain")
    expect(page.locator("#auth-message")).to_contain_text("Uloskirjautumista ei vahvistettu.")
    assert "Olet kirjautunut ulos" not in page.locator("#auth-message").inner_text()
    if status == 429:
        expect(page.locator("#auth-message")).to_contain_text("17 sekuntia")
        assert "viisi minuuttia" not in page.locator("#auth-message").inner_text()
    expect(page.locator("#business-shell")).to_be_hidden()
    page.wait_for_timeout(100)
    assert len(attempts) == 1
    assert local_app.auth_service.inspect_session(local_app.token)


def test_lost_logout_response_remains_uncertain_even_after_server_revocation(page, local_app):
    from webapp.auth_service import AuthenticationError

    attempts = []
    page.goto(local_app[0], wait_until="networkidle")
    expect(page.locator("#business-shell")).to_be_visible()

    def lose(route):
        attempts.append(route.request)
        assert route.fetch().status == 204
        route.abort()

    page.route("**/api/auth/logout", lose)
    page.locator("#logout-button").click()
    expect(page.locator("#auth-panel")).to_have_attribute("data-state", "uncertain")
    expect(page.locator("#auth-message")).to_contain_text("Uloskirjautumista ei vahvistettu.")
    assert "Olet kirjautunut ulos" not in page.locator("#auth-message").inner_text()
    assert len(attempts) == 1
    with pytest.raises(AuthenticationError):
        local_app.auth_service.inspect_session(local_app.token)


def test_real_logout_is_available_after_more_than_five_bad_logout_requests(page, local_app):
    from webapp.auth_service import AuthenticationError

    page.goto(local_app[0], wait_until="networkidle")
    expect(page.locator("#business-shell")).to_be_visible()
    statuses = page.evaluate("""async () => {
        const proof = await sessionProof();
        const statuses = [];
        for (let i = 0; i < 8; i++) {
            const response = await fetch('/api/auth/logout', {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': proof},
                body: JSON.stringify({unexpected: true})
            });
            statuses.push(response.status);
        }
        return statuses;
    }""")
    assert statuses == [422] * 8
    assert local_app.auth_service.inspect_session(local_app.token)
    page.locator("#logout-button").click()
    expect(page.locator("#auth-message")).to_contain_text("Olet kirjautunut ulos.")
    with pytest.raises(AuthenticationError):
        local_app.auth_service.inspect_session(local_app.token)


def test_real_maintenance_429_displays_remaining_window_without_background_retry(page, local_app):
    service = local_app.auth_service
    service.policy = replace(service.policy, maintenance_individual_attempts=1,
                             maintenance_global_attempts=10)
    # Initial page session confirmation fills the one-request maintenance budget.
    page.goto(local_app[0], wait_until="networkidle")
    expect(page.locator("#business-shell")).to_be_visible()
    responses = []
    page.on("response", lambda response: responses.append(response)
            if response.url.endswith("/api/auth/csrf") else None)
    fill_inquiry(page)
    page.locator('#new-work-form button[type="submit"]').click()
    expect(page.locator("#new-work-error")).to_contain_text("sekuntia ennen uutta yritystä")
    assert "viisi minuuttia" not in page.locator("#new-work-error").inner_text()
    assert len(responses) == 1 and responses[0].status == 429
    seconds = int(responses[0].headers["retry-after"])
    assert 1 <= seconds <= 60
    expect(page.locator("#new-work-error")).to_contain_text(f"Odota {seconds} sekuntia")
    assert local_app[1].list_works() == []


@pytest.mark.parametrize("value", [None, "", "-1", "0", "NaN", "999999999", "<script>", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_invalid_retry_after_uses_safe_generic_message(page, local_app, value):
    page.goto(local_app[0], wait_until="networkidle")
    assert page.evaluate("value => CarbitAuth.rateLimitMessage(value)", value) == (
        "Liian monta pyyntöä. Odota hetki ennen uutta yritystä."
    )
