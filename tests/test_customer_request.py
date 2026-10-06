from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.work_item import Operation


def test_known_customer_request_can_be_ready():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["brake_pad"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    assert interpretation.registration == "ABC-123"
    assert interpretation.operation == Operation.BRAKE_REPLACEMENT
    assert interpretation.axle == Axle.FRONT
    assert interpretation.part_types == ["brake_pad"]
    assert interpretation.status == InterpretationStatus.READY


def test_uncertain_customer_request_can_require_human():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=None,
        axle=Axle.FRONT,
        part_types=[],
        diagnosis_required=False,
        status=InterpretationStatus.HUMAN_REQUIRED,
    )

    assert interpretation.operation is None
    assert interpretation.part_types == []
    assert interpretation.status == InterpretationStatus.HUMAN_REQUIRED


def test_ready_request_requires_operation():
    try:
        CustomerRequestInterpretation(
            registration="ABC-123",
            operation=None,
            axle=Axle.FRONT,
            part_types=["brake_pad"],
            diagnosis_required=False,
            status=InterpretationStatus.READY,
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "READY interpretation must require an operation"
        )


def test_ready_request_requires_part_types():
    try:
        CustomerRequestInterpretation(
            registration="ABC-123",
            operation=Operation.BRAKE_REPLACEMENT,
            axle=Axle.FRONT,
            part_types=[],
            diagnosis_required=False,
            status=InterpretationStatus.READY,
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "READY interpretation must require part types"
        )


def test_uncertain_symptom_can_require_diagnosis():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=None,
        axle=Axle.UNKNOWN,
        part_types=[],
        diagnosis_required=True,
        status=InterpretationStatus.HUMAN_REQUIRED,
    )

    assert interpretation.diagnosis_required is True
    assert interpretation.status == InterpretationStatus.HUMAN_REQUIRED

