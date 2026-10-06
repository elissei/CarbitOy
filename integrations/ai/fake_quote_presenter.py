from domain.models.quote_presentation import QuotePresentationInput
from domain.models.quote_presentation_output import QuotePresentationOutput
from integrations.ai.quote_presenter import QuotePresenter


class FakeQuotePresenter(QuotePresenter):
    def __init__(self, work_description: str) -> None:
        self.work_description = work_description

    def present(
        self,
        presentation_input: QuotePresentationInput,
    ) -> QuotePresentationOutput:
        return QuotePresentationOutput(
            work_description=self.work_description,
        )
