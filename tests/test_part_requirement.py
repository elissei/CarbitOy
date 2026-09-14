from domain.models.part_requirement import Axle, PartRequirement


def test_front_brake_pad_requirement():
    requirement = PartRequirement(
        canonical_product_type="brake_pad",
        axle=Axle.FRONT,
        required_quantity=1,
        required_unit="SET",
    )

    assert requirement.canonical_product_type == "brake_pad"
    assert requirement.axle == Axle.FRONT
    assert requirement.required_quantity == 1
    assert requirement.required_unit == "SET"
