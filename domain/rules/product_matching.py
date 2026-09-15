from domain.models.indexed_product import IndexedProduct
from domain.models.part_requirement import PartRequirement


def is_product_type_match(
    requirement: PartRequirement,
    product: IndexedProduct,
) -> bool:
    return (
        requirement.canonical_product_type
        == product.canonical_product_type
    )
