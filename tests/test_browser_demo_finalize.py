from decimal import Decimal

import pytest

from app.browser_demo_finalize import finalize_browser_demo_quote
from app.browser_demo_workflow import process_browser_demo_message
from app.proposal_token import sign_proposal
from integrations.ai.demo_replay_interpreter import (
    READY_DEMO_MESSAGE,
    DemoReplayInterpreter,
)


TEST_SECRET = "test-only-signing-secret-1234567890"


@pytest.fixture
def proposal_token():
    result = process_browser_demo_message(
        message=READY_DEMO_MESSAGE,
        interpreter=DemoReplayInterpreter(),
    )
    assert result.proposal is not None
    return sign_proposal(result.proposal, "replay", TEST_SECRET)


def test_human_can_choose_more_expensive_supplier(proposal_token):
    quote = finalize_browser_demo_quote(
        proposal_token=proposal_token,
        selected_product_id="PAD-2",
        signing_secret=TEST_SECRET,
    )

    assert quote.part_selection.supplier == "SupplierB"
    assert quote.part_selection.total_price == Decimal("50.00")
    assert quote.part_sale_price.sale_price == Decimal("65.00")
    assert quote.labour_cost.total_price == Decimal("72.00")
    assert quote.total_price == Decimal("137.00")


@pytest.mark.parametrize("product_id", ["", "UNKNOWN"])
def test_missing_or_unknown_selection_is_rejected(proposal_token, product_id):
    with pytest.raises(ValueError, match="varaosavaihtoehto"):
        finalize_browser_demo_quote(
            proposal_token=proposal_token,
            selected_product_id=product_id,
            signing_secret=TEST_SECRET,
        )
