import pytest

from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.enums import Axle
from domain.models.work_item import Operation
from domain.rules.customer_request_work_item import (
    build_work_item_from_customer_request,
)


def test_ready_customer_request_builds_work_item():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["brake_pad"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    work_item = build_work_item_from_customer_request(
        interpretation
    )

    assert work_item.operation == Operation.BRAKE_REPLACEMENT
    assert work_item.axle == Axle.FRONT
    assert len(work_item.part_requirements) == 1

    requirement = work_item.part_requirements[0]

    assert requirement.canonical_product_type == "brake_pad"
    assert requirement.axle == Axle.FRONT


def test_human_required_customer_request_cannot_build_work_item():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=None,
        axle=Axle.UNKNOWN,
        part_types=[],
        diagnosis_required=True,
        status=InterpretationStatus.HUMAN_REQUIRED,
    )

    with pytest.raises(ValueError):
        build_work_item_from_customer_request(
            interpretation
        )


def test_unknown_part_type_cannot_build_work_item():
    interpretation = CustomerRequestInterpretation(
        registration="ABC-123",
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_types=["unknown_part"],
        diagnosis_required=False,
        status=InterpretationStatus.READY,
    )

    with pytest.raises(ValueError):
        build_work_item_from_customer_request(
            interpretation
        )
