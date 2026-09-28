from domain.models.supplier_offer import Availability


def is_available(availability: Availability) -> bool:
    return availability in {
        Availability.IN_STOCK,
        Availability.BACKORDER,
    }