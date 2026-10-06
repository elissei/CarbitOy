import pytest

from app.browser_demo_workflow import process_browser_demo_message
from integrations.ai.demo_replay_interpreter import (
    DIAGNOSIS_DEMO_MESSAGE,
    READY_DEMO_MESSAGE,
    DemoReplayInterpreter,
)


def test_replay_ready_case_uses_existing_workflow():
    result = process_browser_demo_message(
        message=READY_DEMO_MESSAGE,
        interpreter=DemoReplayInterpreter(),
    )

    assert result.proposal is not None
    assert len(result.proposal.candidates) == 2
    assert result.proposal.labour_cost.total_price == 72


def test_replay_diagnosis_case_stops_workflow():
    result = process_browser_demo_message(
        message=DIAGNOSIS_DEMO_MESSAGE,
        interpreter=DemoReplayInterpreter(),
    )

    assert result.intake.interpretation.diagnosis_required is True
    assert result.intake.work_item is None
    assert result.proposal is None


def test_replay_rejects_unknown_message():
    with pytest.raises(ValueError, match="esimerkkiviestejä"):
        DemoReplayInterpreter().interpret(
            "XYZ-789, vaihda moottori"
        )
