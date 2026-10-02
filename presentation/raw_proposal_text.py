from domain.models.raw_proposal import RawProposal
from domain.models.supplier_offer import Availability


AVAILABILITY_LABELS = {
    Availability.IN_STOCK: "varastossa",
    Availability.BACKORDER: "jälkitoimitus",
    Availability.OUT_OF_STOCK: "ei varastossa",
    Availability.UNKNOWN: "saatavuus tuntematon",
}


def format_raw_proposal(proposal: RawProposal) -> str:
    lines = [
        "TARJOUSPOHJA",
        proposal.proposal_id,
        "",
        "VARAOSAVAIHTOEHDOT",
        "",
    ]

    if not proposal.candidates:
        lines.extend(
            [
                "Ei saatavilla olevia varaosavaihtoehtoja.",
                "",
                "→ Vaatii ihmisen käsittelyn.",
            ]
        )
        return "\n".join(lines)

    for index, candidate in enumerate(
        proposal.candidates,
        start=1,
    ):
        availability = AVAILABILITY_LABELS[
            candidate.availability
        ]

        price = str(candidate.total_price).replace(".", ",")

        lines.extend(
            [
                f"{index}. {candidate.supplier}",
                f"   Tuote: {candidate.supplier_product_id}",
                f"   Saatavuus: {availability}",
                (
                    "   Määrä: "
                    f"{candidate.commercial_quantity_needed}"
                ),
                f"   Hinta: {price} {candidate.currency}",
                "",
            ]
        )

    lines.append(
        "→ Varaosan valitsee päätöksentekijä."
    )

    return "\n".join(lines)
