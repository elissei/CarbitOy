import json

from integrations.ai.openai_customer_request_client import (
    OpenAICustomerRequestClient,
)


class FakeResponse:
    output_text = json.dumps(
        {
            "registration": "ABC-123",
            "operation": "BRAKE_REPLACEMENT",
            "axle": "FRONT",
            "part_types": ["brake_pad"],
            "diagnosis_required": False,
            "status": "READY",
        }
    )


class FakeResponses:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeResponse()


class FakeOpenAI:
    def __init__(self):
        self.responses = FakeResponses()


def test_openai_customer_request_client_returns_structured_data():
    client = OpenAICustomerRequestClient()
    fake_openai = FakeOpenAI()
    client.client = fake_openai

    result = client.interpret_customer_request(
        "ABC-123, etujarrupalat pitäisi vaihtaa"
    )

    assert result == {
        "registration": "ABC-123",
        "operation": "BRAKE_REPLACEMENT",
        "axle": "FRONT",
        "part_types": ["brake_pad"],
        "diagnosis_required": False,
        "status": "READY",
    }

    assert (
        fake_openai.responses.kwargs["input"]
        == "ABC-123, etujarrupalat pitäisi vaihtaa"
    )

    instructions = fake_openai.responses.kwargs["instructions"]

    assert "Älä diagnosoi" in instructions
    assert "HUMAN_REQUIRED" in instructions
    assert "Älä keksi" in instructions
    assert "diagnosis_required" in instructions
    assert "oiretta" in instructions
    assert "Diagnoositarpeesta ei saa päätellä" in instructions