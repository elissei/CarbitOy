from decimal import Decimal

from domain.models.supplier_offer import Availability, SupplierOffer


def test_supplier_offer_can_be_created():
    offer = SupplierOffer(
	product_id="MOTONET-123456",
        supplier="Motonet",
        supplier_product_id="123456",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    assert offer.supplier == "Motonet"
    assert offer.supplier_product_id == "123456"
    assert offer.unit_price == Decimal("49.90")
    assert offer.currency == "EUR"
    assert offer.availability == Availability.IN_STOCK


def test_supplier_offer_supports_backorder():
    offer = SupplierOffer(
	product_id="SUPPLIER-ABC-123",
        supplier="Supplier",
        supplier_product_id="ABC-123",
        unit_price=Decimal("25.50"),
        currency="EUR",
        availability=Availability.BACKORDER,
    )

    assert offer.availability == Availability.BACKORDER