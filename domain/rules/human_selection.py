from datetime import datetime

from domain.models.human_selection import HumanSelection
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_candidate import SupplierCandidate


def create_human_selection(
    candidate: SupplierCandidate,
    proposal_id: str,
    selected_by: str,
    selected_at: datetime,
) -> HumanSelection:
    if not proposal_id.strip():
        raise ValueError("proposal_id must not be empty")
    if not selected_by.strip():
        raise ValueError("selected_by must not be empty")

    return HumanSelection(
        proposal_id=proposal_id,
        product_id=candidate.product_id,
        supplier=candidate.supplier,
        supplier_product_id=candidate.supplier_product_id,
        commercial_quantity_needed=candidate.commercial_quantity_needed,
        unit_price=candidate.unit_price,
        total_price=candidate.total_price,
        currency=candidate.currency,
        availability=candidate.availability,
        selected_by=selected_by,
        selected_at=selected_at,
    )


def select_candidate_from_proposal(
    proposal: RawProposal,
    candidate: SupplierCandidate,
    selected_by: str,
    selected_at: datetime,
) -> HumanSelection:
    if candidate not in proposal.candidates:
        raise ValueError(
            "candidate must belong to the raw proposal"
        )

    return create_human_selection(
        candidate=candidate,
        proposal_id=proposal.proposal_id,
        selected_by=selected_by,
        selected_at=selected_at,
    )
