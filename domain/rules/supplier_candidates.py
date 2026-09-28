from domain.models.indexed_product import IndexedProduct
from domain.models.part_requirement import PartRequirement
from domain.models.supplier_candidate import SupplierCandidate
from domain.models.supplier_offer import SupplierOffer
from domain.rules.availability import is_available
from domain.rules.supplier_candidate import create_supplier_candidate


def build_supplier_candidates(
    requirement: PartRequirement,
    products: list[IndexedProduct],
    offers: list[SupplierOffer],
) -> list[SupplierCandidate]:
    candidates = []

    for product in products:
        for offer in offers:
            if offer.product_id != product.product_id:
                continue

            if not is_available(offer.availability):
                continue

            candidate = create_supplier_candidate(
                requirement=requirement,
                product=product,
                offer=offer,
            )

            if candidate is not None:
                candidates.append(candidate)

    return candidates