from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class LabourTimeSource(str, Enum):
    AUTODATA = "AUTODATA"


class LabourTime(BaseModel):
    hours: Decimal = Field(gt=0)
    source: LabourTimeSource
