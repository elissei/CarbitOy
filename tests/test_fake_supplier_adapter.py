from decimal import Decimal

from domain.models.supplier_offer import Availability, SupplierOffer
from integrations.suppliers.fake_adapter import FakeSupplierAdapter


def test_fake_supplier_adapter_returns_offers():
    offer = SupplierOffer(
        product_id="BRAKE-PAD-001",
        supplier="FAKE_SUPPLIER",
        supplier_product_id="FP-123",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    adapter = FakeSupplierAdapter(offers=[offer])

    results = adapter.search("brake pads")

    assert results == [offer]
