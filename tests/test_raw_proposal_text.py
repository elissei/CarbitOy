from datetime import datetime
from decimal import Decimal

from domain.models.enums import Axle
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import Availability
from domain.models.vehicle import Vehicle
from domain.models.work_item import Operation, WorkItem
from presentation.raw_proposal_text import format_raw_proposal


def test_format_raw_proposal():
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
        created_at=datetime(2026, 10, 2, 14, 30),
        candidates=[
            SupplierCandidate(
                product_id="BRAKE-PAD-001",
                supplier="Autoparts24",
                supplier_product_id="AP24-123",
                commercial_quantity_needed=1,
                unit_price=Decimal("44.90"),
                total_price=Decimal("44.90"),
                currency="EUR",
                availability=Availability.IN_STOCK,
            ),
            SupplierCandidate(
                product_id="BRAKE-PAD-002",
                supplier="Supplier A",
                supplier_product_id="A-123",
                commercial_quantity_needed=1,
                unit_price=Decimal("49.90"),
                total_price=Decimal("49.90"),
                currency="EUR",
                availability=Availability.BACKORDER,
            ),
        ],
    )

    result = format_raw_proposal(proposal)

    assert "PROPOSAL-001" in result
    assert "AJONEUVO" in result
    assert "ABC-123" in result
    assert "Toyota Corolla 2020" in result
    assert "TYÖ" in result
    assert "Jarrujen vaihto" in result
    assert "etuakseli" in result
    assert "1. Autoparts24" in result
    assert "Saatavuus: varastossa" in result
    assert "Hinta: 44,90 EUR" in result
    assert "2. Supplier A" in result
    assert "Saatavuus: jälkitoimitus" in result
    assert "Hinta: 49,90 EUR" in result
    assert "Varaosan valitsee päätöksentekijä" in result


def test_format_raw_proposal_without_candidates_requires_human():
    proposal = RawProposal(
        proposal_id="PROPOSAL-002",
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
        created_at=datetime(2026, 10, 2, 14, 30),
        candidates=[],
    )

    result = format_raw_proposal(proposal)

    assert "Ei saatavilla olevia varaosavaihtoehtoja." in result
    assert "Vaatii ihmisen käsittelyn." in result
