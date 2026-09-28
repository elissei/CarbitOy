from domain.models.supplier_offer import SupplierOffer


def validate_supplier_offer(offer: SupplierOffer) -> None:
    if not offer.product_id.strip():
        raise ValueError("product_id must not be empty")

    if not offer.supplier.strip():
        raise ValueError("supplier must not be empty")

    if not offer.supplier_product_id.strip():
        raise ValueError("supplier_product_id must not be empty")

    if offer.unit_price <= 0:
        raise ValueError("unit_price must be greater than 0")

    if not offer.currency.strip():
        raise ValueError("currency must not be empty")