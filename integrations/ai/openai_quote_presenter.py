from typing import Protocol

from domain.models.quote_presentation import QuotePresentationInput
from domain.models.quote_presentation_output import QuotePresentationOutput
from integrations.ai.quote_presenter import QuotePresenter


class QuotePresentationClient(Protocol):
    def create_work_description(
        self,
        presentation_input: QuotePresentationInput,
    ) -> str:
        ...


class OpenAIQuotePresenter(QuotePresenter):
    def __init__(
        self,
        client: QuotePresentationClient,
    ) -> None:
        self.client = client

    def present(
        self,
        presentation_input: QuotePresentationInput,
    ) -> QuotePresentationOutput:
        work_description = self.client.create_work_description(
            presentation_input
        )

        return QuotePresentationOutput(
            work_description=work_description,
        )
