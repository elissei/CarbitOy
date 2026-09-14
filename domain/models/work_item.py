from enum import Enum

from pydantic import BaseModel

from domain.models.part_requirement import Axle


class Operation(str, Enum):
    BRAKE_REPLACEMENT = "BRAKE_REPLACEMENT"


class WorkItem(BaseModel):
    operation: Operation
    axle: Axle
