from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class HumanSelection(BaseModel):
    proposal_id: str
    product_id: str
    supplier: str
    supplier_product_id: str
    commercial_quantity_needed: int
    unit_price: Decimal
    total_price: Decimal
    currency: str
    selected_by: str
    selected_at: datetime

