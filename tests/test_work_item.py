from domain.models.part_requirement import Axle
from domain.models.work_item import Operation, WorkItem


def test_front_brake_replacement():
    work_item = WorkItem(
        operation=Operation.BRAKE_REPLACEMENT,
        axle=Axle.FRONT,
    )

    assert work_item.operation == Operation.BRAKE_REPLACEMENT
    assert work_item.axle == Axle.FRONT
