from datetime import datetime

from domain.models.final_quote import FinalQuote
from domain.models.human_selection import HumanSelection
from domain.models.raw_proposal import RawProposal
from domain.rules.part_sale_price import calculate_part_sale_price


def create_final_quote(
    quote_id: str,
    proposal: RawProposal,
    selection: HumanSelection,
    created_at: datetime,
) -> FinalQuote:
    part_sale_price = calculate_part_sale_price(selection)

    total_price = (
        proposal.labour_cost.total_price
        + part_sale_price.sale_price
    )

    return FinalQuote(
        quote_id=quote_id,
        proposal_id=proposal.proposal_id,
        vehicle=proposal.vehicle,
        work_item=proposal.work_item,
        labour_cost=proposal.labour_cost,
        part_selection=selection,
        part_sale_price=part_sale_price,
        total_price=total_price,
        created_at=created_at,
    )
