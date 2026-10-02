from decimal import Decimal

from domain.models.enums import Axle
from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.supplier_offer import Availability, SupplierOffer
from domain.rules.candidate_sorting import sort_by_total_price
from domain.rules.supplier_candidates import build_supplier_candidates
from integrations.suppliers.fake_adapter import FakeSupplierAdapter
from integrations.suppliers.search import search_suppliers


def test_supplier_search_pipeline_for_front_brake_pads():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    product = IndexedProduct(
        product_id="BRAKE-PAD-001",
        supplier="NORMALIZED",
        supplier_product_id="PAD-001",
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        package_quantity=2,
        package_unit=Unit.PCS,
        commercial_quantity=1,
        commercial_unit=Unit.SET,
    )

    offer_a = SupplierOffer(
        product_id=product.product_id,
        supplier="SUPPLIER_A",
        supplier_product_id="A-123",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    offer_b = SupplierOffer(
        product_id=product.product_id,
        supplier="SUPPLIER_B",
        supplier_product_id="B-456",
        unit_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    adapters = [
        FakeSupplierAdapter([offer_a]),
        FakeSupplierAdapter([offer_b]),
    ]

    offers = search_suppliers(
        adapters=adapters,
        query="front brake pads",
    )

    candidates = build_supplier_candidates(
        requirement=requirement,
        products=[product],
        offers=offers,
    )

    sorted_candidates = sort_by_total_price(candidates)

    assert len(sorted_candidates) == 2
    assert sorted_candidates[0].supplier == "SUPPLIER_B"
    assert sorted_candidates[0].commercial_quantity_needed == 1
    assert sorted_candidates[0].total_price == Decimal("44.90")
    assert sorted_candidates[1].supplier == "SUPPLIER_A"
    assert sorted_candidates[1].total_price == Decimal("49.90")
