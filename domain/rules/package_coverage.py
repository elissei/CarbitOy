from pydantic import BaseModel

from domain.models.indexed_product import IndexedProduct
from domain.models.part_requirement import PartRequirement
from domain.rules.product_matching import is_product_type_match
from domain.rules.unit_matching import is_unit_compatible


class PackageCoverageResult(BaseModel):
    covered: bool
    commercial_quantity_needed: int
    covered_quantity: int


def calculate_package_coverage(
    required_quantity: int,
    package_quantity: int,
) -> PackageCoverageResult:
    if required_quantity <= 0:
        raise ValueError("required_quantity must be greater than 0")

    if package_quantity <= 0:
        raise ValueError("package_quantity must be greater than 0")

    commercial_quantity_needed = (
        required_quantity + package_quantity - 1
    ) // package_quantity

    covered_quantity = commercial_quantity_needed * package_quantity

    return PackageCoverageResult(
        covered=covered_quantity >= required_quantity,
        commercial_quantity_needed=commercial_quantity_needed,
        covered_quantity=covered_quantity,
    )


def calculate_product_coverage(
    requirement: PartRequirement,
    product: IndexedProduct,
) -> PackageCoverageResult:
    if not is_product_type_match(requirement, product):
        return PackageCoverageResult(
            covered=False,
            commercial_quantity_needed=0,
            covered_quantity=0,
        )

    if not is_unit_compatible(
        requirement.required_unit,
        product.package_unit,
    ):
        return PackageCoverageResult(
            covered=False,
            commercial_quantity_needed=0,
            covered_quantity=0,
        )

    return calculate_package_coverage(
        required_quantity=requirement.required_quantity,
        package_quantity=product.package_quantity,
    )
