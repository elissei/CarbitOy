from enum import Enum

from pydantic import BaseModel, Field

from domain.models.enums import Axle, Side
from domain.models.part_requirement import PartRequirement


class Operation(str, Enum):
    BRAKE_REPLACEMENT = "BRAKE_REPLACEMENT"
    SHOCK_ABSORBER_REPLACEMENT = "SHOCK_ABSORBER_REPLACEMENT"


class WorkItem(BaseModel):
    operation: Operation
    axle: Axle
    side: Side = Side.UNKNOWN
    part_requirements: list[PartRequirement] = Field(default_factory=list)
