from decimal import Decimal

from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import Availability
from domain.rules.candidate_sorting import sort_by_total_price


def test_candidates_are_sorted_by_total_price():
    expensive = SupplierCandidate(
        product_id="EXPENSIVE",
        supplier="Supplier A",
        supplier_product_id="A",
        commercial_quantity_needed=1,
        unit_price=Decimal("60.00"),
        total_price=Decimal("60.00"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    cheap = SupplierCandidate(
        product_id="CHEAP",
        supplier="Supplier B",
        supplier_product_id="B",
        commercial_quantity_needed=1,
        unit_price=Decimal("40.00"),
        total_price=Decimal("40.00"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    medium = SupplierCandidate(
        product_id="MEDIUM",
        supplier="Supplier C",
        supplier_product_id="C",
        commercial_quantity_needed=1,
        unit_price=Decimal("50.00"),
        total_price=Decimal("50.00"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    result = sort_by_total_price(
        [expensive, cheap, medium]
    )

    assert [candidate.supplier for candidate in result] == [
        "Supplier B",
        "Supplier C",
        "Supplier A",
    ]
