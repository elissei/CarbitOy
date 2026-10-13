import pytest
from pydantic import ValidationError

from domain.models.enums import Axle, Side
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.models.work_plan import PlannedWork, WorkPlan


def vehicle():
    return Vehicle(registration="ABC-123", make="Toyota", model="Corolla")


def work(work_id):
    return PlannedWork(
        work_id=work_id,
        work_item=WorkItem(operation=Operation.BRAKE_REPLACEMENT, axle=Axle.FRONT),
    )


def test_work_plan_identifies_multiple_works_on_one_vehicle():
    plan = WorkPlan(vehicle=vehicle(), work_items=[work("brakes"), work("shocks")])

    assert plan.vehicle.registration == "ABC-123"
    assert [item.work_id for item in plan.work_items] == ["brakes", "shocks"]


def test_work_plan_rejects_duplicate_work_ids():
    with pytest.raises(ValidationError, match="unique"):
        WorkPlan(vehicle=vehicle(), work_items=[work("brakes"), work("brakes")])


def test_work_plan_requires_at_least_one_work():
    with pytest.raises(ValidationError):
        WorkPlan(vehicle=vehicle(), work_items=[])


@pytest.mark.parametrize("work_id", ["", " ", "\t"])
def test_planned_work_rejects_blank_id(work_id):
    with pytest.raises(ValidationError):
        work(work_id)


def test_work_plan_accepts_existing_work_item_without_side():
    plan = WorkPlan(vehicle=vehicle(), work_items=[work("brakes")])

    assert plan.work_items[0].work_item.side == Side.UNKNOWN
    assert WorkPlan.model_validate_json(plan.model_dump_json()) == plan
