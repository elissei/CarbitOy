"""First, deliberately narrow labour-time parser.

Accepted: one positive decimal number of hours followed by a visible hour
unit, e.g. "1,2 h", "0.8 tuntia" or "2 hours". A comma or a dot may be the
decimal separator. The value is an exact Decimal; nothing is rounded.

Rejected: clock or hour-minute formats ("1:30", "1 h 30 min"), ranges
("1,0-1,5 h"), unknown or missing units, signs, exponents, grouping, and
"1,234"-style text where the separator could be a thousands separator. The
displayed format of the real source is not yet mapped; extend the accepted
units only from documented evidence.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from integrations.autodata.models import LabourTimeReading


MAX_TIME_TEXT_LENGTH = 64
HOUR_UNITS = frozenset({"h", "hr", "hrs", "hour", "hours", "tunti", "tuntia"})

_SPACES = re.compile(r"[\s\u00a0\u202f]+")
_ACCEPTED = re.compile(r"(?P<number>[0-9]+(?:[.,][0-9]+)?) ?(?P<unit>[^\W\d_]+)", re.ASCII | re.IGNORECASE)
_NUMBER_ONLY = re.compile(r"[0-9]+(?:[.,][0-9]+)?", re.ASCII)
_CLOCK = re.compile(r"[0-9]\s*[:\u2236]\s*[0-9]")
_RANGE = re.compile(r"[0-9]\s*(?:-|\u2010|\u2011|\u2012|\u2013|\u2014|\u2212|\.\.|to)\s*[0-9]", re.IGNORECASE)
_HOUR_MINUTE = re.compile(
    r"[0-9]\s*(?:h|hrs?|hours?|t|tuntia?)\s*[0-9]+\s*(?:m|mins?|minutes?|minuuttia?)\b", re.IGNORECASE,
)


class TimeRejection(str, Enum):
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    CLOCK_FORMAT = "CLOCK_FORMAT"
    RANGE = "RANGE"
    MISSING_UNIT = "MISSING_UNIT"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    AMBIGUOUS_SEPARATOR = "AMBIGUOUS_SEPARATOR"
    NOT_POSITIVE = "NOT_POSITIVE"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class TimeParseResult:
    raw_time_text: str
    reading: LabourTimeReading | None = None
    rejection: TimeRejection | None = None

    def __post_init__(self):
        if (self.reading is None) == (self.rejection is None):
            raise ValueError("a parse result is either a reading or a rejection")


def _reject(raw: str, reason: TimeRejection) -> TimeParseResult:
    return TimeParseResult(raw_time_text=raw, rejection=reason)


def _thousands_like(number: str) -> bool:
    """'1,234' or '12.500' may be grouping; '0,125' and '1,25' are not."""
    if "," not in number and "." not in number:
        return False
    whole, fraction = re.split(r"[.,]", number)
    return len(fraction) == 3 and whole != "0" and not whole.startswith("0")


def parse_labour_time(raw_time_text: str) -> TimeParseResult:
    raw = raw_time_text
    if len(raw) > MAX_TIME_TEXT_LENGTH:
        return _reject(raw, TimeRejection.TOO_LONG)
    text = _SPACES.sub(" ", raw).strip()
    if not text:
        return _reject(raw, TimeRejection.EMPTY)
    if _CLOCK.search(text) or _HOUR_MINUTE.search(text):
        return _reject(raw, TimeRejection.CLOCK_FORMAT)
    if _RANGE.search(text):
        return _reject(raw, TimeRejection.RANGE)
    if _NUMBER_ONLY.fullmatch(text):
        return _reject(raw, TimeRejection.MISSING_UNIT)
    match = _ACCEPTED.fullmatch(text)
    if match is None:
        return _reject(raw, TimeRejection.MALFORMED)
    number, unit = match.group("number"), match.group("unit")
    if unit.casefold() not in HOUR_UNITS:
        return _reject(raw, TimeRejection.UNKNOWN_UNIT)
    if _thousands_like(number):
        return _reject(raw, TimeRejection.AMBIGUOUS_SEPARATOR)
    # Construct from text: Decimal(str) is exact and ignores context precision.
    hours = Decimal(number.replace(",", "."))
    if not hours.is_finite() or hours <= 0:
        return _reject(raw, TimeRejection.NOT_POSITIVE)
    return TimeParseResult(raw_time_text=raw, reading=LabourTimeReading(
        raw_time_text=raw, number_text=number, unit_evidence=unit, hours=hours,
    ))
