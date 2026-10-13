import pytest

from domain.models.enums import Axle, Side


@pytest.mark.parametrize(
    "value", ["LEFT", "RIGHT", "BOTH", "NOT_APPLICABLE", "UNKNOWN"]
)
def test_side_has_distinct_target_values(value):
    assert Side(value).value == value


def test_side_and_axle_are_separate_concepts():
    assert Side is not Axle
    with pytest.raises(ValueError):
        Side("FRONT")
    with pytest.raises(ValueError):
        Axle("LEFT")
