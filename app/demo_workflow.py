from datetime import datetime

from domain.models.final_quote import FinalQuote
from domain.models.indexed_product import IndexedProduct
from domain.models.labour_time import LabourTime
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import SupplierOffer
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem
from domain.rules.final_quote import create_final_quote
from domain.rules.human_selection import select_candidate_from_proposal
from domain.rules.labour_cost import calculate_labour_cost
from domain.rules.supplier_candidates import build_supplier_candidates

def create_demo_proposal(
    proposal_id: str,
    vehicle: Vehicle,
    work_item: WorkItem,
    labour_time: LabourTime,
    products: list[IndexedProduct],
    offers: list[SupplierOffer],
    created_at: datetime,
) -> RawProposal:
    if len(work_item.part_requirements) != 1:
        raise ValueError(
            "demo workflow currently requires exactly one part requirement"
        )

    requirement = work_item.part_requirements[0]

    labour_cost = calculate_labour_cost(
        labour_time
    )

    candidates = build_supplier_candidates(
        requirement=requirement,
        products=products,
        offers=offers,
    )

    return RawProposal(
        proposal_id=proposal_id,
        vehicle=vehicle,
        work_item=work_item,
        labour_time=labour_time,
        labour_cost=labour_cost,
        candidates=candidates,
        created_at=created_at,
    )


def create_demo_final_quote(
    quote_id: str,
    proposal: RawProposal,
    selected_candidate: SupplierCandidate,
    selected_by: str,
    selected_at: datetime,
    created_at: datetime,
) -> FinalQuote:
    selection = select_candidate_from_proposal(
        proposal=proposal,
        candidate=selected_candidate,
        selected_by=selected_by,
        selected_at=selected_at,
    )

    return create_final_quote(
        quote_id=quote_id,
        proposal=proposal,
        selection=selection,
        created_at=created_at,
    )

