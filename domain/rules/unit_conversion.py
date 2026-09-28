from domain.models.indexed_product import Unit


def convert_to_pcs(quantity: int, unit: Unit) -> int:
    if quantity <= 0:
        raise ValueError("quantity must be greater than 0")

    if unit == Unit.PCS:
        return quantity

    if unit == Unit.PAIR:
        return quantity * 2

    raise ValueError(f"Cannot convert {unit} to PCS")
