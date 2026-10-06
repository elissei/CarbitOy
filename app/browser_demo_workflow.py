from datetime import datetime, timezone
from uuid import uuid4

from app.demo_data import (
    DEMO_REGISTRATION,
    get_demo_labour_time,
    get_demo_offers,
    get_demo_products,
    get_demo_vehicle,
)
from app.demo_intake_workflow import (
    DemoCustomerMessageResult,
    process_demo_customer_message,
)
from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.work_item import Operation
from integrations.ai.customer_request_interpreter import (
    CustomerRequestInterpreter,
)


class DemoScopeInterpreter:
    def __init__(self, interpreter: CustomerRequestInterpreter):
        self.interpreter = interpreter

    def interpret(self, message: str) -> CustomerRequestInterpretation:
        interpretation = self.interpreter.interpret(message)

        if interpretation.status == InterpretationStatus.HUMAN_REQUIRED:
            return interpretation

        supported = (
            interpretation.registration == DEMO_REGISTRATION
            and interpretation.operation == Operation.BRAKE_REPLACEMENT
            and interpretation.axle == Axle.FRONT
            and interpretation.part_types == ["brake_pad"]
        )

        if supported:
            return interpretation

        data = interpretation.model_dump()
        data["status"] = InterpretationStatus.HUMAN_REQUIRED
        return CustomerRequestInterpretation.model_validate(data)


def process_browser_demo_message(
    message: str,
    interpreter: CustomerRequestInterpreter,
) -> DemoCustomerMessageResult:
    return process_demo_customer_message(
        message=message,
        interpreter=DemoScopeInterpreter(interpreter),
        proposal_id=f"proposal-{uuid4().hex}",
        vehicle=get_demo_vehicle(),
        labour_time=get_demo_labour_time(),
        products=get_demo_products(),
        offers=get_demo_offers(),
        created_at=datetime.now(timezone.utc),
    )
