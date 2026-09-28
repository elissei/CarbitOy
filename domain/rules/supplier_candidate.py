from domain.models.indexed_product import IndexedProduct
from domain.models.part_requirement import PartRequirement
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import SupplierOffer
from domain.rules.package_coverage import calculate_product_coverage
from domain.rules.supplier_offer_validation import validate_supplier_offer


def create_supplier_candidate(
    requirement: PartRequirement,
    product: IndexedProduct,
    offer: SupplierOffer,
) -> SupplierCandidate | None:
    validate_supplier_offer(offer)

    coverage = calculate_product_coverage(
        requirement=requirement,
        product=product,
    )

    if not coverage.covered:
        return None

    total_price = (
        offer.unit_price * coverage.commercial_quantity_needed
    )

    return SupplierCandidate(
        product_id=product.product_id,
        supplier=offer.supplier,
        supplier_product_id=offer.supplier_product_id,
        commercial_quantity_needed=coverage.commercial_quantity_needed,
        unit_price=offer.unit_price,
        total_price=total_price,
        currency=offer.currency,
    )