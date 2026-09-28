from decimal import Decimal

from domain.models.enums import Axle
from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.supplier_offer import Availability, SupplierOffer
from domain.rules.supplier_candidates import build_supplier_candidates


def test_build_supplier_candidates_returns_matching_offers():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    product_1 = IndexedProduct(
        product_id="MOTONET-123",
        supplier="Motonet",
        supplier_product_id="123",
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    product_2 = IndexedProduct(
        product_id="OTHER-456",
        supplier="Other",
        supplier_product_id="456",
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    offer_1 = SupplierOffer(
        product_id="MOTONET-123",
        supplier="Motonet",
        supplier_product_id="123",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    offer_2 = SupplierOffer(
        product_id="OTHER-456",
        supplier="Other",
        supplier_product_id="456",
        unit_price=Decimal("55.00"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    candidates = build_supplier_candidates(
        requirement=requirement,
        products=[product_1, product_2],
        offers=[offer_1, offer_2],
    )

    assert len(candidates) == 2
    assert candidates[0].supplier == "Motonet"
    assert candidates[1].supplier == "Other"

def test_build_supplier_candidates_filters_incompatible_products_and_offers():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    valid_product = IndexedProduct(
        product_id="VALID-123",
        supplier="Supplier A",
        supplier_product_id="123",
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    wrong_axle_product = IndexedProduct(
        product_id="REAR-456",
        supplier="Supplier B",
        supplier_product_id="456",
        canonical_product_type="brake_pad",
        axle=Axle.REAR,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    wrong_type_product = IndexedProduct(
        product_id="DISC-789",
        supplier="Supplier C",
        supplier_product_id="789",
        canonical_product_type="brake_disc",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    valid_offer = SupplierOffer(
        product_id="VALID-123",
        supplier="Supplier A",
        supplier_product_id="123",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    wrong_product_offer = SupplierOffer(
        product_id="UNKNOWN-999",
        supplier="Supplier X",
        supplier_product_id="999",
        unit_price=Decimal("39.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    candidates = build_supplier_candidates(
        requirement=requirement,
        products=[
            valid_product,
            wrong_axle_product,
            wrong_type_product,
        ],
        offers=[
            valid_offer,
            wrong_product_offer,
        ],
    )

    assert len(candidates) == 1
    assert candidates[0].product_id == "VALID-123"
    assert candidates[0].supplier == "Supplier A"