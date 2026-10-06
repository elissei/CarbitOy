from decimal import Decimal, ROUND_HALF_UP

from domain.models.human_selection import HumanSelection
from domain.models.part_sale_price import PartSalePrice


PART_PRICE_MULTIPLIER = Decimal("1.30")
MONEY_QUANTUM = Decimal("0.01")


def calculate_part_sale_price(
    selection: HumanSelection,
) -> PartSalePrice:
    purchase_price = selection.total_price

    if purchase_price <= 0:
        raise ValueError(
            "purchase_price must be greater than 0"
        )

    sale_price = (
        purchase_price * PART_PRICE_MULTIPLIER
    ).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )

    return PartSalePrice(
        purchase_price=purchase_price,
        sale_price=sale_price,
        currency=selection.currency,
    )
