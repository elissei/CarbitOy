from enum import Enum

from pydantic import BaseModel, Field

from domain.models.enums import Axle
from domain.models.part_requirement import PartRequirement


class Operation(str, Enum):
    BRAKE_REPLACEMENT = "BRAKE_REPLACEMENT"


class WorkItem(BaseModel):
    operation: Operation
    axle: Axle
    part_requirements: list[PartRequirement] = Field(default_factory=list)
