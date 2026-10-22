from decimal import Decimal, localcontext

import pytest

from integrations.autodata.time_parser import TimeRejection, parse_labour_time


@pytest.mark.parametrize(("text", "hours", "number", "unit"), [
    ("1,2 h", Decimal("1.2"), "1,2", "h"),
    ("1.2 h", Decimal("1.2"), "1.2", "h"),
    ("0,8 tuntia", Decimal("0.8"), "0,8", "tuntia"),
    ("1 tunti", Decimal("1"), "1", "tunti"),
    ("2 hours", Decimal("2"), "2", "hours"),
    ("1.50 hrs", Decimal("1.50"), "1.50", "hrs"),
    ("0,125 h", Decimal("0.125"), "0,125", "h"),
    ("1,25h", Decimal("1.25"), "1,25", "h"),
    ("2,4 H", Decimal("2.4"), "2,4", "H"),
    ("  1,2\u00a0h ", Decimal("1.2"), "1,2", "h"),
])
def test_accepts_unambiguous_decimal_hours_with_visible_unit(text, hours, number, unit):
    result = parse_labour_time(text)
    assert result.rejection is None
    reading = result.reading
    assert reading.hours == hours
    assert reading.hours.as_tuple() == hours.as_tuple()
    assert reading.raw_time_text == text
    assert reading.number_text == number
    assert reading.unit_evidence == unit
    assert reading.unit == "HOURS"


def test_hours_are_exact_even_under_low_decimal_precision():
    text = "1,23456789012345678901234567 h"
    with localcontext() as context:
        context.prec = 3
        result = parse_labour_time(text)
    assert result.reading.hours == Decimal("1.23456789012345678901234567")


@pytest.mark.parametrize(("text", "reason"), [
    ("", TimeRejection.EMPTY),
    ("   ", TimeRejection.EMPTY),
    ("1" * 65 + " h", TimeRejection.TOO_LONG),
    ("1:30", TimeRejection.CLOCK_FORMAT),
    ("1:30 h", TimeRejection.CLOCK_FORMAT),
    ("1 h 30 min", TimeRejection.CLOCK_FORMAT),
    ("1h30min", TimeRejection.CLOCK_FORMAT),
    ("1 t 30 min", TimeRejection.CLOCK_FORMAT),
    ("1,0-1,5 h", TimeRejection.RANGE),
    ("1,0\u20131,5 h", TimeRejection.RANGE),
    ("1 - 2 h", TimeRejection.RANGE),
    ("1 to 2 hours", TimeRejection.RANGE),
    ("1,2", TimeRejection.MISSING_UNIT),
    ("90 min", TimeRejection.UNKNOWN_UNIT),
    ("1,2 units", TimeRejection.UNKNOWN_UNIT),
    ("1,2 t", TimeRejection.UNKNOWN_UNIT),
    ("1,2 tuntia (arvio)", TimeRejection.MALFORMED),
    ("1,234 h", TimeRejection.AMBIGUOUS_SEPARATOR),
    ("12.500 h", TimeRejection.AMBIGUOUS_SEPARATOR),
    ("0 h", TimeRejection.NOT_POSITIVE),
    ("0,0 h", TimeRejection.NOT_POSITIVE),
    ("-1 h", TimeRejection.MALFORMED),
    ("+1 h", TimeRejection.MALFORMED),
    ("1.2.3 h", TimeRejection.MALFORMED),
    ("1, 2 h", TimeRejection.MALFORMED),
    ("1e2 h", TimeRejection.MALFORMED),
    ("NaN h", TimeRejection.MALFORMED),
    ("Infinity h", TimeRejection.MALFORMED),
    ("\u0661 h", TimeRejection.MALFORMED),
    ("1.2 h.", TimeRejection.MALFORMED),
    ("noin 1 h", TimeRejection.MALFORMED),
])
def test_rejects_formats_outside_the_first_parser_scope(text, reason):
    result = parse_labour_time(text)
    assert result.reading is None
    assert result.rejection == reason
    assert result.raw_time_text == text
