from domain.models.indexed_product import Axle
from domain.rules.axle_matching import is_axle_compatible


def test_front_requirement_matches_front_product():
    assert is_axle_compatible(
        required_axle=Axle.FRONT,
        product_axle=Axle.FRONT,
    ) is True


def test_front_requirement_does_not_match_rear_product():
    assert is_axle_compatible(
        required_axle=Axle.FRONT,
        product_axle=Axle.REAR,
    ) is False


def test_rear_requirement_does_not_match_front_product():
    assert is_axle_compatible(
        required_axle=Axle.REAR,
        product_axle=Axle.FRONT,
    ) is False


def test_both_product_matches_front_requirement():
    assert is_axle_compatible(
        required_axle=Axle.FRONT,
        product_axle=Axle.BOTH,
    ) is True


def test_both_product_matches_rear_requirement():
    assert is_axle_compatible(
        required_axle=Axle.REAR,
        product_axle=Axle.BOTH,
    ) is True

def test_unknown_requirement_does_not_block_product():
    assert is_axle_compatible(
        required_axle=Axle.UNKNOWN,
        product_axle=Axle.FRONT,
    ) is True


def test_unknown_product_does_not_block_requirement():
    assert is_axle_compatible(
        required_axle=Axle.FRONT,
        product_axle=Axle.UNKNOWN,
    ) is True