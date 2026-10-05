---
title: Work and Part Data
type: concept
sources:
  - domain/models/enums.py
  - domain/models/vehicle.py
  - domain/models/work_item.py
  - domain/models/part_requirement.py
  - domain/models/indexed_product.py
  - domain/models/supplier_offer.py
  - domain/models/supplier_candidate.py
  - domain/models/labour_time.py
  - domain/models/labour_cost.py
  - domain/models/raw_proposal.py
  - domain/models/human_selection.py
  - domain/models/final_quote.py
  - domain/ontology/parser.py
  - domain/ontology/locations.yaml
  - domain/ontology/part_types.yaml
  - domain/ontology/synonyms.yaml
---

# Work and Part Data

## Business Definition

A work item describes a vehicle operation and its needed parts. The shared models carry that requirement through catalog products, supplier prices, labour, a proposal, a person's selection, and a final quote; they define data contracts, not an application that assembles the whole quote.

## Configuration

| Source | Description |
|--------|-------------|
| `domain/ontology/synonyms.yaml` | Maps Finnish and English part names to canonical product types for text lookup. |
| `domain/ontology/locations.yaml` | Maps axle names to location phrases. |
| `domain/ontology/part_types.yaml` | Records each canonical type's category, description, and `requires_axle` flag. |

**How to access:**

- `domain.ontology.parser.parse_part_requirement()` - parses recognized part text into a `PartRequirement`, or returns `None`.
- `domain.ontology.parser.part_type_requires_axle()` - reads a canonical type's `requires_axle` flag; raises `ValueError` for an unknown type.
- `domain.ontology.parser.find_part_type()` - finds a canonical type for an exact synonym, or returns `None`.
- `domain.ontology.parser.find_axle()` - finds an axle from a location phrase, defaulting to `Axle.UNKNOWN`.

`parse_part_requirement()` trims and lowercases text, then checks whether it contains a synonym; the first matching canonical type in YAML order wins. It separately detects axle text and returns quantity `1` in `RequirementUnit.SET`. This is a parser default, not a package coverage decision. If no part synonym matches, it returns `None`.

## Data Storage

These Pydantic models are in-memory contracts. The source does not define persisted storage for them.

| Object/Model/Type | Field/Property | Purpose |
|--------|-------|---------|
| `Vehicle` | `registration`, `make`, `model`, `year` | Identifies the vehicle; `year` may be `None`. |
| `WorkItem` | `operation`, `axle`, `part_requirements` | Groups an operation and required parts; the list defaults to empty. |
| `Operation` | `BRAKE_REPLACEMENT` | Currently declared work operation. |
| `Axle` | `FRONT`, `REAR`, `BOTH`, `UNKNOWN` | Shared location vocabulary for work, requirements, and products. |
| `PartRequirement` | `canonical_product_type`, `axle`, `required_quantity`, `required_unit` | States which part and quantity the work calls for. |
| `RequirementUnit` | `PCS`, `SET`, `PAIR`, `L`, `KG` | Units allowed on requirements. |
| `IndexedProduct` | `product_id`, `supplier`, `supplier_product_id` | Connects a catalog product to a supplier identity. |
| `IndexedProduct` | `canonical_product_type`, `axle`, `package_quantity`, `package_unit` | Describes the product and contents of one package. |
| `IndexedProduct` | `commercial_quantity`, `commercial_unit` | Describes its commercial selling quantity and unit. |
| `Unit` | `PCS`, `SET`, `PAIR`, `PACKAGE`, `L`, `KG` | Units allowed on indexed products. |
| `SupplierOffer` | `product_id`, `supplier`, `supplier_product_id` | Ties a price and availability to a product and supplier. |
| `SupplierOffer` | `unit_price`, `currency`, `availability` | Supplier's offered price, currency, and availability. |
| `Availability` | `IN_STOCK`, `BACKORDER`, `OUT_OF_STOCK`, `UNKNOWN` | Offer and selection availability vocabulary. |
| `LabourTimeSource` | `AUTODATA` | Declared source vocabulary for a time estimate. |
| `SupplierCandidate` | `product_id`, `supplier`, `supplier_product_id` | Identifies a proposed supplier choice. |
| `SupplierCandidate` | `commercial_quantity_needed`, `unit_price`, `total_price`, `currency`, `availability` | Holds the proposed buying quantity, prices, currency, and stock state. |
| `LabourTime` | `hours`, `source` | Positive time estimate; `LabourTimeSource` currently declares `AUTODATA`. |
| `LabourCost` | `hours`, `hourly_rate`, `total_price`, `currency` | Positive hours, rate, and total; the total must equal hours times rate. |
| `RawProposal` | `proposal_id`, `vehicle`, `work_item`, `labour_time`, `labour_cost` | Bundles the work and labour for a proposal. |
| `RawProposal` | `candidates`, `created_at` | Holds candidate choices and creation time; candidates default to empty. |
| `HumanSelection` | `proposal_id`, `product_id`, `supplier`, `supplier_product_id` | Identifies the proposal and selected product offer. |
| `HumanSelection` | `commercial_quantity_needed`, `unit_price`, `total_price`, `currency`, `availability` | Copies the chosen commercial and availability data. |
| `HumanSelection` | `selected_by`, `selected_at` | Records who selected it and when. |
| `FinalQuote` | `quote_id`, `proposal_id`, `vehicle`, `work_item`, `labour_cost`, `part_selection`, `total_price`, `created_at` | Carries the quote and chosen part; selection and quote must name the same proposal. |

`RawProposal` rejects extra fields and requires `labour_time.hours` to match `labour_cost.hours`. `LabourTime` and `LabourCost` require positive numeric fields as noted above. `FinalQuote` checks proposal identity but does not calculate or verify `total_price` against its components. The remaining quantity and price fields shown here have no explicit positivity constraints in their model declarations.
The model declarations also do not require a `canonical_product_type` string to occur in the ontology.

## Key Services/Functions (PUBLIC/EXPORTED only)

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `domain.ontology.parser` | `parse_part_requirement()` | Converts recognized text to a requirement with parser defaults. |
| `domain.ontology.parser` | `part_type_requires_axle()` | Retrieves the ontology's axle requirement for a canonical type. |
| `domain.ontology.parser` | `find_part_type()` | Resolves an exact synonym after trimming and lowercasing. |
| `domain.ontology.parser` | `find_axle()` | Resolves a location phrase to an `Axle` value. |
| `domain.ontology.parser` | `normalize_text()` | Trims and lowercases text for ontology lookup. |
| `domain.ontology.parser` | `load_yaml()` | Loads a named ontology YAML file from the parser's directory. |

## Reference Implementations

- `domain/ontology/parser.py` - demonstrates vocabulary lookup and construction of a default requirement.
- `domain/models/raw_proposal.py` - demonstrates nested proposal data and model-level validation.
- `domain/models/final_quote.py` - demonstrates the quote contract's proposal identity check.

## Related Concepts

- [Architecture](../ARCHITECTURE.md) places these contracts within the library.
- [Proposal Decisions](../features/proposal-decisions.md) owns the decision and selection sequence that uses them.
