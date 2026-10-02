from decimal import Decimal

from domain.models.labour_time import LabourTime, LabourTimeSource


def test_labour_time():
    labour_time = LabourTime(
        hours=Decimal("0.8"),
        source=LabourTimeSource.AUTODATA,
    )

    assert labour_time.hours == Decimal("0.8")
    assert labour_time.source == LabourTimeSource.AUTODATA


import pytest
from pydantic import ValidationError


def test_labour_time_must_be_greater_than_zero():
    with pytest.raises(ValidationError):
        LabourTime(
            hours=Decimal("0"),
            source=LabourTimeSource.AUTODATA,
        )


def test_labour_time_cannot_be_negative():
    with pytest.raises(ValidationError):
        LabourTime(
            hours=Decimal("-0.5"),
            source=LabourTimeSource.AUTODATA,
        )
