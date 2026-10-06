from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from domain.models.enums import Axle
from domain.models.work_item import Operation


class InterpretationStatus(str, Enum):
    READY = "READY"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class CustomerRequestInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration: str
    operation: Operation | None
    axle: Axle
    part_types: list[str]
    diagnosis_required: bool
    status: InterpretationStatus

    @model_validator(mode="after")
    def validate_ready_interpretation(
        self,
    ) -> "CustomerRequestInterpretation":
        if self.status == InterpretationStatus.READY:
            if self.operation is None:
                raise ValueError(
                    "READY interpretation requires an operation"
                )

            if not self.part_types:
                raise ValueError(
                    "READY interpretation requires part types"
                )

        return self
