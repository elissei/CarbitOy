---
title: "Feature: Proposal Decisions"
type: feature
sources:
  - domain/rules/availability.py
  - domain/rules/axle_matching.py
  - domain/rules/candidate_sorting.py
  - domain/rules/human_selection.py
  - domain/rules/labour_cost.py
  - domain/rules/package_coverage.py
  - domain/rules/product_matching.py
  - domain/rules/supplier_candidate.py
  - domain/rules/supplier_candidates.py
  - domain/rules/supplier_offer_validation.py
  - domain/rules/unit_conversion.py
  - domain/rules/unit_matching.py
  - domain/models/enums.py
  - domain/models/human_selection.py
  - domain/models/indexed_product.py
  - domain/models/labour_cost.py
  - domain/models/labour_time.py
  - domain/models/part_requirement.py
  - domain/models/raw_proposal.py
  - domain/models/supplier_candidate.py
  - domain/models/supplier_offer.py
  - domain/models/vehicle.py
  - domain/models/work_item.py
  - domain/ontology/parser.py
  - domain/ontology/part_types.yaml
  - presentation/raw_proposal_text.py
  - app/demo_workflow.py
---

# Feature: Proposal Decisions

## Overview

Callers turn a requirement, indexed products, and supplier offers into priced candidates, then present a raw proposal for a person to choose from. `create_demo_proposal()` composes these steps for a work item with one requirement.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work and Part Data](../concepts/work-and-part-data.md) owns the requirement, offer, proposal, and selection contracts.

## Functional Specification

### Mechanism

1. Obtain a requirement, products, and offers; [Supplier Adapters](../patterns/supplier-adapters.md) owns offer acquisition. Call `build_supplier_candidates()` and, when price ordering is needed, `sort_by_total_price()`.
2. Obtain `LabourTime`, call `calculate_labour_cost()`, and construct `RawProposal` with the work, labour, and candidates. `create_demo_proposal()` composes this for exactly one part requirement.
3. Call `format_raw_proposal()` for Finnish decision text, then `select_candidate_from_proposal()` with a candidate from that proposal and the selector's identity and time.

After selection, [Final Quote and Presentation](./final-quote.md) owns quote assembly; `create_demo_final_quote()` is the in-repository composition example.

### Validation Rules

- Candidate building pairs offers with products by `product_id`, keeps only `IN_STOCK` or `BACKORDER`, and validates a matched, available offer's nonblank identifiers and currency and positive unit price.
- Coverage requires equal canonical type and unit values; when the ontology requires axle matching, an unknown required axle fails while an unknown or `BOTH` product axle matches a known one. Coverage calculation rejects nonpositive quantities, rounds needed packages up, and multiplies the count by offer unit price.
- Selection requires candidate membership and nonblank `proposal_id` and `selected_by`.
- `RawProposal` requires its labour time and cost hours to agree; labour cost uses a fixed `90.00` EUR hourly rate.
- `create_demo_proposal()` requires exactly one part requirement. It passes the resulting candidates into `RawProposal` without sorting them.

The renderer uses the proposal's existing candidate order, so callers that want price order must sort before constructing the proposal.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No candidates | Renderer says no part options are available and calls for human handling. |
| Offer has no matching product or is unavailable | Candidate builder skips it. |
| Matched available offer has blank identifiers or currency, or a nonpositive price | Validation raises `ValueError`. |
| Candidate does not belong to the proposal | Selection raises `ValueError`. |
| Work item has no requirement or more than one | `create_demo_proposal()` raises `ValueError`. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `PackageCoverageResult` | `covered`, `commercial_quantity_needed`, `covered_quantity` | Coverage and purchasing count. |

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `domain.rules.supplier_candidates` | `build_supplier_candidates()` | Filters offers and builds covered candidates. |
| `domain.rules.package_coverage` | `calculate_product_coverage()` | Checks product, unit, axle, and package coverage. |
| `domain.rules.unit_conversion` | `convert_to_pcs()` | Converts PCS or PAIR; the candidate flow does not call it. |
| `domain.rules.candidate_sorting` | `sort_by_total_price()` | Orders candidates by total price. |
| `domain.rules.labour_cost` | `calculate_labour_cost()` | Applies the fixed EUR hourly rate of 90.00. |
| `presentation.raw_proposal_text` | `format_raw_proposal()` | Renders Finnish labels and two-decimal comma prices. |
| `domain.rules.human_selection` | `select_candidate_from_proposal()` | Copies a member candidate into a human selection. |
| `app.demo_workflow` | `create_demo_proposal()` | Composes candidate and labour rules for one requirement. |

## Integration Points

- **Depends on**: [Work and Part Data](../concepts/work-and-part-data.md) for contracts and ontology; [Supplier Adapters](../patterns/supplier-adapters.md) for offers.
- **Used by**: Callers assembling proposals and requesting a human decision; [Final Quote and Presentation](./final-quote.md) uses the resulting selection.
- **External systems**: None in these rules; labour time is supplied by the caller.

## Extension Guide

Extend offer acquisition through [Supplier Adapters](../patterns/supplier-adapters.md). For work with more than one requirement, compose candidate building per requirement instead of calling the single-requirement demo helper. Once a person chooses a candidate, use [Final Quote and Presentation](./final-quote.md) for quote creation and output.
