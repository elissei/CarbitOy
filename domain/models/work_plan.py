from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.config.pair_replacement import get_pair_replacement_rule
from domain.models.enums import Side
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem


class PlannedWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: str = Field(min_length=1)
    work_item: WorkItem

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("work_id must not be blank")
        return value


class WorkPlan(BaseModel):
    """Required work on one vehicle, with stable work identifiers."""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle
    work_items: list[PlannedWork] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_work_ids(self) -> "WorkPlan":
        ids = [work.work_id for work in self.work_items]
        if len(ids) != len(set(ids)):
            raise ValueError("work IDs must be unique within a WorkPlan")
        return self

    @property
    def split_side_groups(self) -> list[list[str]]:
        """Opposite sides of the same operation, explicitly requested as works."""
        grouped = {}
        for work in self.work_items:
            item = work.work_item
            if item.side in (Side.LEFT, Side.RIGHT):
                grouped.setdefault((item.operation, item.axle), []).append(work)
        return [
            [work.work_id for work in works]
            for works in grouped.values()
            if {work.work_item.side for work in works} == {Side.LEFT, Side.RIGHT}
        ]

    @property
    def confirmed_split_pairs(self) -> list[list[str]]:
        """Only approved practices permit automatic same-operation pair choice.

        No new side is added. Unconfirmed split-side groups require an explicit
        full-group time choice; their single-side times are not a fallback.
        """
        works = {work.work_id: work.work_item for work in self.work_items}
        return [
            group for group in self.split_side_groups
            if works[group[0]].operation == Operation.SHOCK_ABSORBER_REPLACEMENT
            and get_pair_replacement_rule("shock_absorber", works[group[0]].axle)
            is not None
        ]
