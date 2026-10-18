from dataclasses import FrozenInstanceError

import pytest

from domain.config.pair_replacement import (
    PAIR_REPLACEMENT_RULES,
    get_pair_replacement_rule,
)
from domain.models.enums import Axle, Side
from domain.models.work_item import Operation, WorkItem
from domain.ontology.parser import find_part_type, part_type_requires_axle


def test_front_shock_absorber_pair_practice_is_explicit():
    rule = get_pair_replacement_rule("shock_absorber", Axle.FRONT)

    assert rule is not None
    assert rule.canonical_product_type == "shock_absorber"
    assert rule.axle == Axle.FRONT
    assert len(PAIR_REPLACEMENT_RULES) == 1


def test_configured_part_types_match_existing_ontology():
    assert find_part_type("iskunvaimentimet") == "shock_absorber"
    for rule in PAIR_REPLACEMENT_RULES:
        assert part_type_requires_axle(rule.canonical_product_type)


@pytest.mark.parametrize(
    "part_type", ["spring", "brake_disc", "brake_pad", "control_arm", "unknown"]
)
def test_unconfirmed_part_types_have_no_pair_rule(part_type):
    assert get_pair_replacement_rule(part_type, Axle.FRONT) is None


@pytest.mark.parametrize("axle", [Axle.REAR, Axle.BOTH, Axle.UNKNOWN])
def test_unconfirmed_axles_have_no_pair_rule(axle):
    assert get_pair_replacement_rule("shock_absorber", axle) is None


def test_rule_lookup_does_not_guess_from_synonyms():
    assert get_pair_replacement_rule("iskunvaimentimet", Axle.FRONT) is None


def test_pair_rule_is_immutable():
    rule = get_pair_replacement_rule("shock_absorber", Axle.FRONT)
    with pytest.raises(FrozenInstanceError):
        rule.axle = Axle.REAR


def test_pair_practice_does_not_change_explicit_work_scope():
    work_item = WorkItem(
        operation=Operation.SHOCK_ABSORBER_REPLACEMENT,
        axle=Axle.FRONT,
        side=Side.LEFT,
    )
    original = work_item.model_dump()

    assert get_pair_replacement_rule("shock_absorber", work_item.axle) is not None
    assert work_item.model_dump() == original
    assert work_item.side == Side.LEFT
    assert work_item.part_requirements == []
