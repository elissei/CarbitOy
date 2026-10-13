from datetime import datetime, timezone

from domain.models.labour_time import LabourTime
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem
from services.labour_pricing import calculate_labour_cost


def build_raw_proposal(
    proposal_id: str,
    vehicle: Vehicle,
    work_item: WorkItem,
    labour_time: LabourTime,
    candidates: list[SupplierCandidate] | None = None,
) -> RawProposal:
    """Build a proposal using the existing labour pricing rules."""
    return RawProposal(
        proposal_id=proposal_id,
        vehicle=vehicle,
        work_item=work_item,
        labour_time=labour_time,
        labour_cost=calculate_labour_cost(labour_time),
        candidates=list(candidates) if candidates is not None else [],
        created_at=datetime.now(timezone.utc),
    )
