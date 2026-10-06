from typing import Literal

from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict

from domain.models.raw_proposal import RawProposal


PROPOSAL_TOKEN_MAX_AGE = 3600


class ProposalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: RawProposal
    mode: Literal["openai", "replay"]


def get_serializer(secret: str) -> URLSafeTimedSerializer:
    if len(secret) < 32:
        raise ValueError("Proposal signing secret must be at least 32 characters")

    return URLSafeTimedSerializer(
        secret_key=secret,
        salt="carbit-demo-proposal-v1",
    )


def sign_proposal(
    proposal: RawProposal,
    mode: Literal["openai", "replay"],
    secret: str,
) -> str:
    envelope = ProposalEnvelope(proposal=proposal, mode=mode)
    return get_serializer(secret).dumps(
        envelope.model_dump(mode="json")
    )


def read_proposal(
    token: str,
    secret: str,
) -> ProposalEnvelope:
    data = get_serializer(secret).loads(
        token,
        max_age=PROPOSAL_TOKEN_MAX_AGE,
    )
    return ProposalEnvelope.model_validate(data)
