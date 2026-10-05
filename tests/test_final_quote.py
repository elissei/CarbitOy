from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.models.enums import Axle
from domain.models.final_quote import FinalQuote
from domain.models.human_selection import HumanSelection
from domain.models.labour_cost import LabourCost
from domain.models.supplier_offer import Availability
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.part_sale_price import PartSalePrice


def test_final_quote():
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

    quote = FinalQuote(
        quote_id="QUOTE-001",
        proposal_id="PROPOSAL-001",
        vehicle=Vehicle(
            registration="ABC-123",
            make="Toyota",
            model="Corolla",
            year=2020,
        ),
        work_item=WorkItem(
            operation=Operation.BRAKE_REPLACEMENT,
            axle=Axle.FRONT,
        ),
        labour_cost=LabourCost(
            hours=Decimal("0.8"),
            hourly_rate=Decimal("90.00"),
            total_price=Decimal("72.00"),
            currency="EUR",
        ),
        part_selection=selection,
        total_price=Decimal("130.37"),
        created_at=datetime(2026, 10, 5, 12, 5),
        part_sale_price=PartSalePrice(
            purchase_price=Decimal("44.90"),
            sale_price=Decimal("58.37"),
            currency="EUR",
        ),
    )

    assert quote.quote_id == "QUOTE-001"
    assert quote.proposal_id == "PROPOSAL-001"
    assert quote.part_selection.selected_by == "workshop-owner"
    assert quote.labour_cost.total_price == Decimal("72.00")
    assert quote.part_selection.total_price == Decimal("44.90")
    assert quote.total_price == Decimal("130.37")
    assert quote.part_sale_price.sale_price == Decimal("58.37")


def test_final_quote_rejects_selection_from_different_proposal():
    selection = HumanSelection(
        proposal_id="PROPOSAL-999",
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

    with pytest.raises(ValidationError):
        FinalQuote(
            quote_id="QUOTE-001",
            proposal_id="PROPOSAL-001",
            vehicle=Vehicle(
                registration="ABC-123",
                make="Toyota",
                model="Corolla",
                year=2020,
            ),
            work_item=WorkItem(
                operation=Operation.BRAKE_REPLACEMENT,
                axle=Axle.FRONT,
            ),
            labour_cost=LabourCost(
                hours=Decimal("0.8"),
                hourly_rate=Decimal("90.00"),
                total_price=Decimal("72.00"),
                currency="EUR",
            ),
            part_selection=selection,
            total_price=Decimal("130.37"),
            created_at=datetime(2026, 10, 5, 12, 5),
            part_sale_price=PartSalePrice(
                purchase_price=Decimal("44.90"),
                sale_price=Decimal("58.37"),
                currency="EUR",
            ),
        )


def test_final_quote_rejects_incorrect_total_price():
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

    with pytest.raises(
        ValidationError,
        match="total_price must equal labour cost plus part sale price",
    ):
        FinalQuote(
            quote_id="QUOTE-001",
            proposal_id="PROPOSAL-001",
            vehicle=Vehicle(
                registration="ABC-123",
                make="Toyota",
                model="Corolla",
                year=2020,
            ),
            work_item=WorkItem(
                operation=Operation.BRAKE_REPLACEMENT,
                axle=Axle.FRONT,
            ),
            labour_cost=LabourCost(
                hours=Decimal("0.8"),
                hourly_rate=Decimal("90.00"),
                total_price=Decimal("72.00"),
                currency="EUR",
            ),
            part_selection=selection,
            part_sale_price=PartSalePrice(
                purchase_price=Decimal("44.90"),
                sale_price=Decimal("58.37"),
                currency="EUR",
            ),
            total_price=Decimal("999.99"),
            created_at=datetime(2026, 10, 5, 12, 5),
        )

def test_final_quote_rejects_mismatched_part_purchase_price():
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
        selected_at=datetime(2026, 10, 5, 12, 2),
    )

    with pytest.raises(
        ValueError,
        match=(
            "part_sale_price.purchase_price must match "
            "part_selection.total_price"
        ),
    ):
        FinalQuote(
            quote_id="QUOTE-001",
            proposal_id="PROPOSAL-001",
            vehicle=Vehicle(
                registration="ABC-123",
                make="Toyota",
                model="Corolla",
                year=2020,
            ),
            work_item=WorkItem(
                operation=Operation.BRAKE_REPLACEMENT,
                axle=Axle.FRONT,
            ),
            labour_cost=LabourCost(
                hours=Decimal("0.8"),
                hourly_rate=Decimal("90.00"),
                total_price=Decimal("72.00"),
                currency="EUR",
            ),
            part_selection=selection,
            part_sale_price=PartSalePrice(
                purchase_price=Decimal("99.00"),
                sale_price=Decimal("128.70"),
                currency="EUR",
            ),
            total_price=Decimal("200.70"),
            created_at=datetime(2026, 10, 5, 12, 5),
        )

def test_final_quote_rejects_mismatched_currencies():
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
        selected_at=datetime(2026, 10, 5, 12, 2),
    )

    with pytest.raises(
        ValueError,
        match=(
            "labour cost, part selection, and part sale price "
            "must use the same currency"
        ),
    ):
        FinalQuote(
            quote_id="QUOTE-001",
            proposal_id="PROPOSAL-001",
            vehicle=Vehicle(
                registration="ABC-123",
                make="Toyota",
                model="Corolla",
                year=2020,
            ),
            work_item=WorkItem(
                operation=Operation.BRAKE_REPLACEMENT,
                axle=Axle.FRONT,
            ),
            labour_cost=LabourCost(
                hours=Decimal("0.8"),
                hourly_rate=Decimal("90.00"),
                total_price=Decimal("72.00"),
                currency="EUR",
            ),
            part_selection=selection,
            part_sale_price=PartSalePrice(
                purchase_price=Decimal("44.90"),
                sale_price=Decimal("58.37"),
                currency="USD",
            ),
            total_price=Decimal("130.37"),
            created_at=datetime(2026, 10, 5, 12, 5),
        )
