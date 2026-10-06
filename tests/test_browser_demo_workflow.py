from app.browser_demo_workflow import process_browser_demo_message
from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from integrations.ai.fake_customer_request_interpreter import (
    FakeCustomerRequestInterpreter,
)


def make_interpreter(**changes):
    data = {
        "registration": "ABC-123",
        "operation": "BRAKE_REPLACEMENT",
        "axle": "FRONT",
        "part_types": ["brake_pad"],
        "diagnosis_required": False,
        "status": "READY",
    }
    data.update(changes)
    return FakeCustomerRequestInterpreter(
        interpretation=CustomerRequestInterpretation.model_validate(data)
    )


def test_supported_message_returns_candidates_without_selecting_supplier():
    result = process_browser_demo_message(
        message="ABC-123, etujarrupalat pitäisi vaihtaa",
        interpreter=make_interpreter(),
    )

    assert result.proposal is not None
    assert result.intake.work_item is not None
    assert {candidate.supplier for candidate in result.proposal.candidates} == {
        "SupplierA",
        "SupplierB",
    }
    assert result.proposal.labour_cost.total_price == 72


def test_diagnosis_stops_before_proposal():
    result = process_browser_demo_message(
        message="ABC-123, autosta kuuluu outoa ääntä edestä",
        interpreter=make_interpreter(
            operation=None,
            part_types=[],
            diagnosis_required=True,
            status="HUMAN_REQUIRED",
        ),
    )

    assert result.intake.interpretation.diagnosis_required is True
    assert result.intake.work_item is None
    assert result.proposal is None


def test_unsupported_axle_stops_before_using_front_brake_demo_data():
    result = process_browser_demo_message(
        message="ABC-123, takajarrupalat pitäisi vaihtaa",
        interpreter=make_interpreter(axle="REAR"),
    )

    assert result.intake.interpretation.status == (
        InterpretationStatus.HUMAN_REQUIRED
    )
    assert result.intake.interpretation.diagnosis_required is False
    assert result.intake.work_item is None
    assert result.proposal is None
