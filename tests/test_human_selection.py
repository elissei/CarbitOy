from datetime import datetime
from decimal import Decimal
from domain.models.labour_time import LabourTime, LabourTimeSource

import pytest

from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import Availability
from domain.models.enums import Axle
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from domain.rules.human_selection import create_human_selection


def test_create_human_selection_from_candidate():
    candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("44.90"),
        total_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    selected_at = datetime(2026, 10, 2, 12, 30)

    selection = create_human_selection(
        candidate=candidate,
        proposal_id="PROPOSAL-001",
        selected_by="workshop_manager",
        selected_at=selected_at,
    )

    assert selection.product_id == candidate.product_id
    assert selection.supplier == candidate.supplier
    assert (
        selection.supplier_product_id
        == candidate.supplier_product_id
    )
    assert selection.commercial_quantity_needed == 1
    assert selection.unit_price == Decimal("44.90")
    assert selection.total_price == Decimal("44.90")
    assert selection.currency == "EUR"
    assert selection.availability == Availability.IN_STOCK
    assert selection.selected_by == "workshop_manager"
    assert selection.selected_at == selected_at


def test_human_selection_requires_selected_by():
    candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("44.90"),
        total_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    with pytest.raises(
        ValueError,
        match="selected_by must not be empty",
    ):
        create_human_selection(
            candidate=candidate,
            proposal_id="PROPOSAL-001",
            selected_by="",
            selected_at=datetime(2026, 10, 2, 12, 30),
        )

from domain.models.raw_proposal import RawProposal
from domain.rules.human_selection import select_candidate_from_proposal


def test_select_candidate_from_raw_proposal():
    candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("44.90"),
        total_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    proposal = RawProposal(
        proposal_id="PROPOSAL-001",
        vehicle=Vehicle(
            registration="ABC-123",
            make="Toyota",
            model="Corolla",
            year=2020,
        ),
        work_item=WorkItem(
            operation=Operation.BRAKE_REPLACEMENT,
            axle=Axle.FRONT,
        ),
        labour_time=LabourTime(
            hours=Decimal("0.8"),
            source=LabourTimeSource.AUTODATA,
        ),
        candidates=[candidate],
        created_at=datetime(2026, 10, 2, 12, 45),
    )

    selected_at = datetime(2026, 10, 2, 12, 54)

    selection = select_candidate_from_proposal(
        proposal=proposal,
        candidate=candidate,
        selected_by="workshop_manager",
        selected_at=selected_at,
    )

    assert selection.proposal_id == "PROPOSAL-001"
    assert selection.supplier == "AUTOPARTS24"
    assert selection.supplier_product_id == "AP24-123"
    assert selection.commercial_quantity_needed == 1
    assert selection.unit_price == Decimal("44.90")
    assert selection.total_price == Decimal("44.90")
    assert selection.currency == "EUR"
    assert selection.availability == Availability.IN_STOCK
    assert selection.selected_by == "workshop_manager"
    assert selection.selected_at == selected_at


def test_cannot_select_candidate_outside_raw_proposal():
    proposal_candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="AUTOPARTS24",
        supplier_product_id="AP24-123",
        commercial_quantity_needed=1,
        unit_price=Decimal("44.90"),
        total_price=Decimal("44.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    outside_candidate = SupplierCandidate(
        product_id="BRAKE-PAD-001",
        supplier="SUPPLIER_C",
        supplier_product_id="C-999",
        commercial_quantity_needed=1,
        unit_price=Decimal("39.90"),
        total_price=Decimal("39.90"),
        currency="EUR",
        availability=Availability.IN_STOCK,
    )

    proposal = RawProposal(
        proposal_id="PROPOSAL-001",
        vehicle=Vehicle(
            registration="ABC-123",
            make="Toyota",
            model="Corolla",
            year=2020,
        ),
        work_item=WorkItem(
            operation=Operation.BRAKE_REPLACEMENT,
            axle=Axle.FRONT,
        ),
        labour_time=LabourTime(
            hours=Decimal("0.8"),
            source=LabourTimeSource.AUTODATA,
        ),
        candidates=[proposal_candidate],
        created_at=datetime(2026, 10, 2, 12, 45),
    )

    with pytest.raises(
        ValueError,
        match="candidate must belong to the raw proposal",
    ):
        select_candidate_from_proposal(
            proposal=proposal,
            candidate=outside_candidate,
            selected_by="workshop_manager",
            selected_at=datetime(2026, 10, 2, 12, 54),
        )
