from domain.rules.package_coverage import calculate_package_coverage


def test_exact_package_coverage():
    result = calculate_package_coverage(
        required_quantity=4,
        package_quantity=4,
    )

    assert result.covered is True
    assert result.commercial_quantity_needed == 1
    assert result.covered_quantity == 4


def test_multiple_packages_are_needed():
    result = calculate_package_coverage(
        required_quantity=4,
        package_quantity=2,
    )

    assert result.covered is True
    assert result.commercial_quantity_needed == 2
    assert result.covered_quantity == 4


def test_package_overage_is_calculated():
    result = calculate_package_coverage(
        required_quantity=5,
        package_quantity=2,
    )

    assert result.covered is True
    assert result.commercial_quantity_needed == 3
    assert result.covered_quantity == 6

from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import Axle, PartRequirement, RequirementUnit
from domain.rules.package_coverage import calculate_product_coverage


def test_product_coverage_uses_requirement_and_product():
    requirement = PartRequirement(
        canonical_product_type="spark_plug",
        axle=Axle.UNKNOWN,
        required_quantity=5,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="SUPPLIER-123",
        supplier="SUPPLIER",
        supplier_product_id="123",
        canonical_product_type="spark_plug",
        axle=Axle.UNKNOWN,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.PACKAGE,
    )

    result = calculate_product_coverage(
        requirement=requirement,
        product=product,
    )

    assert result.covered is True
    assert result.commercial_quantity_needed == 3
    assert result.covered_quantity == 6

from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import Axle, PartRequirement, RequirementUnit
from domain.rules.package_coverage import calculate_product_coverage

def test_different_product_type_is_not_accepted_for_coverage():
    requirement = PartRequirement(
        canonical_product_type="spark_plug",
        axle=Axle.UNKNOWN,
        required_quantity=4,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="SUPPLIER-BRAKE-123",
        supplier="SUPPLIER",
        supplier_product_id="BRAKE-123",
        canonical_product_type="brake_pad",
        axle=Axle.UNKNOWN,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    result = calculate_product_coverage(
        requirement=requirement,
        product=product,
    )

    assert result.covered is False

def test_incompatible_units_are_not_accepted_for_coverage():
    requirement = PartRequirement(
        canonical_product_type="spark_plug",
        axle=Axle.UNKNOWN,
        required_quantity=4,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="SUPPLIER-SPARK-SET",
        supplier="SUPPLIER",
        supplier_product_id="SPARK-SET",
        canonical_product_type="spark_plug",
        axle=Axle.UNKNOWN,
        package_quantity=2,
        package_unit=Unit.SET,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    result = calculate_product_coverage(
        requirement=requirement,
        product=product,
    )

    assert result.covered is False

def test_different_axle_is_not_accepted_for_coverage():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=1,
        required_unit=RequirementUnit.SET,
    )

    product = IndexedProduct(
        product_id="SUPPLIER-REAR-BRAKE",
        supplier="SUPPLIER",
        supplier_product_id="REAR-BRAKE",
        canonical_product_type="brake_pad",
        axle=Axle.REAR,
        package_quantity=2,
        package_unit=Unit.SET,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    result = calculate_product_coverage(
        requirement=requirement,
        product=product,
    )

    assert result.covered is False
