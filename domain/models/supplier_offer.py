from decimal import Decimal
from enum import Enum

from pydantic import BaseModel


class Availability(str, Enum):
    IN_STOCK = "IN_STOCK"
    BACKORDER = "BACKORDER"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class SupplierOffer(BaseModel):
    product_id: str

    supplier: str
    supplier_product_id: str

    unit_price: Decimal
    currency: str

    availability: Availability