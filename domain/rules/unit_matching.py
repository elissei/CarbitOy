from domain.models.indexed_product import Unit
from domain.models.part_requirement import RequirementUnit


def is_unit_compatible(
    required_unit: RequirementUnit,
    product_unit: Unit,
) -> bool:
    return required_unit.value == product_unit.value
