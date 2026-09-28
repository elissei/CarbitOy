from domain.models.supplier_offer import Availability
from domain.rules.availability import is_available


def test_in_stock_is_available():
    assert is_available(Availability.IN_STOCK) is True


def test_backorder_is_available():
    assert is_available(Availability.BACKORDER) is True


def test_out_of_stock_is_not_available():
    assert is_available(Availability.OUT_OF_STOCK) is False


def test_unknown_is_not_available():
    assert is_available(Availability.UNKNOWN) is False