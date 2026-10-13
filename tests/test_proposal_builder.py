from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.models.enums import Axle
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import Availability
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from services.proposal_builder import build_raw_proposal


@pytest.fixture
def proposal_inputs():
    return {
        "proposal_id": "PROPOSAL-001",
        "vehicle": Vehicle(
            registration="ABC-123", make="Toyota", model="Corolla", year=2020
        ),
        "work_item": WorkItem(
            operation=Operation.BRAKE_REPLACEMENT, axle=Axle.FRONT
        ),
        "labour_time": LabourTime(
            hours=Decimal("1.60"),
            source=LabourTimeSource.MANUAL_AUTODATA,
            work_description="Jarrujen vaihto",
            autodata_reference="AUTODATA-001",
            recorded_by="Korjaamon työntekijä",
        ),
    }


def test_build_raw_proposal_uses_inclusive_labour_price(proposal_inputs):
    proposal = build_raw_proposal(**proposal_inputs)

    assert isinstance(proposal, RawProposal)
    assert proposal.proposal_id == proposal_inputs["proposal_id"]
    assert proposal.vehicle == proposal_inputs["vehicle"]
    assert proposal.work_item == proposal_inputs["work_item"]
    assert proposal.labour_cost.total_price == Decimal("144.00")
    assert proposal.labour_cost.hourly_rate == Decimal("90.00")
    assert proposal.labour_cost.currency == "EUR"


def test_build_raw_proposal_preserves_manual_autodata_details(proposal_inputs):
    proposal = build_raw_proposal(**proposal_inputs)

    assert proposal.labour_time.source == LabourTimeSource.MANUAL_AUTODATA
    assert proposal.labour_time.recorded_by == "Korjaamon työntekijä"
    assert proposal.labour_time.work_description == "Jarrujen vaihto"
    assert proposal.labour_time.autodata_reference == "AUTODATA-001"


@pytest.mark.parametrize("candidate_options", [{}, {"candidates": None}, {"candidates": []}])
def test_build_raw_proposal_defaults_to_empty_candidates(
    proposal_inputs, candidate_options
):
    proposal = build_raw_proposal(**proposal_inputs, **candidate_options)
    other_proposal = build_raw_proposal(**proposal_inputs, **candidate_options)

    assert proposal.candidates == []
    assert proposal.candidates is not other_proposal.candidates


@pytest.mark.parametrize("hours", ["1.60", "2.55", "1.3355"])
def test_build_raw_proposal_keeps_labour_hours_consistent(proposal_inputs, hours):
    proposal_inputs["labour_time"].hours = Decimal(hours)

    proposal = build_raw_proposal(**proposal_inputs)

    assert proposal.labour_time.hours == Decimal(hours)
    assert proposal.labour_cost.hours == proposal.labour_time.hours


def test_build_raw_proposal_sets_current_aware_utc_time(proposal_inputs):
    before = datetime.now(timezone.utc)
    proposal = build_raw_proposal(**proposal_inputs)
    after = datetime.now(timezone.utc)

    assert proposal.created_at.tzinfo is not None
    assert proposal.created_at.utcoffset() == timedelta(0)
    assert before <= proposal.created_at <= after


def test_build_raw_proposal_preserves_inputs_and_candidates(proposal_inputs):
    candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="Supplier A",
        supplier_product_id="A-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("49.90"),
        total_price=Decimal("49.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )
    candidates = [candidate]
    originals = {
        key: value.model_dump()
        for key, value in proposal_inputs.items()
        if key != "proposal_id"
    }
    original_candidate = candidate.model_dump()

    proposal = build_raw_proposal(**proposal_inputs, candidates=candidates)

    for key, original in originals.items():
        assert proposal_inputs[key].model_dump() == original
    assert candidate.model_dump() == original_candidate
    assert candidates == [candidate]
    assert proposal.candidates == candidates
    assert proposal.candidates is not candidates

    proposal.candidates.clear()
    assert candidates == [candidate]
