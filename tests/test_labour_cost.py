from decimal import Decimal

from domain.models.labour_cost import LabourCost


def test_labour_cost():
    labour_cost = LabourCost(
        hours=Decimal("0.8"),
        hourly_rate=Decimal("90.00"),
        total_price=Decimal("72.00"),
        currency="EUR",
    )

    assert labour_cost.hours == Decimal("0.8")
    assert labour_cost.hourly_rate == Decimal("90.00")
    assert labour_cost.total_price == Decimal("72.00")
    assert labour_cost.currency == "EUR"