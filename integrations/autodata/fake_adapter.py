"""Deterministic in-memory adapter for tests; its data is not Autodata data.

Every catalog vehicle counts as a match for the vehicle query. Operations
match when the search text occurs in any of their path labels, ignoring case.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from integrations.autodata.adapter import AutodataLabourAdapter
from integrations.autodata.models import (
    Choice, ChoiceLevel, LabourTimeLookupResult, LabourTimeRequest, LookupOutcome,
    LookupProvenance,
)
from integrations.autodata.resolution import LookupBuilder


FAKE_ADAPTER_VERSION = "fake-1"
# Outcomes the fake can simulate before any choice is read.
SIMULATED_FAILURES = frozenset({
    LookupOutcome.LOGIN_REQUIRED, LookupOutcome.BLOCKED, LookupOutcome.SITE_CHANGED,
    LookupOutcome.TIMEOUT, LookupOutcome.ERROR,
})


@dataclass(frozen=True)
class FakeVariant:
    choice: Choice
    displayed_time_text: str | None


@dataclass(frozen=True)
class FakeOperation:
    choice: Choice
    variants: tuple[FakeVariant, ...]


@dataclass(frozen=True)
class FakeVehicle:
    choice: Choice
    operations: tuple[FakeOperation, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeAutodataLabourAdapter(AutodataLabourAdapter):
    def __init__(self, vehicles: tuple[FakeVehicle, ...] = (), *,
                 failure: LookupOutcome | None = None, clock=_utc_now) -> None:
        if failure is not None and failure not in SIMULATED_FAILURES:
            raise ValueError("the fake simulates only source-level failures")
        self.vehicles = tuple(vehicles)
        self.failure = failure
        self.clock = clock
        self.requests: list[LabourTimeRequest] = []

    def lookup(self, request: LabourTimeRequest) -> LabourTimeLookupResult:
        self.requests.append(request)
        builder = LookupBuilder(LookupProvenance(
            adapter_name="fake", adapter_version=FAKE_ADAPTER_VERSION,
            retrieved_at=self.clock(), synthetic=True,
        ))
        if self.failure is not None:
            return builder.result(self.failure)
        stop = builder.resolve(ChoiceLevel.VEHICLE, (v.choice for v in self.vehicles),
                               request.vehicle_selection)
        if stop is not None:
            return stop
        vehicle = next(v for v in self.vehicles if v.choice == builder.resolved["vehicle"].choice)
        needle = request.work_query.search_text.casefold()
        operations = [
            operation for operation in vehicle.operations
            if any(needle in label.casefold() for label in operation.choice.identity.selection_path)
        ]
        stop = builder.resolve(ChoiceLevel.OPERATION, (o.choice for o in operations),
                               request.operation_selection)
        if stop is not None:
            return stop
        operation = next(o for o in operations if o.choice == builder.resolved["operation"].choice)
        stop = builder.resolve(ChoiceLevel.VARIANT, (v.choice for v in operation.variants),
                               request.variant_selection)
        if stop is not None:
            return stop
        variant = next(v for v in operation.variants if v.choice == builder.resolved["variant"].choice)
        return builder.time_result(variant.displayed_time_text)
