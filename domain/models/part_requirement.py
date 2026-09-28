from enum import Enum

from pydantic import BaseModel

from domain.models.enums import Axle


class RequirementUnit(str, Enum):
    PCS = "PCS"
    SET = "SET"
    PAIR = "PAIR"
    L = "L"
    KG = "KG"


class PartRequirement(BaseModel):
    canonical_product_type: str
    axle: Axle

    required_quantity: int
    required_unit: RequirementUnit
