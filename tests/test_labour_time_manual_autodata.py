from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.models.labour_time import LabourTime, LabourTimeSource


def test_existing_autodata_labour_time_remains_valid():
    result = LabourTime(
        hours=Decimal("1.60"),
        source=LabourTimeSource.AUTODATA,
    )
    assert result.hours == Decimal("1.60")


def test_manual_autodata_labour_time():
    result = LabourTime(
        hours=Decimal("2.55"),
        source=LabourTimeSource.MANUAL_AUTODATA,
        work_description="Iskunvaimentimet (pari), irrotus ja asennus",
        recorded_by="Korjaamon työntekijä",
    )
    assert result.hours == Decimal("2.55")
    assert result.source == LabourTimeSource.MANUAL_AUTODATA


def test_manual_autodata_requires_description():
    with pytest.raises(ValidationError):
        LabourTime(
            hours=Decimal("1.60"),
            source=LabourTimeSource.MANUAL_AUTODATA,
            recorded_by="Korjaamon työntekijä",
        )


def test_manual_autodata_requires_recorder():
    with pytest.raises(ValidationError):
        LabourTime(
            hours=Decimal("1.60"),
            source=LabourTimeSource.MANUAL_AUTODATA,
            work_description="Iskunvaimennin, irrotus ja asennus",
        )


def test_labour_time_must_be_positive():
    with pytest.raises(ValidationError):
        LabourTime(
            hours=Decimal("0"),
            source=LabourTimeSource.AUTODATA,
        )
