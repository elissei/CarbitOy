from dataclasses import dataclass

from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.work_item import WorkItem
from domain.rules.customer_request_work_item import (
    build_work_item_from_customer_request,
)
from integrations.ai.customer_request_interpreter import (
    CustomerRequestInterpreter,
)


@dataclass
class CustomerIntakeResult:
    interpretation: CustomerRequestInterpretation
    work_item: WorkItem | None


def process_customer_message(
    message: str,
    interpreter: CustomerRequestInterpreter,
) -> CustomerIntakeResult:
    interpretation = interpreter.interpret(message)

    if interpretation.status == InterpretationStatus.HUMAN_REQUIRED:
        return CustomerIntakeResult(
            interpretation=interpretation,
            work_item=None,
        )

    work_item = build_work_item_from_customer_request(
        interpretation
    )

    return CustomerIntakeResult(
        interpretation=interpretation,
        work_item=work_item,
    )
