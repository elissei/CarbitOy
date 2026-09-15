from domain.models.indexed_product import Unit
from domain.models.part_requirement import RequirementUnit
from domain.rules.unit_matching import is_unit_compatible


def test_same_units_are_compatible():
    assert is_unit_compatible(RequirementUnit.SET, Unit.SET) is True
    assert is_unit_compatible(RequirementUnit.PAIR, Unit.PAIR) is True
    assert is_unit_compatible(RequirementUnit.PCS, Unit.PCS) is True
    assert is_unit_compatible(RequirementUnit.L, Unit.L) is True
    assert is_unit_compatible(RequirementUnit.KG, Unit.KG) is True


def test_different_units_are_not_compatible():
    assert is_unit_compatible(RequirementUnit.SET, Unit.PCS) is False
    assert is_unit_compatible(RequirementUnit.PAIR, Unit.PCS) is False
    assert is_unit_compatible(RequirementUnit.L, Unit.PCS) is False
    assert is_unit_compatible(RequirementUnit.KG, Unit.PCS) is False
