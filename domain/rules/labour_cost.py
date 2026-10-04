from decimal import Decimal

from domain.models.labour_cost import LabourCost
from domain.models.labour_time import LabourTime


LABOUR_HOURLY_RATE = Decimal("90.00")
LABOUR_CURRENCY = "EUR"


def calculate_labour_cost(
    labour_time: LabourTime,
) -> LabourCost:
    total_price = labour_time.hours * LABOUR_HOURLY_RATE

    return LabourCost(
        hours=labour_time.hours,
        hourly_rate=LABOUR_HOURLY_RATE,
        total_price=total_price,
        currency=LABOUR_CURRENCY,
    )