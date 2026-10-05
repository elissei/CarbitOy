from decimal import Decimal
from unittest.mock import Mock

from domain.models.quote_presentation import QuotePresentationInput
from integrations.ai.openai_client import OpenAIQuotePresentationClient


def test_openai_client_returns_work_description():
    presentation_input = QuotePresentationInput(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        operation="BRAKE_REPLACEMENT",
        axle="FRONT",
        part_types=["brake_pad", "brake_disc"],
        labour_hours=Decimal("0.8"),
        labour_price=Decimal("72.00"),
        parts_price=Decimal("58.37"),
        total_price=Decimal("130.37"),
        currency="EUR",
        prices_include_vat=True,
    )

    client = OpenAIQuotePresentationClient.__new__(
        OpenAIQuotePresentationClient
    )
    client.model = "test-model"
    client.client = Mock()

    response = Mock()
    response.output_text = "Etujarrulevyjen ja -palojen vaihto"

    client.client.responses.create.return_value = response

    result = client.create_work_description(
        presentation_input
    )

    assert result == "Etujarrulevyjen ja -palojen vaihto"

    client.client.responses.create.assert_called_once()

def test_openai_client_sends_only_required_work_facts():
    presentation_input = QuotePresentationInput(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        operation="BRAKE_REPLACEMENT",
        axle="FRONT",
        part_types=["brake_pad", "brake_disc"],
        labour_hours=Decimal("0.8"),
        labour_price=Decimal("72.00"),
        parts_price=Decimal("58.37"),
        total_price=Decimal("130.37"),
        currency="EUR",
        prices_include_vat=True,
    )

    client = OpenAIQuotePresentationClient.__new__(
        OpenAIQuotePresentationClient
    )
    client.model = "test-model"
    client.client = Mock()

    response = Mock()
    response.output_text = "Etujarrulevyjen ja -palojen vaihto"
    client.client.responses.create.return_value = response

    client.create_work_description(presentation_input)

    call_kwargs = client.client.responses.create.call_args.kwargs
    sent_input = call_kwargs["input"]

    assert "BRAKE_REPLACEMENT" in sent_input
    assert "FRONT" in sent_input
    assert "brake_pad" in sent_input
    assert "brake_disc" in sent_input

    assert "130.37" not in sent_input
    assert "58.37" not in sent_input
    assert "72.00" not in sent_input
    assert "0.8" not in sent_input
    assert "ABC-123" not in sent_input
