"""Pure WD-2 projection: preserves domain 3A/3B, never grants approval."""

from dataclasses import dataclass

from domain.models.labour_plan import LabourPlan, LabourTimeEntry
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.part_requirement import PartRequirement
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan
from app.work_plan_contract import WorkPlanSnapshot


@dataclass(frozen=True)
class MissingData:
    path: str
    reason: str = "required for domain projection"


class IncompleteWorkPlanError(ValueError):
    def __init__(self, missing: tuple[MissingData, ...]):
        self.missing = missing
        super().__init__("incomplete application snapshot: " + ", ".join(item.path for item in missing))


def _validated(snapshot: WorkPlanSnapshot) -> WorkPlanSnapshot:
    # model_copy(update=...) bypasses validation: revalidate at boundaries.
    return WorkPlanSnapshot.model_validate(snapshot.model_dump(round_trip=True))


def _missing_work_data(snapshot: WorkPlanSnapshot) -> tuple[MissingData, ...]:
    missing = []
    for name in ("registration", "make", "model"):
        if getattr(snapshot.vehicle, name) is None:
            missing.append(MissingData(f"vehicle.{name}"))
    if not snapshot.works:
        missing.append(MissingData("works", "domain WorkPlan requires at least one identified work"))
    for work in snapshot.works:
        for name in ("operation", "axle", "side"):
            if getattr(work, name) is None:
                missing.append(MissingData(f"works[{work.work_id}].{name}"))
        for need in work.part_needs:
            for name in ("canonical_product_type", "axle", "required_quantity", "required_unit"):
                if getattr(need, name) is None:
                    missing.append(MissingData(f"works[{work.work_id}].part_needs[{need.requirement_id}].{name}"))
    return tuple(missing)


def missing_work_data(snapshot: WorkPlanSnapshot) -> tuple[MissingData, ...]:
    """Editor readiness, independent of time coverage and all approvals."""
    return _missing_work_data(_validated(snapshot))


def to_domain_work_plan(snapshot: WorkPlanSnapshot) -> WorkPlan:
    snapshot = _validated(snapshot)
    missing = _missing_work_data(snapshot)
    if missing:
        raise IncompleteWorkPlanError(missing)
    return WorkPlan(
        vehicle=Vehicle(**snapshot.vehicle.model_dump()),
        work_items=[PlannedWork(
            work_id=work.work_id,
            work_item=WorkItem(
                operation=work.operation, axle=work.axle, side=work.side,
                part_requirements=[PartRequirement(**need.model_dump(exclude={"requirement_id"}))
                                   for need in work.part_needs],
            ),
        ) for work in sorted(snapshot.works, key=lambda work: work.display_order)],
    )


def to_domain_labour_plan(snapshot: WorkPlanSnapshot) -> LabourPlan:
    """Project explicit originals/selections without inventing missing times.

    No automatic selection, quantity multiplication, overlap deduction or
    review transfer. Domain issues report missing/partial/pair coverage.
    """
    snapshot = _validated(snapshot)
    work_plan = to_domain_work_plan(snapshot)
    missing = []
    for entry in snapshot.original_times:
        required = ["hours", "source"]
        if entry.source == LabourTimeSource.MANUAL_AUTODATA:
            required += ["work_description", "recorded_by"]
        for name in required:
            if getattr(entry, name) is None:
                missing.append(MissingData(f"original_times[{entry.time_id}].{name}"))
    if missing:
        raise IncompleteWorkPlanError(tuple(missing))
    return LabourPlan(
        work_plan=work_plan,
        original_times=[LabourTimeEntry(
            time_id=entry.time_id, covered_work_ids=list(entry.covered_work_ids), side=entry.side,
            labour_time=LabourTime(**entry.model_dump(exclude={"time_id", "covered_work_ids", "side"})),
        ) for entry in snapshot.original_times],
        selected_time_ids=list(snapshot.selected_time_ids),
    )
