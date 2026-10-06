from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class QuotePresentationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration: str
    make: str
    model: str

    operation: str
    axle: str
    part_types: list[str] = Field(default_factory=list)

    labour_hours: Decimal
    labour_price: Decimal
    parts_price: Decimal
    total_price: Decimal
    currency: str

    prices_include_vat: bool
