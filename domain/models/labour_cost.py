from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class LabourCost(BaseModel):
    hours: Decimal = Field(gt=0)
    hourly_rate: Decimal = Field(gt=0)
    total_price: Decimal = Field(gt=0)
    currency: str

    @model_validator(mode="after")
    def validate_total_price(self) -> "LabourCost":
        expected_total = self.hours * self.hourly_rate

        if self.total_price != expected_total:
            raise ValueError(
                "total_price must equal hours * hourly_rate"
            )

        return self
