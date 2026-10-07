import pytest

from app.browser_demo_finalize import finalize_browser_demo_quote
from app.browser_demo_workflow import process_browser_demo_message
from app.proposal_token import sign_proposal
from integrations.ai.demo_replay_interpreter import (
    READY_DEMO_MESSAGE,
    DemoReplayInterpreter,
)
from integrations.ai.fake_quote_presenter import FakeQuotePresenter
from presentation.browser_quote_text import create_browser_customer_text


@pytest.fixture
def quote():
    secret = "test-only-signing-secret-1234567890"
    result = process_browser_demo_message(
        message=READY_DEMO_MESSAGE,
        interpreter=DemoReplayInterpreter(),
    )
    assert result.proposal is not None

    return finalize_browser_demo_quote(
        proposal_token=sign_proposal(result.proposal, "replay", secret),
        selected_product_id="PAD-2",
        signing_secret=secret,
    )


def test_customer_text_uses_presenter_description_and_domain_prices(quote):
    text = create_browser_customer_text(
        quote=quote,
        presenter=FakeQuotePresenter(
            work_description="Vaihdamme autosi etujarrupalat."
        ),
    )

    assert "Vaihdamme autosi etujarrupalat." in text
    assert "Varaosat: 65.00 €" in text
    assert "Työ: 0.8 h / 72.00 €" in text
    assert "Yhteensä: 137.00 € sis. ALV" in text
    assert quote.part_selection.supplier == "SupplierB"
    assert quote.total_price == 137


def test_empty_presenter_description_is_rejected(quote):
    with pytest.raises(ValueError, match="Työn kuvaus puuttuu"):
        create_browser_customer_text(
            quote=quote,
            presenter=FakeQuotePresenter(work_description="   "),
        )
