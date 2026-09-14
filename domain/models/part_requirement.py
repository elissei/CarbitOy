from enum import Enum

from pydantic import BaseModel


class Axle(str, Enum):
    FRONT = "FRONT"
    REAR = "REAR"
    BOTH = "BOTH"
    UNKNOWN = "UNKNOWN"


class PartRequirement(BaseModel):
    canonical_product_type: str
    axle: Axle

    required_quantity: int
    required_unit: str
