from datetime import datetime

from app.customer_intake_workflow import (
    CustomerIntakeResult,
)
from app.demo_workflow import create_demo_proposal
from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.indexed_product import (
    IndexedProduct,
    Unit,
)
from domain.models.labour_time import (
    LabourTime,
    LabourTimeSource,
)
from domain.models.supplier_offer import (
    Availability,
    SupplierOffer,
)
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation
from domain.rules.customer_request_work_item import (
    build_work_item_from_customer_request,
)


def test_ready_intake_can_create_demo_proposal():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["brake_pad"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    work_item = build_work_item_from_customer_request(
        interpretation
    )

    intake = CustomerIntakeResult(
        interpretation=interpretation,
        work_item=work_item,
    )

    vehicle = Vehicle(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        year=2020,
    )

    labour_time = LabourTime(
        hours=0.8,
        source=LabourTimeSource.AUTODATA,
    )

    products = [
        IndexedProduct(
            product_id="PAD-1",
            supplier="DemoSupplier",
            supplier_product_id="DS-123",
            canonical_product_type="brake_pad",
            axle=Axle.FRONT,
            package_quantity=1,
            package_unit=Unit.SET,
            commercial_quantity=1,
            commercial_unit=Unit.SET,
        )
    ]

    offers = [
        SupplierOffer(
            product_id="PAD-1",
            supplier="DemoSupplier",
            supplier_product_id="DS-123",
            unit_price="40.00",
            currency="EUR",
            availability=Availability.IN_STOCK,
        )
    ]

    assert intake.work_item is not None

    proposal = create_demo_proposal(
        proposal_id="proposal-1",
        vehicle=vehicle,
        work_item=intake.work_item,
        labour_time=labour_time,
        products=products,
        offers=offers,
        created_at=datetime(
            2026, 10, 6, 12, 0, 0
        ),
    )

    assert proposal.vehicle.registration == "ABC-123"
    assert proposal.work_item == intake.work_item
    assert len(proposal.candidates) == 1
    assert proposal.candidates[0].total_price == 40
