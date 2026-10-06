import pytest
from itsdangerous import BadSignature, SignatureExpired
from itsdangerous.timed import TimestampSigner

from app.browser_demo_workflow import process_browser_demo_message
from app.proposal_token import read_proposal, sign_proposal
from integrations.ai.demo_replay_interpreter import (
    READY_DEMO_MESSAGE,
    DemoReplayInterpreter,
)


TEST_SECRET = "test-only-signing-secret-1234567890"


@pytest.fixture
def proposal():
    result = process_browser_demo_message(
        message=READY_DEMO_MESSAGE,
        interpreter=DemoReplayInterpreter(),
    )
    assert result.proposal is not None
    return result.proposal


def test_signed_proposal_preserves_domain_data_and_mode(proposal):
    token = sign_proposal(proposal, "replay", TEST_SECRET)
    envelope = read_proposal(token, TEST_SECRET)

    assert envelope.proposal == proposal
    assert envelope.mode == "replay"


def test_modified_token_is_rejected(proposal):
    token = sign_proposal(proposal, "replay", TEST_SECRET)
    payload, signature = token.rsplit(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    modified = payload + "." + replacement + signature[1:]

    with pytest.raises(BadSignature):
        read_proposal(modified, TEST_SECRET)


def test_wrong_signing_secret_is_rejected(proposal):
    token = sign_proposal(proposal, "replay", TEST_SECRET)

    with pytest.raises(BadSignature):
        read_proposal(token, "different-test-secret-123456789012")


def test_expired_proposal_is_rejected(proposal, monkeypatch):
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 1000)
    token = sign_proposal(proposal, "replay", TEST_SECRET)

    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 4601)

    with pytest.raises(SignatureExpired):
        read_proposal(token, TEST_SECRET)
