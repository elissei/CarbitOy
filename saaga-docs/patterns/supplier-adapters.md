---
title: Supplier Adapters
type: pattern
sources:
  - integrations/suppliers/adapter.py
  - integrations/suppliers/fake_adapter.py
  - integrations/suppliers/search.py
  - domain/models/supplier_offer.py
---

# Supplier Adapters

## When to Use

Use this pattern when a caller needs offers from one or more supplier sources. Implement `SupplierAdapter` for each source, then pass the adapters to `search_suppliers()`; [Work and Part Data](../concepts/work-and-part-data.md) owns the offer contract.

## Pattern

```python
from decimal import Decimal
from domain.models.supplier_offer import Availability, SupplierOffer
from integrations.suppliers.adapter import SupplierAdapter
from integrations.suppliers.fake_adapter import FakeSupplierAdapter
from integrations.suppliers.search import search_suppliers

class MySupplierAdapter(SupplierAdapter):
    def search(self, query: str) -> list[SupplierOffer]:
        return []

sample_offer = SupplierOffer(
    product_id="BRAKE-PAD-001", supplier="EXAMPLE",
    supplier_product_id="PAD-001", unit_price=Decimal("49.90"),
    currency="EUR", availability=Availability.IN_STOCK,
)
adapters: list[SupplierAdapter] = [FakeSupplierAdapter([sample_offer]), MySupplierAdapter()]
offers = search_suppliers(adapters, query="front brake pads")
assert offers == [sample_offer]
```

## Key Points

- `SupplierAdapter.search(query)` must return a `list[SupplierOffer]`; the base method is abstract.
- `search_suppliers()` calls each adapter with the same query, in list order, and extends one result list with each adapter's offers.
- `FakeSupplierAdapter` stores the supplied list and returns it for any query; it does not filter or call a supplier service.
- Pass the collected offers to the caller's decision flow; [Proposal Decisions](../features/proposal-decisions.md) owns candidate building and ordering.

## Reference Implementations

| File | Function/Method | Notes |
| --- | --- | --- |
| `integrations/suppliers/adapter.py` | `SupplierAdapter.search()` | Abstract public search contract. |
| `integrations/suppliers/fake_adapter.py` | `FakeSupplierAdapter.search()` | In-memory implementation with fixed offers. |
| `integrations/suppliers/search.py` | `search_suppliers()` | Sequential collection across adapters. |

## Anti-Patterns

**Do NOT:**

- Expect the collector or fake adapter to filter offers by query; implement any required filtering in a real adapter.
- Assume this boundary supplies retries, concurrent calls, or error isolation; an adapter exception interrupts collection.
