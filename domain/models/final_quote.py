from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, model_validator

from domain.models.human_selection import HumanSelection
from domain.models.labour_cost import LabourCost
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem


class FinalQuote(BaseModel):
    quote_id: str
    proposal_id: str
    vehicle: Vehicle
    work_item: WorkItem
    labour_cost: LabourCost
    part_selection: HumanSelection
    total_price: Decimal
    created_at: datetime

    @model_validator(mode="after")
    def validate_selection_proposal(self) -> "FinalQuote":
        if self.part_selection.proposal_id != self.proposal_id:
            raise ValueError(
                "part_selection.proposal_id must match proposal_id"
            )

        return self