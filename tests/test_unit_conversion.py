import pytest

from domain.models.indexed_product import Unit
from domain.rules.unit_conversion import convert_to_pcs


def test_pcs_stays_as_pcs():
    assert convert_to_pcs(3, Unit.PCS) == 3


def test_pair_converts_to_two_pcs():
    assert convert_to_pcs(1, Unit.PAIR) == 2
    assert convert_to_pcs(2, Unit.PAIR) == 4


def test_set_cannot_be_converted_to_pcs():
    with pytest.raises(ValueError):
        convert_to_pcs(1, Unit.SET)
