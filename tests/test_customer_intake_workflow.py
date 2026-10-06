from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.work_item import Operation
from integrations.ai.fake_customer_request_interpreter import (
    FakeCustomerRequestInterpreter,
)
from app.customer_intake_workflow import process_customer_message


def test_ready_message_returns_interpretation_and_work_item():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["brake_pad"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    interpreter = FakeCustomerRequestInterpreter(
        interpretation=interpretation,
    )

    result = process_customer_message(
        message="ABC-123, etujarrupalat pitäisi vaihtaa",
        interpreter=interpreter,
    )

    assert result.interpretation == interpretation
    assert result.work_item is not None
    assert result.work_item.operation == Operation.BRAKE_REPLACEMENT
    assert result.work_item.axle == Axle.FRONT


def test_human_required_message_stops_before_work_item():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=None,
        axle=Axle.FRONT,
        part_types=[],
        diagnosis_required=True,
        status=InterpretationStatus.HUMAN_REQUIRED,
    )

    interpreter = FakeCustomerRequestInterpreter(
        interpretation=interpretation,
    )

    result = process_customer_message(
        message="ABC-123, autosta kuuluu outoa ääntä edestä",
        interpreter=interpreter,
    )

    assert result.interpretation == interpretation
    assert result.work_item is None
