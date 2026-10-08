from decimal import Decimal

from domain.models.labour_time import LabourTime, LabourTimeSource
from services.labour_pricing import calculate_labour_cost, split_inclusive_vat


def test_single_shock_absorber_price_includes_vat():
    labour_time = LabourTime(
        hours=Decimal("1.60"),
        source=LabourTimeSource.MANUAL_AUTODATA,
        work_description="Iskunvaimennin, irrotus ja asennus",
        recorded_by="Korjaamon työntekijä",
    )

    cost = calculate_labour_cost(labour_time)
    net, vat = split_inclusive_vat(cost.total_price)

    assert cost.total_price == Decimal("144.00")
    assert net == Decimal("114.74")
    assert vat == Decimal("29.26")


def test_pair_replacement_uses_combined_labour_time():
    labour_time = LabourTime(
        hours=Decimal("2.55"),
        source=LabourTimeSource.MANUAL_AUTODATA,
        work_description="Iskunvaimentimet (pari)",
        recorded_by="Korjaamon työntekijä",
    )

    cost = calculate_labour_cost(labour_time)

    assert cost.total_price == Decimal("229.50")


def test_vat_is_not_added_twice():
    net, vat = split_inclusive_vat(Decimal("90.00"))

    assert net + vat == Decimal("90.00")
    assert net == Decimal("71.71")
    assert vat == Decimal("18.29")


def test_labour_cost_rounds_to_cents():
    labour_time = LabourTime(
        hours=Decimal("1.3355"),
        source=LabourTimeSource.AUTODATA,
    )

    cost = calculate_labour_cost(labour_time)

    assert cost.total_price == Decimal("120.20")
