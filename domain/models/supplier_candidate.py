from decimal import Decimal

from pydantic import BaseModel

from domain.models.supplier_offer import Availability


class SupplierCandidate(BaseModel):
    product_id: str
    supplier: str
    supplier_product_id: str
    commercial_quantity_needed: int
    unit_price: Decimal
    total_price: Decimal
    currency: str
    availability: Availability
