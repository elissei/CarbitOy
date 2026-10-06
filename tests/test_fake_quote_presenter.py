from decimal import Decimal

from domain.models.quote_presentation import QuotePresentationInput
from integrations.ai.fake_quote_presenter import FakeQuotePresenter


def test_fake_quote_presenter_returns_work_description():
    presentation_input = QuotePresentationInput(
        registration="ABC-123",
        make="Toyota",
        model="Corolla",
        operation="BRAKE_REPLACEMENT",
        axle="FRONT",
        part_types=["brake_pad", "brake_disc"],
        labour_hours=Decimal("0.8"),
        labour_price=Decimal("72.00"),
        parts_price=Decimal("58.37"),
        total_price=Decimal("130.37"),
        currency="EUR",
        prices_include_vat=True,
    )

    presenter = FakeQuotePresenter(
        work_description="Etujarrulevyjen ja -palojen vaihto",
    )

    output = presenter.present(presentation_input)

    assert (
        output.work_description
        == "Etujarrulevyjen ja -palojen vaihto"
    )
