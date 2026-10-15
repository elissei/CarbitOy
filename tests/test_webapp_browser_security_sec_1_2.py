"""SEC-1.2 two-tab logout confirmations against a real HTTPS server."""

import pytest
from playwright.sync_api import expect

from test_webapp_auth_security_sec_1_1 import event_count
from test_webapp_browser import browser, local_app, page, wait_for_held
from webapp.auth_service import AuthenticationError


@pytest.mark.parametrize("winner", [0, 1])
def test_overlapping_two_tab_logout_only_confirmed_tab_claims_success(page, local_app, winner):
    # BroadcastChannel is optional in production. Exercise the supported fallback
    # so both tabs can submit before either receives a server confirmation.
    page.context.add_init_script("window.BroadcastChannel = undefined;")
    second = page.context.new_page()
    tabs = [page, second]
    held = {}
    attempts = []

    def hold_for(key):
        def hold(route):
            attempts.append(key)
            held[key] = route
        return hold

    revoked_before = event_count(local_app.auth_service.repository, "SESSION_REVOKED")
    try:
        for index, tab in enumerate(tabs):
            tab.goto(local_app[0], wait_until="networkidle")
            expect(tab.locator("#business-shell")).to_be_visible()
            tab.route("**/api/auth/logout", hold_for(index))
        for index, tab in enumerate(tabs):
            tab.locator("#logout-button").click()
            wait_for_held(tab, held, index)
        assert len(held) == 2
        assert local_app.auth_service.inspect_session(local_app.token)
        confirmed = held[winner].fetch()
        assert confirmed.status == 204
        held[winner].fulfill(response=confirmed)
        expect(tabs[winner].locator("#auth-message")).to_contain_text("Olet kirjautunut ulos.")
        loser = 1 - winner
        rejected = held[loser].fetch()
        assert rejected.status == 401
        held[loser].fulfill(response=rejected)
        expect(tabs[loser].locator("#auth-panel")).to_have_attribute("data-state", "uncertain")
        expect(tabs[loser].locator("#auth-message")).to_contain_text("Uloskirjautumista ei vahvistettu.")
        assert "Olet kirjautunut ulos" not in tabs[loser].locator("#auth-message").inner_text()
        assert all(tab.locator("#business-shell").is_hidden() for tab in tabs)
        assert event_count(local_app.auth_service.repository, "SESSION_REVOKED") == revoked_before + 1
        assert not any(cookie["name"] == "__Host-carbit-session" for cookie in page.context.cookies())
        with pytest.raises(AuthenticationError):
            local_app.auth_service.inspect_session(local_app.token)
        # No automatic resend in either tab after confirmed/unconfirmed outcomes.
        assert len(held) == 2 and sorted(attempts) == [0, 1]
    finally:
        second.unroute_all(behavior="ignoreErrors")
        second.close()


def test_cross_tab_notification_locks_without_claiming_server_revocation(page, local_app):
    second = page.context.new_page()
    held = []
    try:
        for tab in (page, second):
            tab.goto(local_app[0], wait_until="networkidle")
            expect(tab.locator("#business-shell")).to_be_visible()
        page.route("**/api/auth/logout", lambda route: held.append(route))
        page.locator("#logout-button").click()
        wait_for_held(page, held, 0)
        expect(second.locator("#auth-panel")).to_have_attribute("data-state", "uncertain")
        expect(second.locator("#business-shell")).to_be_hidden()
        assert "Olet kirjautunut ulos" not in second.locator("#auth-message").inner_text()
        assert local_app.auth_service.inspect_session(local_app.token)
        confirmed = held[0].fetch()
        assert confirmed.status == 204
        held[0].fulfill(response=confirmed)
        expect(page.locator("#auth-message")).to_contain_text("Olet kirjautunut ulos.")
        expect(second.locator("#auth-panel")).to_have_attribute("data-state", "uncertain")
        with pytest.raises(AuthenticationError):
            local_app.auth_service.inspect_session(local_app.token)
        assert len(held) == 1
    finally:
        second.close()
