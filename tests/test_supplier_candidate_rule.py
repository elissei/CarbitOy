from decimal import Decimal

from domain.models.enums import Axle
from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.supplier_offer import Availability, SupplierOffer
from domain.rules.supplier_candidate import create_supplier_candidate


def test_supplier_candidate_calculates_total_price():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=5,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="MOTONET-123456",
        supplier="Motonet",
        supplier_product_id="123456",
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    offer = SupplierOffer(
        product_id="MOTONET-123456",
        supplier="Motonet",
        supplier_product_id="123456",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    candidate = create_supplier_candidate(
        requirement=requirement,
        product=product,
        offer=offer,
    )

    assert candidate is not None
    assert candidate.commercial_quantity_needed == 3
    assert candidate.unit_price == Decimal("49.90")
    assert candidate.total_price == Decimal("149.70")
    assert candidate.availability == Availability.IN_STOCK

def test_incompatible_product_returns_no_candidate():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="MOTONET-REAR-PAD",
        supplier="Motonet",
        supplier_product_id="REAR-PAD",
        canonical_product_type="brake_pad",
        axle=Axle.REAR,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    offer = SupplierOffer(
        product_id="MOTONET-REAR-PAD",
        supplier="Motonet",
        supplier_product_id="REAR-PAD",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    candidate = create_supplier_candidate(
        requirement=requirement,
        product=product,
        offer=offer,
    )

    assert candidate is None


def test_different_product_type_returns_no_candidate():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="MOTONET-DISC-123",
        supplier="Motonet",
        supplier_product_id="DISC-123",
        canonical_product_type="brake_disc",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    offer = SupplierOffer(
        product_id="MOTONET-DISC-123",
        supplier="Motonet",
        supplier_product_id="DISC-123",
        unit_price=Decimal("79.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    candidate = create_supplier_candidate(
        requirement=requirement,
        product=product,
        offer=offer,
    )

    assert candidate is None
