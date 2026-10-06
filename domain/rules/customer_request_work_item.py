from domain.models.customer_request import (
    CustomerRequestInterpretation,
    InterpretationStatus,
)
from domain.models.part_requirement import (
    PartRequirement,
    RequirementUnit,
)
from domain.models.work_item import WorkItem


PART_REQUIREMENT_UNITS = {
    "brake_pad": RequirementUnit.SET,
}


def build_work_item_from_customer_request(
    interpretation: CustomerRequestInterpretation,
) -> WorkItem:
    if interpretation.status != InterpretationStatus.READY:
        raise ValueError(
            "WorkItem can only be built from READY interpretation"
        )

    if interpretation.operation is None:
        raise ValueError(
            "READY interpretation requires an operation"
        )

    requirements = []

    for part_type in interpretation.part_types:
        required_unit = PART_REQUIREMENT_UNITS.get(part_type)

        if required_unit is None:
            raise ValueError(
                f"Unsupported part type: {part_type}"
            )

        requirements.append(
            PartRequirement(
                canonical_product_type=part_type,
                axle=interpretation.axle,
                required_quantity=1,
                required_unit=required_unit,
            )
        )

    return WorkItem(
        operation=interpretation.operation,
        axle=interpretation.axle,
        part_requirements=requirements,
    )
