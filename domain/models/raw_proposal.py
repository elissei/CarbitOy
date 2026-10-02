from datetime import datetime

from pydantic import BaseModel, Field

from domain.models.supplier_candidate import SupplierCandidate


class RawProposal(BaseModel):
    proposal_id: str
    candidates: list[SupplierCandidate] = Field(default_factory=list)
    created_at: datetime
