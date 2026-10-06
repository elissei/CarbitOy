from dataclasses import dataclass
from datetime import datetime

from app.customer_intake_workflow import (
    CustomerIntakeResult,
    process_customer_message,
)
from app.demo_workflow import create_demo_proposal
from domain.models.indexed_product import IndexedProduct
from domain.models.labour_time import LabourTime
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_offer import SupplierOffer
from domain.models.vehicle import Vehicle
from integrations.ai.customer_request_interpreter import (
    CustomerRequestInterpreter,
)


@dataclass
class DemoCustomerMessageResult:
    intake: CustomerIntakeResult
    proposal: RawProposal | None


def process_demo_customer_message(
    message: str,
    interpreter: CustomerRequestInterpreter,
    proposal_id: str | None = None,
    vehicle: Vehicle | None = None,
    labour_time: LabourTime | None = None,
    products: list[IndexedProduct] | None = None,
    offers: list[SupplierOffer] | None = None,
    created_at: datetime | None = None,
) -> DemoCustomerMessageResult:
    intake = process_customer_message(
        message=message,
        interpreter=interpreter,
    )

    if intake.work_item is None:
        return DemoCustomerMessageResult(
            intake=intake,
            proposal=None,
        )

    if (
        proposal_id is None
        or vehicle is None
        or labour_time is None
        or products is None
        or offers is None
        or created_at is None
    ):
        raise ValueError(
            "READY demo workflow requires proposal data"
        )

    proposal = create_demo_proposal(
        proposal_id=proposal_id,
        vehicle=vehicle,
        work_item=intake.work_item,
        labour_time=labour_time,
        products=products,
        offers=offers,
        created_at=created_at,
    )

    return DemoCustomerMessageResult(
        intake=intake,
        proposal=proposal,
    )
