from decimal import Decimal

from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.rules.labour_cost import calculate_labour_cost


def test_calculate_labour_cost():
    labour_time = LabourTime(
        hours=Decimal("0.8"),
        source=LabourTimeSource.AUTODATA,
    )

    labour_cost = calculate_labour_cost(
        labour_time=labour_time,
    )

    assert labour_cost.hours == Decimal("0.8")
    assert labour_cost.hourly_rate == Decimal("90.00")
    assert labour_cost.total_price == Decimal("72.000")
    assert labour_cost.currency == "EUR"


def test_half_hour_labour_cost():
    labour_time = LabourTime(
        hours=Decimal("0.5"),
        source=LabourTimeSource.AUTODATA,
    )

    labour_cost = calculate_labour_cost(
        labour_time=labour_time,
    )

    assert labour_cost.hours == Decimal("0.5")
    assert labour_cost.hourly_rate == Decimal("90.00")
    assert labour_cost.total_price == Decimal("45.000")
    assert labour_cost.currency == "EUR"