from domain.models.enums import Axle
from domain.models.part_requirement import PartRequirement, RequirementUnit
from domain.models.work_item import Operation, WorkItem


def test_front_brake_replacement():
    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
    )

    assert work_item.operation == Operation.BRAKE_REPLACEMENT
    assert work_item.axle == Axle.FRONT


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
