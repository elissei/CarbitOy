from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import Axle, PartRequirement, RequirementUnit


def test_brake_pad_set_package_and_accounting_quantity():
    product = IndexedProduct(
        product_id="MOTONET-123456",
        supplier="MOTONET",
        supplier_product_id="123456",
        canonical_product_type="brake_pad",
        package_quantity=2,
        package_unit=Unit.PCS,
        accounting_quantity=1,
        accounting_unit=Unit.SET,
    )

    assert product.package_quantity == 2
    assert product.package_unit == Unit.PCS

    assert product.accounting_quantity == 1
    assert product.accounting_unit == Unit.SET


def test_brake_pad_requirement_is_separate_from_supplier_package():
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
        accounting_quantity=1,
        accounting_unit=Unit.SET,
    )

    assert requirement.required_quantity == 1
    assert requirement.required_unit == RequirementUnit.SET

    assert product.package_quantity == 2
    assert product.package_unit == Unit.PCS