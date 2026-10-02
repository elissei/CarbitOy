from decimal import Decimal

from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import Availability


def test_supplier_candidate_can_be_created():
    candidate = SupplierCandidate(
        product_id="MOTONET-123456",
        supplier="Motonet",
        supplier_product_id="123456",
        commercial_quantity_needed=1,
        unit_price=Decimal("49.90"),
        total_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    assert candidate.product_id == "MOTONET-123456"
    assert candidate.supplier == "Motonet"
    assert candidate.commercial_quantity_needed == 1
    assert candidate.unit_price == Decimal("49.90")
    assert candidate.total_price == Decimal("49.90")
