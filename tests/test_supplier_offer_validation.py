from decimal import Decimal

import pytest

from domain.models.supplier_offer import Availability, SupplierOffer
from domain.rules.supplier_offer_validation import validate_supplier_offer


def create_valid_offer() -> SupplierOffer:
    return SupplierOffer(
        product_id="MOTONET-123456",
        supplier="Motonet",
        supplier_product_id="123456",
        unit_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )


def test_valid_supplier_offer_passes_validation():
    offer = create_valid_offer()

    validate_supplier_offer(offer)


def test_zero_price_is_rejected():
    offer = create_valid_offer()
    offer.unit_price = Decimal("0")

    with pytest.raises(ValueError):
        validate_supplier_offer(offer)


def test_negative_price_is_rejected():
    offer = create_valid_offer()
    offer.unit_price = Decimal("-1.00")

    with pytest.raises(ValueError):
        validate_supplier_offer(offer)


def test_empty_supplier_is_rejected():
    offer = create_valid_offer()
    offer.supplier = "   "

    with pytest.raises(ValueError):
        validate_supplier_offer(offer)