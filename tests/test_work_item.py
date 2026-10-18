import pytest
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.work_item import Operation, WorkItem


def test_front_brake_replacement():
    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
    )

    assert work_item.operation == Operation.BRAKE_REPLACEMENT
    assert work_item.axle == Axle.FRONT
    assert work_item.side == Side.UNKNOWN


def test_rear_brake_replacement():
    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.REAR,
    )

    assert work_item.operation == Operation.BRAKE_REPLACEMENT
    assert work_item.axle == Axle.REAR


def test_front_brake_replacement_has_part_requirements():
    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
        part_requirements=[
            PartRequirement(
                canonical_product_type="brake_pad",
                axle=Axle.FRONT,
                required_quantity=1,
                required_unit=RequirementUnit.SET,
            ),
            PartRequirement(
                canonical_product_type="brake_disc",
                axle=Axle.FRONT,
                required_quantity=2,
                required_unit=RequirementUnit.PCS,
            ),
        ],
    )

    assert len(work_item.part_requirements) == 2
    assert work_item.part_requirements[0].canonical_product_type == "brake_pad"
    assert work_item.part_requirements[1].canonical_product_type == "brake_disc"


@pytest.mark.parametrize("side", list(Side))
def test_work_item_accepts_explicit_side(side):
    work_item = WorkItem(
        operation=Operation.SHOCK_ABSORBER_REPLACEMENT,
        axle=Axle.FRONT,
        side=side,
    )

    assert work_item.operation == Operation.SHOCK_ABSORBER_REPLACEMENT
    assert work_item.axle == Axle.FRONT
    assert work_item.side == side


def test_work_item_reads_legacy_payload_without_side():
    work_item = WorkItem.model_validate(
        {"operation": "BRAKE_REPLACEMENT", "axle": "REAR"}
    )

    assert work_item.side == Side.UNKNOWN
    assert work_item.part_requirements == []


def test_work_item_serializes_side_and_new_operation():
    work_item = WorkItem(
        operation="SHOCK_ABSORBER_REPLACEMENT",
        axle="FRONT",
        side="BOTH",
    )

    payload = work_item.model_dump(mode="json")

    assert payload["operation"] == "SHOCK_ABSORBER_REPLACEMENT"
    assert payload["axle"] == "FRONT"
    assert payload["side"] == "BOTH"
    assert WorkItem.model_validate(payload) == work_item


def test_work_item_rejects_axle_as_side():
    with pytest.raises(ValidationError):
        WorkItem(operation="BRAKE_REPLACEMENT", axle="FRONT", side="FRONT")
