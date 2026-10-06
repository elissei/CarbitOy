
from datetime import datetime, timezone
from uuid import uuid4

from app.demo_intake_workflow import finalize_demo_quote
from app.proposal_token import read_proposal
from domain.models.final_quote import FinalQuote


def finalize_browser_demo_quote(
    proposal_token: str,
    selected_product_id: str,
    signing_secret: str,
) -> FinalQuote:
    envelope = read_proposal(proposal_token, signing_secret)
    proposal = envelope.proposal

    matches = [
        candidate
        for candidate in proposal.candidates
        if candidate.product_id == selected_product_id
    ]

    if len(matches) != 1:
        raise ValueError(
            "Valitse yksi proposalissa oleva varaosavaihtoehto."
        )

    now = datetime.now(timezone.utc)

    return finalize_demo_quote(
        quote_id=f"quote-{uuid4().hex}",
        proposal=proposal,
        selected_candidate=matches[0],
        selected_by="demo-user",
        selected_at=now,
        created_at=now,
    )
