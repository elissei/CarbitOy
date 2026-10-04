from decimal import Decimal

from pydantic import BaseModel, Field


class LabourCost(BaseModel):
    hours: Decimal = Field(gt=0)
    hourly_rate: Decimal = Field(gt=0)
    total_price: Decimal = Field(gt=0)
    currency: str