from domain.models.indexed_product import Axle


def is_axle_compatible(
    required_axle: Axle,
    product_axle: Axle,
) -> bool:
    if required_axle == Axle.UNKNOWN:
        return True

    if product_axle == Axle.UNKNOWN:
        return True

    if product_axle == Axle.BOTH:
        return True

    return required_axle == product_axle