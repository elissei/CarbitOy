from datetime import datetime
from decimal import Decimal

from domain.models.enums import Axle
from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.supplier_offer import Availability, SupplierOffer
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from app.demo_workflow import (
    create_demo_final_quote,
    create_demo_proposal,
)
from integrations.ai.fake_quote_presenter import FakeQuotePresenter
from presentation.quote_workflow import present_final_quote


def test_create_demo_proposal_builds_real_domain_pipeline():
    vehicle = Vehicle(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        year=2020,
    )

    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=2,
        required_unit=RequirementUnit.PCS,
    )

    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_requirements=[requirement],
    )

    labour_time = LabourTime(
        hours=Decimal("0.8"),
        source=LabourTimeSource.AUTODATA,
    )

    products = [
        IndexedProduct(
            product_id="PAD-001",
            supplier="DemoSupplier",
            supplier_product_id="DS-001",
            canonical_product_type="brake_pad",
            axle=Axle.FRONT,
            package_quantity=2,
            package_unit=Unit.PCS,
            commercial_quantity=1,
            commercial_unit=Unit.SET,
        )
    ]

    offers = [
        SupplierOffer(
            product_id="PAD-001",
            supplier="DemoSupplier",
            supplier_product_id="DS-001",
            unit_price=Decimal("40.00"),
            currency="EUR",
            availability=Availability.IN_STOCK,
        )
    ]

    proposal = create_demo_proposal(
        proposal_id="proposal-001",
        vehicle=vehicle,
        work_item=work_item,
        labour_time=labour_time,
        products=products,
        offers=offers,
        created_at=datetime(2026, 10, 6, 3, 0),
    )

    assert proposal.proposal_id == "proposal-001"
    assert proposal.labour_cost.hours == Decimal("0.8")
    assert proposal.labour_cost.hourly_rate == Decimal("90.00")
    assert proposal.labour_cost.total_price == Decimal("72.000")
    assert len(proposal.candidates) == 1
    assert proposal.candidates[0].supplier == "DemoSupplier"

    selected_candidate = proposal.candidates[0]

    quote = create_demo_final_quote(
        quote_id="quote-001",
        proposal=proposal,
        selected_candidate=selected_candidate,
        selected_by="demo-user",
        selected_at=datetime(2026, 10, 6, 3, 5),
        created_at=datetime(2026, 10, 6, 3, 6),
    )

    assert quote.quote_id == "quote-001"
    assert quote.proposal_id == "proposal-001"
    assert quote.part_selection.supplier == "DemoSupplier"
    assert quote.part_selection.selected_by == "demo-user"
    assert quote.labour_cost.total_price == Decimal("72.000")
    assert quote.part_sale_price.purchase_price == Decimal("40.00")
    assert quote.part_sale_price.sale_price == Decimal("52.00")
    assert quote.total_price == Decimal("124.000")

    presenter = FakeQuotePresenter(
        work_description="Etujarrupalojen vaihto",
    )

    presentation = present_final_quote(
        quote=quote,
        presenter=presenter,
    )

    assert presentation.work_description == "Etujarrupalojen vaihto"


def test_create_demo_proposal_rejects_multiple_part_requirements():
    vehicle = Vehicle(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        year=2020,
    )

    requirements = [
        PartRequirement(
            canonical_product_type="brake_pad",
            axle=Axle.FRONT,
            required_quantity=2,
            required_unit=RequirementUnit.PCS,
        ),
        PartRequirement(
            canonical_product_type="brake_disc",
            axle=Axle.FRONT,
            required_quantity=2,
            required_unit=RequirementUnit.PCS,
        ),
    ]

    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_requirements=requirements,
    )

    labour_time = LabourTime(
        hours=Decimal("0.8"),
        source=LabourTimeSource.AUTODATA,
    )

    try:
        create_demo_proposal(
            proposal_id="proposal-001",
            vehicle=vehicle,
            work_item=work_item,
            labour_time=labour_time,
            products=[],
            offers=[],
            created_at=datetime(2026, 10, 6, 3, 0),
        )
    except ValueError as error:
        assert str(error) == (
            "demo workflow currently requires exactly one part requirement"
        )
    else:
        raise AssertionError("Expected ValueError")