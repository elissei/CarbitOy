from decimal import Decimal, ROUND_HALF_UP

from pydantic import BaseModel, Field, model_validator


CENT = Decimal("0.01")


class LabourCost(BaseModel):
    hours: Decimal = Field(gt=0)
    hourly_rate: Decimal = Field(gt=0)
    total_price: Decimal = Field(gt=0)
    currency: str

    @model_validator(mode="after")
    def validate_total_price(self) -> "LabourCost":
        expected_total = (
            self.hours * self.hourly_rate
        ).quantize(CENT, rounding=ROUND_HALF_UP)

        if self.total_price != expected_total:
            raise ValueError(
                "total_price must equal rounded hours * hourly_rate"
            )

        return self
