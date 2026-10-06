---
title: Work and Part Data
type: concept
last_verified: 2026-10-06
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
  - domain/models/customer_request.py
  - domain/models/part_sale_price.py
  - domain/models/final_quote.py
  - domain/models/quote_presentation.py
  - domain/models/quote_presentation_output.py
  - domain/rules/customer_request_work_item.py
  - domain/rules/final_quote.py
  - domain/ontology/parser.py
  - domain/ontology/locations.yaml
  - domain/ontology/part_types.yaml
  - domain/ontology/synonyms.yaml
---

# Work and Part Data

## Business Definition

A work item describes a vehicle operation and its needed parts. The shared models carry a customer's interpreted request through catalog products, supplier prices, labour, a proposal, a person's selection, and a final quote. They also define the input and output contracts for a quote's work description.

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
| `InterpretationStatus` | `READY`, `HUMAN_REQUIRED` | Indicates whether an interpreted request can proceed to work-item creation or needs a person. |
| `CustomerRequestInterpretation` | `registration` | Carries the registration string returned by the interpreter. |
| `CustomerRequestInterpretation` | `operation` | Requested operation, or `None` when one cannot be identified. |
| `CustomerRequestInterpretation` | `axle` | Interpreted vehicle axle. |
| `CustomerRequestInterpretation` | `part_types` | Interpreted canonical part-type strings. |
| `CustomerRequestInterpretation` | `diagnosis_required` | Indicates that diagnosis is needed. |
| `CustomerRequestInterpretation` | `status` | Controls whether intake can build a work item. |
| `WorkItem` | `operation` | Work operation to perform. |
| `WorkItem` | `axle` | Axle associated with the work. |
| `WorkItem` | `part_requirements` | Required parts; the list defaults to empty. |
| `Operation` | `BRAKE_REPLACEMENT` | Currently declared work operation. |
| `Axle` | `FRONT`, `REAR`, `BOTH`, `UNKNOWN` | Shared location vocabulary for work, requirements, and products. |
| `PartRequirement` | `canonical_product_type` | Part vocabulary name used for product matching. |
| `PartRequirement` | `axle` | Required location when that part type needs axle matching. |
| `PartRequirement` | `required_quantity`, `required_unit` | Required count and its unit. |
| `RequirementUnit` | `PCS`, `SET`, `PAIR`, `L`, `KG` | Units allowed on requirements. |
| `IndexedProduct` | `product_id` | Identifies the catalog product used to join offers. |
| `IndexedProduct` | `supplier`, `supplier_product_id` | Identifies the supplier's product. |
| `IndexedProduct` | `canonical_product_type`, `axle`, `package_quantity`, `package_unit` | Describes the product and contents of one package. |
| `IndexedProduct` | `commercial_quantity` | States the product's commercial selling quantity. |
| `IndexedProduct` | `commercial_unit` | States the unit of that selling quantity. |
| `Unit` | `PCS`, `SET`, `PAIR`, `PACKAGE`, `L`, `KG` | Units allowed on indexed products. |
| `SupplierOffer` | `product_id`, `supplier`, `supplier_product_id` | Ties the offer to a product and supplier. |
| `SupplierOffer` | `unit_price` | Supplier's offered unit price. |
| `SupplierOffer` | `currency` | Currency of the offer price. |
| `SupplierOffer` | `availability` | Supplier's stock state for the offer. |
| `Availability` | `IN_STOCK`, `BACKORDER`, `OUT_OF_STOCK`, `UNKNOWN` | Offer and selection availability vocabulary. |
| `LabourTimeSource` | `AUTODATA` | Declared source vocabulary for a time estimate. |
| `SupplierCandidate` | `product_id`, `supplier`, `supplier_product_id` | Identifies a proposed supplier choice. |
| `SupplierCandidate` | `commercial_quantity_needed`, `unit_price`, `total_price`, `currency`, `availability` | Holds the proposed buying quantity, prices, currency, and stock state. |
| `LabourTime` | `hours`, `source` | Positive time estimate; `LabourTimeSource` currently declares `AUTODATA`. |
| `LabourCost` | `hours`, `hourly_rate`, `total_price`, `currency` | Positive hours, rate, and total; the total must equal hours times rate. |
| `RawProposal` | `proposal_id`, `created_at` | Identifies and dates the proposal. |
| `RawProposal` | `vehicle`, `work_item` | Connects vehicle and requested work. |
| `RawProposal` | `labour_time` | Supplies the time estimate. |
| `RawProposal` | `labour_cost` | Supplies the priced labour. |
| `RawProposal` | `candidates` | Holds part choices; the list defaults to empty. |
| `HumanSelection` | `proposal_id` | Identifies the proposal in which the choice was made. |
| `HumanSelection` | `product_id`, `supplier`, `supplier_product_id` | Identifies the selected product offer. |
| `HumanSelection` | `commercial_quantity_needed`, `unit_price` | Copies the chosen purchase quantity and unit price. |
| `HumanSelection` | `total_price`, `currency`, `availability` | Copies the chosen purchase total and availability. |
| `HumanSelection` | `selected_by`, `selected_at` | Records who selected it and when. |
| `PartSalePrice` | `purchase_price` | Selected part's purchase total. |
| `PartSalePrice` | `sale_price` | Price charged to the customer for that part. |
| `PartSalePrice` | `currency` | Currency shared by the purchase and sale prices. |
| `FinalQuote` | `quote_id`, `proposal_id` | Identifies the quote and originating proposal. |
| `FinalQuote` | `vehicle`, `work_item` | Carries the vehicle and requested work into the quote. |
| `FinalQuote` | `labour_cost` | Carries labour pricing into the quote. |
| `FinalQuote` | `part_selection` | Records the chosen supplier candidate. |
| `FinalQuote` | `part_sale_price` | Carries purchase and sale prices for the chosen part. |
| `FinalQuote` | `total_price` | Final amount checked against labour and part sale price. |
| `FinalQuote` | `created_at` | Dates the quote. |
| `QuotePresentationInput` | `registration`, `make`, `model` | Identifies the vehicle to the presenter. |
| `QuotePresentationInput` | `operation`, `axle` | Names the requested work. |
| `QuotePresentationInput` | `part_types` | Lists work-item part types; defaults to empty. |
| `QuotePresentationInput` | `labour_hours`, `labour_price` | Provides the work duration and labour price. |
| `QuotePresentationInput` | `parts_price` | Provides the part sale price. |
| `QuotePresentationInput` | `total_price`, `currency` | Provides the quote amount and currency. |
| `QuotePresentationInput` | `prices_include_vat` | Signals whether those prices include VAT. |
| `QuotePresentationOutput` | `work_description` | Holds the generated description of the work. |

`CustomerRequestInterpretation` rejects extra fields. `READY` requires an operation and at least one part type, and `diagnosis_required=True` requires `HUMAN_REQUIRED`. Its model does not check part-type membership; the [intake conversion rule](../features/customer-intake.md) currently supports only `brake_pad`.

`RawProposal` rejects extra fields and requires `labour_time.hours` to match `labour_cost.hours`. `LabourTime` and `LabourCost` require positive numeric fields as noted above. `PartSalePrice` requires positive purchase and sale prices. `FinalQuote` verifies matching proposal identity, purchase price, currency agreement, and that its total equals labour cost plus part sale price. [Quote creation](../features/final-quote.md) calculates the prices before constructing that model; the model does not itself apply the sale-price multiplier.

`QuotePresentationInput` and `QuotePresentationOutput` reject extra fields. Neither model validates the business content of `work_description`; the output contract accepts an empty string. The remaining quantity and price fields shown here have no explicit positivity constraints in their model declarations. The model declarations also do not require a `canonical_product_type` string to occur in the ontology.

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
- `domain/models/customer_request.py` - demonstrates interpretation status and model-level readiness checks.
- `domain/models/final_quote.py` - demonstrates identity, purchase-price, currency, and total checks.
- `domain/models/quote_presentation.py` - demonstrates the presentation input contract.

## Related Concepts

- [Architecture](../ARCHITECTURE.md) places these contracts within the library.
- [Proposal Decisions](../features/proposal-decisions.md) owns the decision and selection sequence that uses them.
- [Customer Intake](../features/customer-intake.md) owns the message interpretation and work-item conversion flow.
- [Final Quote and Presentation](../features/final-quote.md) owns price calculation and presentation sequence.
