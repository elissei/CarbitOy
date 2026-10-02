from abc import ABC, abstractmethod

from domain.models.supplier_offer import SupplierOffer


class SupplierAdapter(ABC):
    @abstractmethod
    def search(self, query: str) -> list[SupplierOffer]:
        raise NotImplementedError
