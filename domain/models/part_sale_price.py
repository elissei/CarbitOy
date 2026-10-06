from decimal import Decimal

from pydantic import BaseModel, Field


class PartSalePrice(BaseModel):
    purchase_price: Decimal = Field(gt=0)
    sale_price: Decimal = Field(gt=0)
    currency: str
