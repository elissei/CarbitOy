from datetime import datetime
from decimal import Decimal

from domain.models.human_selection import HumanSelection
from domain.models.supplier_offer import Availability
from domain.rules.part_sale_price import calculate_part_sale_price


def test_calculate_part_sale_price_from_human_selection():
    selection = HumanSelection(
        proposal_id="PROPOSAL-001",
        product_id="BRAKE-PAD-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("44.90"),
        total_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
        selected_by="workshop-owner",
        selected_at=datetime(2026, 10, 5, 12, 0),
    )

    part_price = calculate_part_sale_price(selection)

    assert part_price.purchase_price == Decimal("44.90")
    assert part_price.sale_price == Decimal("58.37")
    assert part_price.currency == "EUR"


def test_calculate_part_sale_price_uses_total_purchase_price():
    selection = HumanSelection(
        proposal_id="PROPOSAL-001",
        product_id="BRAKE-DISC-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-456",
        commercial_quantity_needed=2,
        unit_price=Decimal("50.00"),
        total_price=Decimal("100.00"),
        currency="EUR",
        availability=Availability.IN_STOCK,
        selected_by="workshop-owner",
        selected_at=datetime(2026, 10, 5, 12, 0),
    )

    part_price = calculate_part_sale_price(selection)

    assert part_price.purchase_price == Decimal("100.00")
    assert part_price.sale_price == Decimal("130.00")
