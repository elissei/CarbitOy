from domain.models.final_quote import FinalQuote
from integrations.ai.quote_presenter import QuotePresenter
from presentation.final_quote_text import format_final_quote
from presentation.quote_workflow import present_final_quote


def create_browser_customer_text(
    quote: FinalQuote,
    presenter: QuotePresenter,
) -> str:
    presentation = present_final_quote(
        quote=quote,
        presenter=presenter,
    )
    description = presentation.work_description.strip()

    if not description:
        raise ValueError("Työn kuvaus puuttuu.")

    return (
        f"Hei!\n\n"
        f"{description}\n\n"
        f"{format_final_quote(quote)}\n\n"
        f"Vahvistathan, haluatko hyväksyä tarjouksen."
    )
