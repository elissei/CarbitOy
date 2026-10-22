from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from integrations.autodata.fake_adapter import (
    FakeAutodataLabourAdapter, FakeOperation, FakeVariant, FakeVehicle,
)
from integrations.autodata.models import (
    Choice, ChoiceIdentity, ChoiceLevel, LabourTimeLookupResult, LabourTimeReading,
    LabourTimeRequest, LookupOutcome, LookupProvenance, ResolvedBy, VehicleQuery, WorkQuery,
)


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def choice(*path, source_id=None):
    return Choice(identity=ChoiceIdentity(source_id=source_id, selection_path=path))


def variant(label, text):
    return FakeVariant(choice=choice(label), displayed_time_text=text)


BRAKES = FakeOperation(choice=choice("Jarrut", "Etujarrupalat, vaihto", source_id="SYN-O-1"),
                       variants=(variant("Vakio", "1,2 h"),))
SHOCK_PAIR = FakeOperation(choice=choice("Iskunvaimentimet", "Etuiskunvaimentimet, molemmat puolet"),
                           variants=(variant("Molemmat puolet", "2,4 h"),))
DISCS = FakeOperation(choice=choice("Jarrut", "Etujarrulevyt ja -palat, vaihto"), variants=(
    variant("Ilman ilmastointia", "1,5 h"), variant("Ilmastoinnilla", "1.9 h"),
))
NO_ROWS = FakeOperation(choice=choice("Valot", "Ajovalo, säätö"), variants=())
NO_TIME = FakeOperation(choice=choice("Sähkö", "Laturi, vaihto"), variants=(variant("Vakio", None),))
CLOCK = FakeOperation(choice=choice("Sähkö", "Käynnistin, vaihto"), variants=(variant("Vakio", "1:30 h"),))
OPERATIONS = (BRAKES, SHOCK_PAIR, DISCS, NO_ROWS, NO_TIME, CLOCK)
PETROL = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1", "1.6 bensiini",
                                   source_id="SYN-V-1"), operations=OPERATIONS)
DIESEL = FakeVehicle(choice=choice("Testimerkki A", "Testimalli 1", "2.0 diesel"), operations=OPERATIONS)


def request(search="vaihto", vehicle=None, operation=None, variant=None):
    return LabourTimeRequest(
        vehicle_query=VehicleQuery(registration="SYN-1"), work_query=WorkQuery(search_text=search),
        vehicle_selection=vehicle, operation_selection=operation, variant_selection=variant,
    )


def adapter(*vehicles, **kwargs):
    return FakeAutodataLabourAdapter(vehicles, clock=lambda: NOW, **kwargs)


def test_several_vehicles_return_options_without_hours():
    result = adapter(PETROL, DIESEL).lookup(request())
    assert result.outcome == LookupOutcome.VEHICLE_AMBIGUOUS
    assert result.level == ChoiceLevel.VEHICLE
    assert result.candidates == (PETROL.choice, DIESEL.choice)
    assert result.hours is None and result.time is None
    assert result.model_dump(mode="json")["hours"] is None


def test_explicit_selections_resolve_to_found_with_provenance():
    result = adapter(PETROL, DIESEL).lookup(request(
        "etujarrupalat", vehicle=PETROL.choice.identity, operation=BRAKES.choice.identity,
    ))
    assert result.outcome == LookupOutcome.FOUND
    assert result.vehicle.resolved_by == ResolvedBy.EXPLICIT_SELECTION
    assert result.operation.resolved_by == ResolvedBy.EXPLICIT_SELECTION
    assert result.variant.resolved_by == ResolvedBy.ONLY_CANDIDATE
    assert result.operation.choice.identity.source_id == "SYN-O-1"
    assert result.hours == Decimal("1.2")
    assert result.time.raw_time_text == "1,2 h" and result.time.unit_evidence == "h"
    assert result.provenance.synthetic is True
    assert result.provenance.retrieved_at == NOW


def test_vehicle_without_source_id_is_selected_by_its_selection_path():
    identity = ChoiceIdentity(selection_path=("Testimerkki A", "Testimalli 1", "2.0 diesel"))
    result = adapter(PETROL, DIESEL).lookup(request("etujarrupalat", vehicle=identity))
    assert result.outcome == LookupOutcome.FOUND
    assert result.vehicle.choice == DIESEL.choice
    assert result.vehicle.choice.identity.source_id is None


def test_selection_must_match_the_full_identity():
    # The visible path alone does not select a choice that has a source ID.
    path_only = ChoiceIdentity(selection_path=PETROL.choice.identity.selection_path)
    result = adapter(PETROL, DIESEL).lookup(request(vehicle=path_only))
    assert result.outcome == LookupOutcome.SELECTION_INVALID
    assert result.level == ChoiceLevel.VEHICLE
    assert result.candidates == (PETROL.choice, DIESEL.choice)
    assert result.hours is None


def test_several_operations_return_options():
    result = adapter(PETROL).lookup(request("jarru"))
    assert result.outcome == LookupOutcome.WORK_AMBIGUOUS
    assert result.vehicle.resolved_by == ResolvedBy.ONLY_CANDIDATE
    assert result.candidates == (BRAKES.choice, DISCS.choice)
    assert result.hours is None


def test_unknown_vehicle_and_work_are_not_found():
    assert adapter().lookup(request()).outcome == LookupOutcome.VEHICLE_NOT_FOUND
    result = adapter(PETROL).lookup(request("vetokoukku"))
    assert result.outcome == LookupOutcome.WORK_NOT_FOUND
    assert result.level == ChoiceLevel.OPERATION and result.hours is None


def test_several_time_variants_require_a_staff_choice():
    ambiguous = adapter(PETROL).lookup(request("levyt"))
    assert ambiguous.outcome == LookupOutcome.VARIANT_AMBIGUOUS
    assert ambiguous.candidates == tuple(v.choice for v in DISCS.variants)
    assert ambiguous.hours is None
    chosen = adapter(PETROL).lookup(request(
        "levyt", vehicle=PETROL.choice.identity, operation=DISCS.choice.identity,
        variant=DISCS.variants[1].choice.identity,
    ))
    assert chosen.outcome == LookupOutcome.FOUND
    assert chosen.hours == Decimal("1.9")


def test_pair_time_is_the_displayed_pair_time_never_doubled():
    result = adapter(PETROL).lookup(request("iskunvaimentimet"))
    assert result.outcome == LookupOutcome.FOUND
    assert result.hours == Decimal("2.4")


def test_missing_time_is_null_not_zero():
    no_rows = adapter(PETROL).lookup(request("ajovalo"))
    assert no_rows.outcome == LookupOutcome.TIME_MISSING
    assert no_rows.operation.choice == NO_ROWS.choice and no_rows.variant is None
    no_time = adapter(PETROL).lookup(request("laturi"))
    assert no_time.outcome == LookupOutcome.TIME_MISSING
    assert no_time.variant is not None
    assert no_rows.hours is None and no_time.hours is None


def test_rejected_time_text_is_kept_as_evidence_without_hours():
    result = adapter(PETROL).lookup(request("käynnistin"))
    assert result.outcome == LookupOutcome.UNPARSEABLE
    assert result.rejected_time_text == "1:30 h"
    assert result.rejection_reason == "CLOCK_FORMAT"
    assert result.hours is None


def test_long_rejected_time_text_is_capped_with_an_explicit_flag():
    long_text = "noin " + "1" * 300 + " h"
    operation = FakeOperation(choice=choice("Muu", "Pitkä aikateksti"),
                              variants=(variant("Vakio", long_text),))
    vehicle = FakeVehicle(choice=PETROL.choice, operations=(operation,))
    result = adapter(vehicle).lookup(request("pitkä"))
    assert result.outcome == LookupOutcome.UNPARSEABLE
    assert result.rejection_reason == "TOO_LONG"
    assert result.rejected_time_text == long_text[:200]
    assert result.rejected_time_text_truncated is True
    # Codex P2: a capped text of only whitespace must not crash the lookup.
    padded = " " * 200 + "x"
    operation = FakeOperation(choice=choice("Muu", "Tyhjä alku"), variants=(variant("Vakio", padded),))
    result = adapter(FakeVehicle(choice=PETROL.choice, operations=(operation,))).lookup(request("tyhjä"))
    assert result.outcome == LookupOutcome.UNPARSEABLE
    assert result.rejected_time_text == " " * 200
    assert result.rejected_time_text_truncated is True
    assert result.hours is None
    short = adapter(PETROL).lookup(request("käynnistin"))
    assert short.rejected_time_text_truncated is False


def test_indistinguishable_choices_cannot_be_selected():
    twin = FakeVehicle(choice=choice("Testimerkki C", "Testimalli 3"), operations=OPERATIONS)
    result = adapter(twin, twin).lookup(request())
    assert result.outcome == LookupOutcome.SELECTION_NOT_UNIQUE
    assert result.hours is None
    selected = adapter(twin, twin).lookup(request(vehicle=twin.choice.identity))
    assert selected.outcome == LookupOutcome.SELECTION_NOT_UNIQUE


@pytest.mark.parametrize("failure", [
    LookupOutcome.LOGIN_REQUIRED, LookupOutcome.BLOCKED, LookupOutcome.SITE_CHANGED,
    LookupOutcome.TIMEOUT, LookupOutcome.ERROR,
])
def test_source_failures_carry_no_hours_or_choices(failure):
    result = adapter(PETROL, failure=failure).lookup(request())
    assert result.outcome == failure
    assert result.hours is None and result.candidates == () and result.vehicle is None


def test_fake_refuses_to_simulate_choice_outcomes():
    with pytest.raises(ValueError):
        adapter(PETROL, failure=LookupOutcome.FOUND)


def provenance():
    return LookupProvenance(adapter_name="fake", adapter_version="fake-1", retrieved_at=NOW, synthetic=True)


def reading(hours="1.2"):
    return LabourTimeReading(raw_time_text=f"{hours} h", number_text=hours, unit_evidence="h",
                             hours=Decimal(hours))


def resolved(path):
    from integrations.autodata.models import ResolvedChoice
    return ResolvedChoice(choice=choice(path), resolved_by=ResolvedBy.EXPLICIT_SELECTION)


def test_found_requires_every_choice_and_a_time():
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.FOUND, provenance=provenance(),
                               vehicle=resolved("V"), operation=resolved("O"), time=reading())
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.FOUND, provenance=provenance(),
                               vehicle=resolved("V"), operation=resolved("O"), variant=resolved("A"))


def test_only_found_may_carry_a_time():
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.TIME_MISSING, provenance=provenance(),
                               vehicle=resolved("V"), operation=resolved("O"), time=reading())


def test_ambiguous_results_need_two_unique_candidates_at_the_next_level():
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.VEHICLE_AMBIGUOUS, provenance=provenance(),
                               level=ChoiceLevel.VEHICLE, candidates=(choice("A"),))
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.VEHICLE_AMBIGUOUS, provenance=provenance(),
                               level=ChoiceLevel.VEHICLE, candidates=(choice("A"), choice("A")))
    with pytest.raises(ValidationError):
        LabourTimeLookupResult(outcome=LookupOutcome.WORK_AMBIGUOUS, provenance=provenance(),
                               level=ChoiceLevel.OPERATION, candidates=(choice("A"), choice("B")))


@pytest.mark.parametrize("hours", [0, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.2, True])
def test_time_reading_requires_positive_finite_exact_hours(hours):
    with pytest.raises(ValidationError):
        LabourTimeReading(raw_time_text="x h", number_text="x", unit_evidence="h", hours=hours)


def test_identity_and_queries_reject_blank_or_empty_input():
    with pytest.raises(ValidationError):
        ChoiceIdentity(selection_path=())
    with pytest.raises(ValidationError):
        ChoiceIdentity(selection_path=("A", " "))
    with pytest.raises(ValidationError):
        ChoiceIdentity(source_id=" ", selection_path=("A",))
    with pytest.raises(ValidationError):
        Choice(identity=ChoiceIdentity(selection_path=("A",)), details=("",))
    with pytest.raises(ValidationError):
        VehicleQuery()
    with pytest.raises(ValidationError):
        WorkQuery(search_text=" ")


def test_selections_follow_vehicle_operation_variant_order():
    identity = ChoiceIdentity(selection_path=("A",))
    with pytest.raises(ValidationError):
        request(operation=identity)
    with pytest.raises(ValidationError):
        request(vehicle=identity, variant=identity)


def test_retrieved_at_requires_timezone():
    with pytest.raises(ValidationError):
        LookupProvenance(adapter_name="fake", adapter_version="1", retrieved_at=datetime(2026, 10, 22),
                         synthetic=True)
