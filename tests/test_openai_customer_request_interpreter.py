from domain.models.customer_request import InterpretationStatus
from domain.models.enums import Axle
from domain.models.work_item import Operation
from integrations.ai.openai_customer_request_interpreter import (
    OpenAICustomerRequestInterpreter,
)


class FakeOpenAIClient:
    def interpret_customer_request(
        self,
        message: str,
    ) -> dict:
        return {
            "registration": "ABC-123",
            "operation": "BRAKE_REPLACEMENT",
            "axle": "FRONT",
            "part_types": ["brake_pad"],
            "diagnosis_required": False,
            "status": "READY",
        }


def test_openai_interpreter_returns_validated_domain_model():
    interpreter = OpenAICustomerRequestInterpreter(
        client=FakeOpenAIClient(),
    )

    result = interpreter.interpret(
        "ABC-123, etujarrupalat pitäisi vaihtaa"
    )

    assert result.registration == "ABC-123"
    assert result.operation == Operation.BRAKE_REPLACEMENT
    assert result.axle == Axle.FRONT
    assert result.part_types == ["brake_pad"]
    assert result.diagnosis_required is False
    assert result.status == InterpretationStatus.READY


class InvalidReadyOpenAIClient:
    def interpret_customer_request(
        self,
        message: str,
    ) -> dict:
        return {
            "registration": "ABC-123",
            "operation": None,
            "axle": "FRONT",
            "part_types": ["brake_pad"],
            "diagnosis_required": False,
            "status": "READY",
        }


def test_openai_interpreter_rejects_invalid_ready_result():
    interpreter = OpenAICustomerRequestInterpreter(
        client=InvalidReadyOpenAIClient(),
    )

    try:
        interpreter.interpret(
            "ABC-123, autosta kuuluu ääntä edestä"
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "Invalid READY interpretation must be rejected"
        )

