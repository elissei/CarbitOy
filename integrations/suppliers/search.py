from integrations.suppliers.adapter import SupplierAdapter
from domain.models.supplier_offer import SupplierOffer


def search_suppliers(
    adapters: list[SupplierAdapter],
    query: str,
) -> list[SupplierOffer]:
    offers = []

    for adapter in adapters:
        offers.extend(adapter.search(query))

    return offers
