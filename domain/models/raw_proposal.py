from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.models.labour_cost import LabourCost
from domain.models.labour_time import LabourTime
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem


class RawProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    vehicle: Vehicle
    work_item: WorkItem
    labour_time: LabourTime
    labour_cost: LabourCost
    candidates: list[SupplierCandidate] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def validate_labour_hours(self) -> "RawProposal":
        if self.labour_time.hours != self.labour_cost.hours:
            raise ValueError(
                "labour_time.hours must match labour_cost.hours"
            )

        return self