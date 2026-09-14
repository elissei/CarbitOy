from domain.models.part_requirement import (
    Axle,
    PartRequirement,
    RequirementUnit,
)


def test_front_brake_pad_requirement():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=1,
        required_unit=RequirementUnit.SET,
    )

    assert requirement.canonical_product_type == "brake_pad"
    assert requirement.axle == Axle.FRONT
    assert requirement.required_quantity == 1
    assert requirement.required_unit == RequirementUnit.SET


def test_requirement_unit_is_enum():
    requirement = PartRequirement(
        canonical_product_type="brake_disc",
        axle=Axle.REAR,
        required_quantity=1,
        required_unit=RequirementUnit.PAIR,
    )

    assert requirement.required_unit == RequirementUnit.PAIR
    assert isinstance(requirement.required_unit, RequirementUnit)
