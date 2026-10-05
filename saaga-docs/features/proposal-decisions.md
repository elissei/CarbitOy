---
title: "Feature: Proposal Decisions"
type: feature
sources:
  - domain/rules/*.py
  - domain/models/*.py
  - domain/ontology/parser.py
  - domain/ontology/part_types.yaml
  - presentation/raw_proposal_text.py
---
# Feature: Proposal Decisions

## Overview

Callers can turn a requirement, indexed products, and supplier offers into priced candidates, then present a raw proposal for a person to choose from. The library supplies rules and contracts; callers compose the sequence.
## Key Concepts

Before working with this feature, understand [Work and Part Data](../concepts/work-and-part-data.md), which owns the requirement, offer, proposal, selection, and quote contracts.
## Functional Specification

### Mechanism

1. Obtain a parsed requirement and offers ([Supplier Adapters](../patterns/supplier-adapters.md) owns acquisition); call `build_supplier_candidates()` and `sort_by_total_price()`.
2. Obtain `LabourTime`, call `calculate_labour_cost()`, and have the caller construct `RawProposal` with work, labour, and candidates.
3. Call `format_raw_proposal()` for Finnish decision text, then `select_candidate_from_proposal()` for the chosen candidate; a caller may construct `FinalQuote`, but no function assembles it.
### Validation Rules

- Candidate building pairs offers with products by `product_id`, keeps only `IN_STOCK` or `BACKORDER`, and validates a matched, available offer's nonblank identifiers and currency and positive unit price.
- Coverage requires equal canonical type and unit values; when the ontology requires axle matching, an unknown required axle fails while an unknown or `BOTH` product axle matches a known one. Coverage calculation rejects nonpositive quantities, rounds needed packages up, and multiplies the count by offer unit price.
- Selection requires candidate membership, nonblank `proposal_id` and `selected_by`; `FinalQuote` requires matching selection and quote proposal IDs.
### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No candidates | Renderer says no part options are available and calls for human handling. |
## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `PackageCoverageResult` | `covered`, `commercial_quantity_needed`, `covered_quantity` | Coverage and purchasing count. |
### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `domain.rules.supplier_candidates` | `build_supplier_candidates()` | Filters offers and builds covered candidates. |
| `domain.rules.unit_conversion` | `convert_to_pcs()` | Converts PCS or PAIR; the candidate flow does not call it. |
| `domain.rules.candidate_sorting` | `sort_by_total_price()` | Orders candidates by total price. |
| `domain.rules.labour_cost` | `calculate_labour_cost()` | Applies the fixed EUR hourly rate of 90.00. |
| `presentation.raw_proposal_text` | `format_raw_proposal()` | Renders Finnish labels and two-decimal comma prices. |
| `domain.rules.human_selection` | `select_candidate_from_proposal()` | Copies a member candidate into a human selection. |
## Integration Points

- **Depends on**: [Work and Part Data](../concepts/work-and-part-data.md) for contracts and ontology; [Supplier Adapters](../patterns/supplier-adapters.md) for offers.
- **Used by**: Callers assembling proposals and requesting a human decision; no application orchestrator is present.
- **External systems**: None in these rules; labour time is supplied by the caller.
## Extension Guide

Change the relevant rule before candidate construction or sorting. Extend offer acquisition through [Supplier Adapters](../patterns/supplier-adapters.md), and construct `FinalQuote` in caller code after selection.
