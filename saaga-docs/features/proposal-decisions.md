---
title: "Feature: Proposal Decisions"
type: feature
last_verified: 2026-10-19
sources:
  - services/proposal_builder.py
  - services/labour_pricing.py
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
- [Labour Planning](./labour-planning.md) owns multi-work time coverage and explicit review; its result is not wired into this single-work proposal path.

## Functional Specification

### Mechanism

1. Obtain a requirement, products, and offers; [Supplier Adapters](../patterns/supplier-adapters.md) owns offer acquisition. Call `build_supplier_candidates()` and, when price ordering is needed, `sort_by_total_price()`.
2. Obtain a caller-supplied `LabourTime`. `build_raw_proposal()` prices it with `services.labour_pricing.calculate_labour_cost()` and builds a `RawProposal` for one `WorkItem`; omitted candidates become an empty list and `created_at` is the current aware UTC time. `create_demo_proposal()` instead calls the domain labour rule and composes candidates for exactly one part requirement.
3. Call `format_raw_proposal()` for Finnish decision text, then `select_candidate_from_proposal()` with a candidate from that proposal and the selector's identity and time.

After selection, [Final Quote and Presentation](./final-quote.md) owns quote assembly; `create_demo_final_quote()` is the in-repository composition example.

The two proposal entry points leave different work to the caller. Use
`build_raw_proposal()` when the caller already has a `WorkItem`, a time, and
any candidate list it wants to show. It accepts no product or offer arguments
and therefore does not discover parts. Use `create_demo_proposal()` only for
its one-requirement demo composition; it takes products, offers, and a
caller-supplied creation timestamp. Neither path selects hours from a plan.

```python
candidates = build_supplier_candidates(requirement, products, offers)
ordered = sort_by_total_price(candidates)
proposal = build_raw_proposal(
    proposal_id, vehicle, work_item, labour_time, ordered
)
selection = select_candidate_from_proposal(
    proposal, ordered[0], selected_by, selected_at
)
```

This composition is appropriate only when `ordered` is nonempty. When it is
empty, render the proposal for human handling instead of indexing a missing
candidate. `build_raw_proposal()` preserves the candidate order it receives.

### Validation Rules

- Candidate building pairs offers with products by `product_id`, keeps only `IN_STOCK` or `BACKORDER`, and validates a matched, available offer's nonblank identifiers and currency and positive unit price.
- Coverage requires equal canonical type and unit values; when the ontology requires axle matching, an unknown required axle fails while an unknown or `BOTH` product axle matches a known one. Coverage calculation rejects nonpositive quantities, rounds needed packages up, and multiplies the count by offer unit price.
- Selection requires candidate membership and nonblank `proposal_id` and `selected_by`.
- `RawProposal` requires its labour time and cost hours to agree. The library labour rule multiplies hours by its fixed `90.00` EUR hourly rate without rounding before it constructs `LabourCost`. The shared [labour cost contract](../concepts/work-and-part-data.md) requires the product rounded half up to cents; an input whose raw product has fractional cents can therefore fail `LabourCost` validation.
- `create_demo_proposal()` requires exactly one part requirement. It passes the resulting candidates into `RawProposal` without sorting them.
- `build_raw_proposal()` copies the supplied candidate list, so later changes to the caller's list do not change the proposal's candidate list. It does not retrieve or select labour times.
- An unavailable offer never reaches candidate validation. A matched available offer does; invalid identifiers, currency, or price then raise rather than silently disappearing.

The renderer uses the proposal's existing candidate order, so callers that want price order must sort before constructing the proposal.

For example, `1.3355` hours multiplied by `90.00` yields `120.195` EUR,
while `LabourCost` expects `120.20` EUR, so construction fails. The separate
[Local Work Queue](./local-work-queue.md) owns web rounding and VAT behavior.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No candidates | Renderer says no part options are available and calls for human handling. |
| Offer has no matching product or is unavailable | Candidate builder skips it. |
| Product type, package unit, or required axle is incompatible | Product coverage reports no match and the builder omits that candidate. |
| Requirement quantity spans multiple packages | Coverage rounds the package count up; candidate total uses that count times offer unit price. |
| Matched available offer has blank identifiers or currency, or a nonpositive price | Validation raises `ValueError`. |
| Candidate does not belong to the proposal | Selection raises `ValueError`. |
| Work item has no requirement or more than one | `create_demo_proposal()` raises `ValueError`. |
| Shock absorber work reaches `format_raw_proposal()` | Its operation-label lookup raises `KeyError`; the formatter currently has a brake replacement label only. |
| Candidates are not price sorted | The formatter numbers them in the order already stored on the proposal. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `PackageCoverageResult` | `covered`, `commercial_quantity_needed`, `covered_quantity` | Coverage and purchasing count. |

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `services.proposal_builder` | `build_raw_proposal()` | Builds one `RawProposal` using the shared rounded pricing service; separate from the demo composition. |
| `services.labour_pricing` | `calculate_labour_cost()` | Prices supplied Decimal hours at 90.00 EUR/h including VAT and rounds half up to cents. |
| `domain.rules.supplier_candidates` | `build_supplier_candidates()` | Filters offers and builds covered candidates. |
| `domain.rules.package_coverage` | `calculate_product_coverage()` | Checks product, unit, axle, and package coverage. |
| `domain.rules.unit_conversion` | `convert_to_pcs()` | Converts PCS or PAIR; the candidate flow does not call it. |
| `domain.rules.candidate_sorting` | `sort_by_total_price()` | Orders candidates by total price. |
| `domain.rules.labour_cost` | `calculate_labour_cost()` | Multiplies by the fixed EUR hourly rate of 90.00 and constructs `LabourCost` without rounding first. |
| `presentation.raw_proposal_text` | `format_raw_proposal()` | Renders Finnish labels and two-decimal comma prices. |
| `domain.rules.human_selection` | `select_candidate_from_proposal()` | Copies a member candidate into a human selection. |
| `app.demo_workflow` | `create_demo_proposal()` | Composes candidate and labour rules for one requirement. |

## Integration Points

- **Depends on**: [Work and Part Data](../concepts/work-and-part-data.md) for contracts and ontology; [Supplier Adapters](../patterns/supplier-adapters.md) for offers.
- **Used by**: Callers assembling proposals and requesting a human decision; [Final Quote and Presentation](./final-quote.md) uses the resulting selection.
- **External systems**: None in these rules; labour time is supplied by the caller. The separate [Local Work Queue](./local-work-queue.md) owns manual entry and web pricing.

## Extension Guide

Extend offer acquisition through [Supplier Adapters](../patterns/supplier-adapters.md).
For work with more than one requirement, compose candidate building per
requirement instead of calling the single-requirement demo helper. Account
for the shared model's cent-rounded total requirement if accepting labour
durations that produce fractional cents; the current library rule can reject
them. Once a person chooses a candidate, use [Final Quote and Presentation](./final-quote.md)
for quote creation and output.
