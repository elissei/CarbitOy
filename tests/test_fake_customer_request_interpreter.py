from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.work_item import Operation
from integrations.ai.fake_customer_request_interpreter import (
    FakeCustomerRequestInterpreter,
)


def test_fake_interpreter_returns_configured_interpretation():
    expected = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["brake_pad"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    interpreter = FakeCustomerRequestInterpreter(
        interpretation=expected,
    )

    result = interpreter.interpret(
        "ABC-123, etujarrupalat pitäisi vaihtaa"
    )

    assert result == expected
