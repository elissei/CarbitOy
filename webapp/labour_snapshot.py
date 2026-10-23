"""Stored labour price snapshot shared by the manual and Autodata routes.

The calculation, rounding and VAT split are the existing services; this module
only fixes the snapshot fields. It neither approves billing nor changes prices.
"""

from decimal import InvalidOperation, Overflow

from pydantic import ValidationError

from domain.models.labour_time import LabourTime
from services.labour_pricing import VAT_RATE, calculate_labour_cost, split_inclusive_vat


class UnpriceableLabourTimeError(ValueError):
    """The time does not produce a positive, calculable cent price."""


def build_labour_snapshot(labour_time: LabourTime) -> dict[str, str]:
    """Snapshot for WorkRepository.record_labour; unexpected errors propagate."""
    try:
        labour_cost = calculate_labour_cost(labour_time)
        net_price, vat_price = split_inclusive_vat(labour_cost.total_price)
    except (ValidationError, InvalidOperation, Overflow) as error:
        raise UnpriceableLabourTimeError(str(error)) from error
    return {
        "hours": str(labour_time.hours),
        "source": labour_time.source.value,
        "work_description": labour_time.work_description or "",
        "recorded_by": labour_time.recorded_by or "",
        "autodata_reference": labour_time.autodata_reference or "",
        "hourly_rate": str(labour_cost.hourly_rate),
        "total_price": str(labour_cost.total_price),
        "net_price": str(net_price),
        "vat_price": str(vat_price),
        "vat_rate": str(VAT_RATE),
        "currency": labour_cost.currency,
    }
