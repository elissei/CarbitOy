from domain.models.final_quote import FinalQuote
from domain.models.quote_presentation_output import QuotePresentationOutput
from domain.rules.quote_presentation import build_quote_presentation_input
from integrations.ai.quote_presenter import QuotePresenter


def present_final_quote(
    quote: FinalQuote,
    presenter: QuotePresenter,
) -> QuotePresentationOutput:
    presentation_input = build_quote_presentation_input(quote)

    return presenter.present(presentation_input)
