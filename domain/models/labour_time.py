from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class LabourTimeSource(str, Enum):
    AUTODATA = "AUTODATA"
    MANUAL_AUTODATA = "MANUAL_AUTODATA"


class LabourTime(BaseModel):
    hours: Decimal = Field(gt=0)
    source: LabourTimeSource
    work_description: str | None = None
    autodata_reference: str | None = None
    recorded_by: str | None = None

    @model_validator(mode="after")
    def validate_manual_source(self) -> "LabourTime":
        if self.source == LabourTimeSource.MANUAL_AUTODATA:
            if not self.work_description or not self.work_description.strip():
                raise ValueError(
                    "Manual Autodata labour time requires work_description"
                )
            if not self.recorded_by or not self.recorded_by.strip():
                raise ValueError(
                    "Manual Autodata labour time requires recorded_by"
                )
        return self
