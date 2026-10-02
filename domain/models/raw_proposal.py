from datetime import datetime

from pydantic import BaseModel, Field

from domain.models.labour_time import LabourTime
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.vehicle import Vehicle
from domain.models.work_item import WorkItem


class RawProposal(BaseModel):
    proposal_id: str
    vehicle: Vehicle
    work_item: WorkItem
    labour_time: LabourTime
    candidates: list[SupplierCandidate] = Field(default_factory=list)
    created_at: datetime
