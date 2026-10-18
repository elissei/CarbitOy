---
title: "Feature: Proposal Decisions"
type: feature
last_verified: 2026-10-09
sources:
  - domain/config/pair_replacement.py
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

## Domain stage 1 — implemented in 6fb2e993

Source: [feature/autodata-labour, 6fb2e993](https://github.com/elissei/CarbitOy/tree/6fb2e9930e20235661aece6856c396d8db15a3a4).
The source branch passed 158 tests. These additions are documented without
merging source files into visuals.

WorkItem accepts Side, defaulting to UNKNOWN, and the new
SHOCK_ABSORBER_REPLACEMENT operation. Explicit pair lookup confirms only
front shock absorbers. A missing rule means the practice is unspecified;
lookup does not change a work item, select an Autodata time or price it.

The existing services.proposal_builder.build_raw_proposal() receives a
proposal ID, Vehicle, WorkItem, LabourTime and optional candidate list.
It prices the supplied time through services.labour_pricing, creates the
current RawProposal with an aware UTC timestamp and a fresh candidate list,
and does not mutate inputs. It does not fetch or choose labour times.

The frozen demo still calls domain.rules.labour_cost and accepts exactly
one part requirement. Its path is not migrated to the new builder.
format_raw_proposal() still has a label only for BRAKE_REPLACEMENT;
passing SHOCK_ABSORBER_REPLACEMENT to that formatter raises KeyError.
This stage does not provide end-to-end shock absorber intake or rendering.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work and Part Data](../concepts/work-and-part-data.md) owns the requirement, offer, proposal, and selection contracts.

## Functional Specification

### Mechanism

1. Obtain a requirement, products, and offers; [Supplier Adapters](../patterns/supplier-adapters.md) owns offer acquisition. Call `build_supplier_candidates()` and, when price ordering is needed, `sort_by_total_price()`.
2. Obtain `LabourTime`, call `domain.rules.labour_cost.calculate_labour_cost()`, and construct `RawProposal` with the work, labour, and candidates. `create_demo_proposal()` composes this for exactly one part requirement.
3. Call `format_raw_proposal()` for Finnish decision text, then `select_candidate_from_proposal()` with a candidate from that proposal and the selector's identity and time.

After selection, [Final Quote and Presentation](./final-quote.md) owns quote assembly; `create_demo_final_quote()` is the in-repository composition example.

### Validation Rules

- Candidate building pairs offers with products by `product_id`, keeps only `IN_STOCK` or `BACKORDER`, and validates a matched, available offer's nonblank identifiers and currency and positive unit price.
- Coverage requires equal canonical type and unit values; when the ontology requires axle matching, an unknown required axle fails while an unknown or `BOTH` product axle matches a known one. Coverage calculation rejects nonpositive quantities, rounds needed packages up, and multiplies the count by offer unit price.
- Selection requires candidate membership and nonblank `proposal_id` and `selected_by`.
- `RawProposal` requires its labour time and cost hours to agree. The library labour rule multiplies hours by its fixed `90.00` EUR hourly rate without rounding before it constructs `LabourCost`. The shared [labour cost contract](../concepts/work-and-part-data.md) requires the product rounded half up to cents; an input whose raw product has fractional cents can therefore fail `LabourCost` validation.
- `create_demo_proposal()` requires exactly one part requirement. It passes the resulting candidates into `RawProposal` without sorting them.

The renderer uses the proposal's existing candidate order, so callers that want price order must sort before constructing the proposal.

For example, `1.3355` hours multiplied by `90.00` yields `120.195` EUR,
while `LabourCost` expects `120.20` EUR, so construction fails. The separate
[Local Work Queue](./local-work-queue.md) owns web rounding and VAT behavior.

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
| `domain.config.pair_replacement` | `get_pair_replacement_rule()` | Queries workshop practice without selecting a labour time. |
| `services.proposal_builder` | `build_raw_proposal()` | Builds one RawProposal using the shared rounded pricing service; independent of the frozen demo. |
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

## Planned LabourPlan decisions — not implemented

| Scenario | Future decision |
| --- | --- |
| Same operation on both sides; matching Autodata pair time exists | Use the pair time instead of individual side times. |
| Pair time missing | Await human review; do not invent time, sum the sides as a fallback or substitute zero. |
| Distinct operations | Sum their separate Autodata times by default. |
| Shared removal/installation steps | No automatic deduction; later authorized adjustment with rationale. |
| Exact Autodata combined-work time exists | It may replace the times of the covered operations, retaining its original provenance. |
| A combined group covers only some operations | Add the uncovered operations; count every operation exactly once. |

The proposed aggregation prices the full Decimal sum once, then rounds the
overall inclusive labour charge half up to cents. At 90.00 EUR/h, two separate
1.3355-hour operations give 2.6710 hours and 240.39 EUR, not the 240.40 EUR sum
of separately rounded prices. This is future aggregation policy; the current
builder prices one caller-supplied time. Inclusive VAT is split at 25.5% using
the existing service, never added again.

## Later human time adjustments — not implemented

A future LabourTimeAdjustment records an approved absolute replacement time,
its target work/group, original basis, reason, authorized entrepreneur or
mechanic, and aware UTC timestamp. Originals are retained. A missing pair
time has no fabricated prior value. New approval supersedes an earlier
adjustment in calculation but preserves its history; changed scope or basis
requires renewed review. Authorization belongs to the application boundary.
Neither HumanSelection (parts choice) nor manual Autodata entry implements
this workflow. Current web event history likewise is not approval authorization.

See [decisions](../../docs/notes/paatokset.md) and the
[status-separated diagram](../../docs/visualizations/domain-vaihe-1.md).
