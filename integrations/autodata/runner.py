"""Runs one labour-time lookup at a time; failures become stored outcomes.

The adapter is called synchronously in the caller's thread, never in a
background thread that could outlive the released lookup lock. An exceeded
time limit is therefore detected only after the call returns; a hard
interruption belongs to the subprocess adapter (1C). The runner logs the
outcome only: no page content, URLs, cookies, request fields or exception text.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from integrations.autodata.adapter import AutodataLabourAdapter
from integrations.autodata.models import (
    LabourTimeLookupResult, LabourTimeRequest, LookupOutcome, LookupProvenance,
)


DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 600.0
# One lookup per process; the service holds it from its retry check to storage.
# Never injectable: separately built services must not run lookups side by side.
PROCESS_LOOKUP_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AdapterIdentity:
    """Given explicitly, never read from the adapter; used for ERROR and TIMEOUT."""

    name: str
    version: str
    synthetic: bool

    def __post_init__(self):
        if (type(self.name) is not str or not self.name.strip()
                or type(self.version) is not str or not self.version.strip()
                or type(self.synthetic) is not bool):
            raise ValueError("an adapter identity requires a name, a version and a synthetic flag")


@dataclass(frozen=True)
class LookupRun:
    requested_at: datetime
    result: LabourTimeLookupResult


class LookupRunner:
    def __init__(self, adapter: AutodataLabourAdapter, identity: AdapterIdentity, *,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, clock=_utc_now,
                 monotonic=time.monotonic) -> None:
        if not isinstance(adapter, AutodataLabourAdapter):
            raise TypeError("an autodata labour adapter is required")
        if not isinstance(identity, AdapterIdentity):
            raise TypeError("an explicit adapter identity is required")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ValueError(f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]")
        self.adapter = adapter
        self.identity = identity
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.monotonic = monotonic

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.utcoffset() is None:
            raise ValueError("the runner clock must include a timezone")
        return moment.astimezone(timezone.utc)

    def _failure(self, outcome: LookupOutcome, retrieved_at: datetime) -> LabourTimeLookupResult:
        return LabourTimeLookupResult(outcome=outcome, provenance=LookupProvenance(
            adapter_name=self.identity.name, adapter_version=self.identity.version,
            retrieved_at=retrieved_at, synthetic=self.identity.synthetic,
        ))

    def _matches(self, result, requested_at: datetime, finished: datetime) -> bool:
        if not isinstance(result, LabourTimeLookupResult):
            return False
        provenance = result.provenance
        return ((provenance.adapter_name, provenance.adapter_version, provenance.synthetic)
                == (self.identity.name, self.identity.version, self.identity.synthetic)
                and requested_at <= provenance.retrieved_at <= finished)

    def run(self, request: LabourTimeRequest) -> LookupRun:
        """Call the adapter once; PROCESS_LOOKUP_LOCK is the caller's (the lookup service)."""
        requested_at = self._now()
        started = self.monotonic()
        try:
            result = self.adapter.lookup(request)
        except Exception:
            # Never stored or logged: the text may echo page content or inputs.
            result = None
        elapsed = self.monotonic() - started
        # The stored retrieved_at may never precede requested_at, even if the clock steps back.
        finished = max(self._now(), requested_at)
        if elapsed > self.timeout_seconds:
            result = self._failure(LookupOutcome.TIMEOUT, finished)
        elif not self._matches(result, requested_at, finished):
            result = self._failure(LookupOutcome.ERROR, finished)
        logger.info("autodata lookup finished: outcome=%s", result.outcome.value)
        return LookupRun(requested_at=requested_at, result=result)
