from domain.models.enums import Axle
from domain.models.raw_proposal import RawProposal
from domain.models.supplier_offer import Availability
from domain.models.work_item import Operation


AVAILABILITY_LABELS = {
    Availability.IN_STOCK: "varastossa",
    Availability.BACKORDER: "jälkitoimitus",
    Availability.OUT_OF_STOCK: "ei varastossa",
    Availability.UNKNOWN: "saatavuus tuntematon",
}

OPERATION_LABELS = {
    Operation.BRAKE_REPLACEMENT: "Jarrujen vaihto",
}

AXLE_LABELS = {
    Axle.FRONT: "etuakseli",
    Axle.REAR: "taka-akseli",
    Axle.BOTH: "molemmat akselit",
    Axle.UNKNOWN: "akseli tuntematon",
}


def format_raw_proposal(proposal: RawProposal) -> str:
    vehicle = proposal.vehicle
    work_item = proposal.work_item

    vehicle_description = f"{vehicle.make} {vehicle.model}"
    if vehicle.year is not None:
        vehicle_description += f" {vehicle.year}"

    operation = OPERATION_LABELS[work_item.operation]
    axle = AXLE_LABELS[work_item.axle]

    lines = [
        "TARJOUSPOHJA",
        proposal.proposal_id,
        "",
        "AJONEUVO",
        vehicle.registration,
        vehicle_description,
        "",
        "TYÖ",
        f"{operation} – {axle}",
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

        price = f"{candidate.total_price:.2f}".replace(".", ",")

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
