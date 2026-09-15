from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import Axle, PartRequirement, RequirementUnit
from domain.rules.product_matching import is_product_type_match


def test_matching_product_type_returns_true():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=1,
        required_unit=RequirementUnit.SET,
    )

    product = IndexedProduct(
        product_id="MOTONET-123456",
        supplier="MOTONET",
        supplier_product_id="123456",
        canonical_product_type="brake_pad",
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    assert is_product_type_match(requirement, product) is True


def test_different_product_type_returns_false():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=1,
        required_unit=RequirementUnit.SET,
    )

    product = IndexedProduct(
        product_id="MOTONET-654321",
        supplier="MOTONET",
        supplier_product_id="654321",
        canonical_product_type="brake_disc",
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.PAIR,
    )

    assert is_product_type_match(requirement, product) is False
