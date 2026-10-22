"""Synthetic pages read through Chromium; this proves nothing about real Autodata.

Pages are served from integrations/autodata/synthetic by request interception.
Every other request is aborted and recorded, so no test reaches a network.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from integrations.autodata.models import (
    ChoiceIdentity, ChoiceLevel, LabourTimeRequest, LookupOutcome, ResolvedBy, VehicleQuery, WorkQuery,
)
from integrations.autodata.synthetic_reader import SyntheticPageAdapter


PAGES = Path(__file__).resolve().parent.parent / "integrations" / "autodata" / "synthetic"
ORIGIN = "https://synthetic.carbit.test"
BASE = ORIGIN + "/autodata/"
PETROL = ChoiceIdentity(source_id="SYN-V-001", selection_path=(
    "Testimerkki A", "Testimalli 1", "1.6 bensiini 2015-2018"))
DIESEL = ChoiceIdentity(selection_path=("Testimerkki A", "Testimalli 1", "2.0 diesel 2015-2018"))
BRAKES = ChoiceIdentity(source_id="SYN-O-101", selection_path=("Jarrut", "Etujarrupalat, vaihto"))
PAIR = ChoiceIdentity(source_id="SYN-O-102", selection_path=(
    "Iskunvaimentimet", "Etuiskunvaimentimet, molemmat puolet, vaihto"))
DISCS = ChoiceIdentity(selection_path=("Jarrut", "Etujarrulevyt ja -palat, vaihto"))


def op(*labels):
    return ChoiceIdentity(selection_path=labels)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def synthetic(browser):
    context = browser.new_context()
    blocked = []

    def serve(route):
        url = route.request.url
        name = url.split("?", 1)[0].removeprefix(BASE)
        path = PAGES / name
        if not url.startswith(BASE) or "/" in name or path.suffix != ".html" or not path.is_file():
            blocked.append(url)
            route.abort()
            return
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=path.read_bytes())

    context.route("**/*", serve)
    page = context.new_page()
    yield page, blocked
    context.close()


def lookup(page, entry, vehicle=None, operation=None, variant=None, **kwargs):
    adapter = SyntheticPageAdapter(page, BASE + entry, **kwargs)
    return adapter.lookup(LabourTimeRequest(
        vehicle_query=VehicleQuery(registration="SYN-1"), work_query=WorkQuery(search_text="testi"),
        vehicle_selection=vehicle, operation_selection=operation, variant_selection=variant,
    ))


def test_ambiguous_vehicle_page_returns_both_options(synthetic):
    page, blocked = synthetic
    result = lookup(page, "vehicles.html")
    assert result.outcome == LookupOutcome.VEHICLE_AMBIGUOUS
    assert [c.identity for c in result.candidates] == [PETROL, DIESEL]
    assert result.candidates[0].details == ("SYN16A", "85 kW")
    assert result.hours is None and result.provenance.synthetic is True
    assert blocked == []


def test_selected_chain_reads_exact_displayed_time(synthetic):
    page, blocked = synthetic
    result = lookup(page, "vehicles.html", vehicle=PETROL, operation=BRAKES)
    assert result.outcome == LookupOutcome.FOUND
    assert result.vehicle.choice.identity == PETROL
    assert result.operation.choice.identity == BRAKES
    assert result.variant.resolved_by == ResolvedBy.ONLY_CANDIDATE
    assert result.time.raw_time_text == "1,2 h"
    assert result.hours == Decimal("1.2")
    assert blocked == []


def test_vehicle_without_source_id_resolves_by_path(synthetic):
    page, _ = synthetic
    result = lookup(page, "vehicles.html", vehicle=DIESEL, operation=PAIR)
    assert result.outcome == LookupOutcome.FOUND
    assert result.vehicle.choice.identity.source_id is None
    assert result.hours == Decimal("2.4")


def test_single_vehicle_then_operation_options(synthetic):
    page, _ = synthetic
    result = lookup(page, "vehicles-single.html")
    assert result.outcome == LookupOutcome.WORK_AMBIGUOUS
    assert result.vehicle.resolved_by == ResolvedBy.ONLY_CANDIDATE
    assert result.level == ChoiceLevel.OPERATION
    assert len(result.candidates) == 7 and result.hours is None


def test_time_variants_require_a_choice(synthetic):
    page, _ = synthetic
    vehicle = ChoiceIdentity(selection_path=("Testimerkki B", "Testimalli 2", "1.4 bensiini 2019-2022"))
    ambiguous = lookup(page, "vehicles-single.html", vehicle=vehicle, operation=DISCS)
    assert ambiguous.outcome == LookupOutcome.VARIANT_AMBIGUOUS
    assert [c.label for c in ambiguous.candidates] == ["Ilman ilmastointia", "Ilmastoinnilla"]
    chosen = lookup(page, "vehicles-single.html", vehicle=vehicle, operation=DISCS,
                    variant=op("Ilmastoinnilla"))
    assert chosen.outcome == LookupOutcome.FOUND
    assert chosen.time.raw_time_text == "1.9 h" and chosen.hours == Decimal("1.9")


@pytest.mark.parametrize(("operation", "outcome", "has_variant"), [
    (op("Valot", "Ajovalo, säätö"), LookupOutcome.TIME_MISSING, False),
    (op("Sähkö", "Laturi, vaihto"), LookupOutcome.TIME_MISSING, True),
    (op("Sähkö", "Käynnistinmoottori, vaihto"), LookupOutcome.UNPARSEABLE, True),
])
def test_missing_or_rejected_times_have_no_hours(synthetic, operation, outcome, has_variant):
    page, _ = synthetic
    vehicle = ChoiceIdentity(selection_path=("Testimerkki B", "Testimalli 2", "1.4 bensiini 2019-2022"))
    result = lookup(page, "vehicles-single.html", vehicle=vehicle, operation=operation)
    assert result.outcome == outcome
    assert (result.variant is not None) == has_variant
    assert result.hours is None
    if outcome == LookupOutcome.UNPARSEABLE:
        assert result.rejected_time_text == "1:30 h"
        assert result.rejection_reason == "CLOCK_FORMAT"


def test_variant_with_several_time_fields_is_site_changed_not_found(synthetic):
    # Codex P2: the reader must not take the first of two different times.
    page, blocked = synthetic
    vehicle = ChoiceIdentity(selection_path=("Testimerkki B", "Testimalli 2", "1.4 bensiini 2019-2022"))
    result = lookup(page, "vehicles-single.html", vehicle=vehicle,
                    operation=op("Muut", "Kaksi aikakenttää"))
    assert result.outcome == LookupOutcome.SITE_CHANGED
    assert result.operation is not None and result.variant is None
    assert result.hours is None and result.time is None
    assert blocked == []


def test_selection_not_on_page_returns_options(synthetic):
    page, _ = synthetic
    result = lookup(page, "vehicles.html", vehicle=op("Testimerkki Z"))
    assert result.outcome == LookupOutcome.SELECTION_INVALID
    assert len(result.candidates) == 2 and result.hours is None


@pytest.mark.parametrize(("entry", "outcome"), [
    ("vehicles-empty.html", LookupOutcome.VEHICLE_NOT_FOUND),
    ("vehicles-duplicate.html", LookupOutcome.SELECTION_NOT_UNIQUE),
    ("vehicles-blank-label.html", LookupOutcome.SITE_CHANGED),
    ("login.html", LookupOutcome.LOGIN_REQUIRED),
    ("blocked.html", LookupOutcome.BLOCKED),
    ("changed.html", LookupOutcome.SITE_CHANGED),
    ("operations.html", LookupOutcome.SITE_CHANGED),
    ("missing.html", LookupOutcome.ERROR),
])
def test_page_level_outcomes(synthetic, entry, outcome):
    page, _ = synthetic
    result = lookup(page, entry)
    assert result.outcome == outcome
    assert result.hours is None and result.time is None


def test_link_leaving_synthetic_origin_is_not_followed(synthetic):
    page, blocked = synthetic
    result = lookup(page, "vehicles-offsite.html")
    assert result.outcome == LookupOutcome.SITE_CHANGED
    assert result.vehicle is not None and result.hours is None
    assert blocked == []


def test_unanswered_page_times_out(browser):
    context = browser.new_context()
    context.route("**/*", lambda route: None)
    try:
        result = lookup(context.new_page(), "vehicles.html", timeout_ms=500)
    finally:
        context.close()
    assert result.outcome == LookupOutcome.TIMEOUT
    assert result.hours is None
