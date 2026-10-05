from datetime import datetime
from decimal import Decimal

from domain.models.enums import Axle
from domain.models.final_quote import FinalQuote
from domain.models.human_selection import HumanSelection
from domain.models.labour_cost import LabourCost
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.part_sale_price import PartSalePrice
from domain.models.supplier_offer import Availability
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.rules.quote_presentation import build_quote_presentation_input


def test_build_quote_presentation_input():
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
            part_requirements=[
                PartRequirement(
                    canonical_product_type="brake_pad",
                    axle=Axle.FRONT,
                    required_quantity=1,
                    required_unit=RequirementUnit.SET,
                ),
                PartRequirement(
                    canonical_product_type="brake_disc",
                    axle=Axle.FRONT,
                    required_quantity=2,
                    required_unit=RequirementUnit.PCS,
                ),
            ],
        ),
        labour_cost=LabourCost(
            hours=Decimal("0.8"),
            hourly_rate=Decimal("90.00"),
            total_price=Decimal("72.00"),
            currency="EUR",
        ),
        part_selection=HumanSelection(
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
        ),
        part_sale_price=PartSalePrice(
            purchase_price=Decimal("44.90"),
            sale_price=Decimal("58.37"),
            currency="EUR",
        ),
        total_price=Decimal("130.37"),
        created_at=datetime(2026, 10, 5, 12, 5),
    )

    presentation = build_quote_presentation_input(quote)

    assert presentation.registration == "ABC-123"
    assert presentation.make == "Toyota"
    assert presentation.model == "Corolla"
    assert presentation.operation == "BRAKE_REPLACEMENT"
    assert presentation.axle == "FRONT"
    assert presentation.part_types == ["brake_pad", "brake_disc"]

    assert presentation.labour_hours == Decimal("0.8")
    assert presentation.labour_price == Decimal("72.00")
    assert presentation.parts_price == Decimal("58.37")
    assert presentation.total_price == Decimal("130.37")
    assert presentation.currency == "EUR"
    assert presentation.prices_include_vat is True

    assert "brake_fluid" not in presentation.part_types
