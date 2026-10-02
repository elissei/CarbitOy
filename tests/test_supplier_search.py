from decimal import Decimal

from domain.models.supplier_offer import Availability, SupplierOffer
from integrations.suppliers.fake_adapter import FakeSupplierAdapter
from integrations.suppliers.search import search_suppliers


def test_search_suppliers_combines_offers_from_multiple_adapters():
    offer_a = SupplierOffer(
        product_id="BRAKE-PAD-001",
        supplier="SUPPLIER_A",
        supplier_product_id="A-123",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    offer_b = SupplierOffer(
        product_id="BRAKE-PAD-001",
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

    results = search_suppliers(
        adapters=adapters,
        query="brake pads",
    )

    assert results == [offer_a, offer_b]
