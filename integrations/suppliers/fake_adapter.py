from domain.models.supplier_offer import SupplierOffer
from integrations.suppliers.adapter import SupplierAdapter


class FakeSupplierAdapter(SupplierAdapter):
    def __init__(self, offers: list[SupplierOffer]) -> None:
        self.offers = offers

    def search(self, query: str) -> list[SupplierOffer]:
        return self.offers
