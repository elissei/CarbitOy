from decimal import Decimal, ROUND_HALF_UP

from domain.models.labour_cost import LabourCost
from domain.models.labour_time import LabourTime


DEFAULT_HOURLY_RATE = Decimal("90.00")
VAT_RATE = Decimal("0.255")
CENT = Decimal("0.01")


def calculate_labour_cost(
    labour_time: LabourTime,
    hourly_rate: Decimal = DEFAULT_HOURLY_RATE,
) -> LabourCost:
    total = (labour_time.hours * hourly_rate).quantize(
        CENT, rounding=ROUND_HALF_UP
    )

    return LabourCost(
        hours=labour_time.hours,
        hourly_rate=hourly_rate,
        total_price=total,
        currency="EUR",
    )


def split_inclusive_vat(total: Decimal) -> tuple[Decimal, Decimal]:
    net = (total / (Decimal("1") + VAT_RATE)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    vat = total - net
    return net, vat
