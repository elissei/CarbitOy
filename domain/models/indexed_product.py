from enum import Enum

from pydantic import BaseModel

from domain.models.enums import Axle


class Unit(str, Enum):
    PCS = "PCS"
    SET = "SET"
    PAIR = "PAIR"
    PACKAGE = "PACKAGE"
    L = "L"
    KG = "KG"


class IndexedProduct(BaseModel):
    product_id: str
    supplier: str
    supplier_product_id: str

    canonical_product_type: str
    axle: Axle

    package_quantity: int
    package_unit: Unit

    commercial_quantity: int
    commercial_unit: Unit
