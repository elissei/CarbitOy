from datetime import datetime
from decimal import Decimal

from domain.models.enums import Axle
from domain.models.human_selection import HumanSelection
from domain.models.labour_cost import LabourCost
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_offer import Availability
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.rules.final_quote import create_final_quote


def test_create_final_quote_calculates_prices_automatically():
    proposal = RawProposal(
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
        labour_time=LabourTime(
            hours=Decimal("0.8"),
            source=LabourTimeSource.AUTODATA,
        ),
        labour_cost=LabourCost(
            hours=Decimal("0.8"),
            hourly_rate=Decimal("90.00"),
            total_price=Decimal("72.00"),
            currency="EUR",
        ),
        candidates=[],
        created_at=datetime(2026, 10, 5, 12, 0),
    )

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

    quote = create_final_quote(
        quote_id="QUOTE-001",
        proposal=proposal,
        selection=selection,
        created_at=datetime(2026, 10, 5, 12, 5),
    )

    assert quote.proposal_id == "PROPOSAL-001"
    assert quote.part_selection == selection
    assert quote.part_sale_price.purchase_price == Decimal("44.90")
    assert quote.part_sale_price.sale_price == Decimal("58.37")
    assert quote.labour_cost.total_price == Decimal("72.00")
    assert quote.total_price == Decimal("130.37")
