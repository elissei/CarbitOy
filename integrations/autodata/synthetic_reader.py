"""Playwright reader for CARBIT's own synthetic pages; not a real Autodata reader.

The markup below is a CARBIT test contract. It says nothing about the real
Autodata pages, whose structure and selectors are not yet mapped. Passing
tests here show only that selection, outcome and time handling work through
a real browser.

Synthetic markup:
- exactly one element with data-synthetic-page = vehicles | operations |
  variants | login-required | blocked
- each choice has data-synthetic-choice, optional data-source-id (shown only
  when the page itself provides one), one or more data-synthetic-path labels,
  optional data-synthetic-detail texts and, except for variants, an href
- a variant shows its time in exactly one data-synthetic-time; a missing
  element means no displayed time, and several elements mean SITE_CHANGED.

The work query is not used for filtering: each synthetic page is a fixed
result list.
"""

from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from integrations.autodata.adapter import AutodataLabourAdapter
from integrations.autodata.models import (
    Choice, ChoiceIdentity, ChoiceLevel, LabourTimeLookupResult, LabourTimeRequest,
    LookupOutcome, LookupProvenance,
)
from integrations.autodata.resolution import LookupBuilder


SYNTHETIC_ADAPTER_VERSION = "synthetic-1"
EXPECTED_PAGE = {
    ChoiceLevel.VEHICLE: "vehicles",
    ChoiceLevel.OPERATION: "operations",
    ChoiceLevel.VARIANT: "variants",
}
STOP_PAGES = {"login-required": LookupOutcome.LOGIN_REQUIRED, "blocked": LookupOutcome.BLOCKED}

_READ_PAGE = """() => {
  const clean = (node) => (node.textContent || "").replace(/\\s+/g, " ").trim();
  const markers = document.querySelectorAll("[data-synthetic-page]");
  if (markers.length !== 1) return {kind: null, choices: []};
  const choices = [...markers[0].querySelectorAll("[data-synthetic-choice]")].map((node) => {
    const times = node.querySelectorAll("[data-synthetic-time]");
    const time = times.length === 1 ? times[0] : null;
    return {
      time_count: times.length,
      source_id: node.hasAttribute("data-source-id") ? node.getAttribute("data-source-id") : null,
      path: [...node.querySelectorAll("[data-synthetic-path]")].map(clean),
      details: [...node.querySelectorAll("[data-synthetic-detail]")].map(clean),
      href: node.getAttribute("href"),
      time: time === null ? null : (time.textContent || ""),
    };
  });
  return {kind: markers[0].getAttribute("data-synthetic-page"), choices};
}"""


class SiteChanged(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_synthetic_page(page) -> tuple[str | None, list[tuple[Choice, dict]]]:
    """Return the page kind and its validated choices with their raw values."""
    snapshot = page.evaluate(_READ_PAGE)
    choices = []
    for raw in snapshot["choices"]:
        if raw["time_count"] > 1:
            # Never pick one of several displayed times for the same choice.
            raise SiteChanged("a synthetic choice shows more than one time")
        try:
            choice = Choice(identity=ChoiceIdentity(
                source_id=raw["source_id"], selection_path=tuple(raw["path"]),
            ), details=tuple(raw["details"]))
        except ValidationError as error:
            raise SiteChanged("a synthetic choice lacks a usable identity") from error
        choices.append((choice, raw))
    return snapshot["kind"], choices


class SyntheticPageAdapter(AutodataLabourAdapter):
    def __init__(self, page, entry_url: str, *, timeout_ms: int = 10_000, clock=_utc_now) -> None:
        self.page = page
        self.entry_url = entry_url
        self.timeout_ms = timeout_ms
        self.clock = clock

    def _goto(self, url: str) -> None:
        target = urljoin(self.page.url if self.page.url != "about:blank" else self.entry_url, url)
        origin = urlsplit(self.entry_url)
        parsed = urlsplit(target)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise SiteChanged("a synthetic link left the synthetic origin")
        self.page.goto(target, timeout=self.timeout_ms)

    def _read(self, level: ChoiceLevel, builder: LookupBuilder):
        kind, choices = read_synthetic_page(self.page)
        if kind in STOP_PAGES:
            return builder.result(STOP_PAGES[kind]), None
        if kind != EXPECTED_PAGE[level]:
            raise SiteChanged(f"expected a {EXPECTED_PAGE[level]} page")
        return None, choices

    def _step(self, level, builder, selection):
        stop, choices = self._read(level, builder)
        if stop is not None:
            return stop, None
        stop = builder.resolve(level, (choice for choice, _ in choices), selection)
        if stop is not None:
            return stop, None
        chosen = builder.resolved[level.value.lower()].choice
        return None, next(raw for choice, raw in choices if choice == chosen)

    def lookup(self, request: LabourTimeRequest) -> LabourTimeLookupResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        builder = LookupBuilder(LookupProvenance(
            adapter_name="synthetic-page", adapter_version=SYNTHETIC_ADAPTER_VERSION,
            retrieved_at=self.clock(), synthetic=True,
        ))
        try:
            self._goto(self.entry_url)
            selections = (
                (ChoiceLevel.VEHICLE, request.vehicle_selection),
                (ChoiceLevel.OPERATION, request.operation_selection),
            )
            for level, selection in selections:
                stop, raw = self._step(level, builder, selection)
                if stop is not None:
                    return stop
                if not raw["href"]:
                    raise SiteChanged("a synthetic choice has no link")
                self._goto(raw["href"])
            stop, raw = self._step(ChoiceLevel.VARIANT, builder, request.variant_selection)
            if stop is not None:
                return stop
            return builder.time_result(raw["time"])
        except SiteChanged:
            return builder.result(LookupOutcome.SITE_CHANGED)
        except PlaywrightTimeoutError:
            return builder.result(LookupOutcome.TIMEOUT)
        except PlaywrightError:
            return builder.result(LookupOutcome.ERROR)
