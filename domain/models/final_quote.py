from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, model_validator

from domain.models.human_selection import HumanSelection
from domain.models.labour_cost import LabourCost
from domain.models.part_sale_price import PartSalePrice
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem


class FinalQuote(BaseModel):
    quote_id: str
    proposal_id: str
    vehicle: Vehicle
    work_item: WorkItem
    labour_cost: LabourCost
    part_selection: HumanSelection
    part_sale_price: PartSalePrice
    total_price: Decimal
    created_at: datetime

    @model_validator(mode="after")
    def validate_selection_proposal(self) -> "FinalQuote":
        if self.part_selection.proposal_id != self.proposal_id:
            raise ValueError(
                "part_selection.proposal_id must match proposal_id"
            )

        return self

    @model_validator(mode="after")
    def validate_total_price(self) -> "FinalQuote":
        expected_total = (
            self.labour_cost.total_price
            + self.part_sale_price.sale_price
        )

        if self.total_price != expected_total:
            raise ValueError(
                "total_price must equal labour cost plus part sale price"
            )

        return self
