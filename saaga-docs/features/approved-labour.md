---
title: "Feature: Approved Labour"
type: feature
last_verified: 2026-10-19
sources:
  - webapp/labour_models.py
  - webapp/labour_migrations.py
  - webapp/labour_service.py
  - webapp/approved_labour_pricing.py
  - webapp/database.py
  - webapp/main.py
  - webapp/static/labour-review-demo.html
  - webapp/static/labour-review-demo.js
  - services/labour_pricing.py
  - domain/models/labour_time.py
---

# Feature: Approved Labour

## Overview

Internal web services store an inquiry's original work times and human review decisions, then calculate a labour-only price from currently approved hours. No registered HTTP route exposes these services; the existing queue's manual labour entry is a separate flow.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work Plans](../concepts/work-plans.md) defines domain plan content, original times, and approval identity.
- [Workshop Identity](../concepts/workshop-identity.md) defines the trusted employee and workshop context supplied to these services.

## Functional Specification

### Mechanism

1. Trusted server code obtains a `TrustedWorkshopContext` and supplies an inquiry ID, its expected version, the domain content ID and review rules version, and a request replay key.
2. `LabourPlanService.save_plan()` checks workshop scope and save permission, then reconstructs a domain plan from the supplied `WorkPlan` and exactly one original-time record per work. Missing original hours remain missing; it does not infer combined times.
3. After checking the inquiry's registration, version, plan content ID, and rules version, the service inserts an immutable revision and its original-time rows. It makes that revision active, clears any active decision, advances the inquiry version, and appends a save event and replay receipt in the same business transaction.
4. A reviewer calls `approve_plan()` with the current inquiry version, active revision ID, content ID, and rules version. The service reloads and validates stored originals and the plan, then uses the domain unchanged or adjusted approval function described in [Labour Planning](./labour-planning.md).
5. The service records the human decision, reviewer from the trusted context, event, active decision pointer, new inquiry version, and replay receipt together. Earlier decisions remain in history.
6. A caller asks `require_approved_final_hours()` for current approved hours using the expected inquiry version, revision ID, content ID, and rules version. Pricing calls this gate rather than trusting hours in a request or an earlier operation response.
7. `ApprovedLabourPricingService.price_approved_labour()` multiplies those hours by the configured default hourly rate, rounds the inclusive total to cents, derives net and VAT, and returns a labour-only snapshot. `require_current_snapshot()` recalculates and compares it against current storage and pricing policy.

### Validation Rules

- A save needs `SAVE_LABOUR_PLAN`; approval needs `APPROVE_LABOUR_PLAN`; reads and pricing need `READ_LABOUR_PLAN`. The repository checks the workshop assignment and authorization inside each operation.
- Original records must match their work IDs, operation identifiers, axle, and side. Supplied hours are positive exact decimals with a source; manual Autodata entries need a work description and recorder.
- `READY` requires every work's original time and an exact sum. Coverage readiness alone does not grant approval.
- An unchanged decision supplies neither final hours nor a reason. An adjusted decision requires nonnegative final hours and a nonblank reason; zero approved hours are permitted.
- Content or rules mismatches raise `StaleLabourPlanError`; a changed inquiry version raises `VersionConflictError`. An approval also requires the expected active revision.
- Monetary fields use decimal values and cent precision. The snapshot's net price plus VAT must equal its inclusive total.
- Request and storage models reject extra fields. Decimal input rejects booleans and floats; stored decimal values serialize as text.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Same operation and replay key with identical authenticated request | Returns the stored historical response before checking the expected version again; the response alone is not current approval. |
| Authenticated replay key reused by another actor or with different content | Raises `IdempotencyConflictError`. |
| New revision after approval | Prior decisions remain readable, but the active decision is cleared and current approved hours are unavailable. |
| Incomplete or mismatched stored revision | Approval or approved-hours reading fails after reconstruction and validation. |
| Pricing a zero-hour adjustment | Produces zero labour gross, net, and VAT while retaining the original positive total. |
| Inquiry changes after a price snapshot | `require_current_snapshot()` fails its current-version or snapshot comparison; its read check does not reserve a version for a future write. |
| Active revision lacks a decision | The approved-hours gate rejects the plan, even if every original time is present. |
| Review rules change | The expected rules version no longer matches the rebuilt plan, so approval and pricing require a current revision. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `OriginalWorkTimeRecord` | `work_item_id`, `original_hours`, `source`, `side` | One work's immutable original-time input; nullable hours represent unfinished coverage. |
| `SaveLabourPlanCommand` | `expected_version`, `expected_content_id`, `original_times`, `idempotency_key` | Save request bound to inquiry state and a replay key. |
| `ApproveLabourPlanCommand` | `expected_revision_id`, `status`, `final_billable_hours`, `reason` | Review request bound to an exact stored revision. |
| `LabourPlanRevisionRecord` | `revision_id`, `revision`, `content_id`, `rules_version`, `items` | Immutable inquiry plan revision; its revision number is separate from the inquiry version. |
| `LabourReviewDecisionRecord` | `decision_id`, `revision_id`, `status`, `final_billable_hours`, `reviewer_id` | Persisted human outcome and event link. |
| `LabourOperationResult` | `work_version`, `revision_id`, `coverage_status`, `decision_id` | Save or approval response, also stored for request replay. |
| `ApprovedLabourPriceSnapshot` | `work_version`, `decision_id`, `final_billable_hours`, `total_price`, `net_price`, `vat_price` | Serializable labour-only price tied to an approval and pricing rules version. |
| `ApprovedLabourPricingRequest` | `expected_version`, `expected_revision_id`, `expected_content_id`, `expected_rules_version` | Pricing expectations; it carries no caller-supplied billable hours. |

SQLite keeps original rows, revisions, decisions, active pointers, and replay receipts in separate tables. Migrations enforce references, original-time shape, and immutable historical rows and review events. `WorkRepository.initialize()` applies them inside its initialization transaction.
The active pointer can change while historical revisions and decisions remain intact.

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `webapp.labour_service` | `build_storage_labour_plan()` | Rebuilds the selected one-record-per-work domain plan from storage inputs. |
| `LabourPlanService` | `save_plan()` | Stores a new active revision and resets current approval. |
| `LabourPlanService` | `approve_plan()` | Records a trusted review decision against the active revision. |
| `LabourPlanService` | `get_active_plan()` | Reads and revalidates the latest active revision and decision. |
| `LabourPlanService` | `decision_history()` | Returns the inquiry's decisions in event order. |
| `LabourPlanService` | `require_approved_final_hours()` | Returns current approved decimal hours after version and identity checks. |
| `ApprovedLabourPricingService` | `price_approved_labour()` | Calculates the inclusive labour price from current approved hours. |
| `ApprovedLabourPricingService` | `require_current_snapshot()` | Reprices and rejects a stale or policy-mismatched snapshot. |

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `labour-review-demo.html` and `labour-review-demo.js` | Static, invented-data preview of original times, review history, and price choices. It has no live labour-plan API, reads no stored plans, and persists no decision. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/works/{work_id}/labour` | PUT | Legacy manual single labour snapshot for the queue; it does not save or approve a stored labour plan. |

There is no registered save, approve, or approved-pricing HTTP endpoint in `create_app()`. The queue's manual endpoint belongs to [Local Work Queue](./local-work-queue.md).
Static file mounting makes the preview asset available under `/static/`; it does not wire the preview to the stored service.

## Integration Points

- **Depends on**: Domain review from [Labour Planning](./labour-planning.md), trusted workshop authorization, the inquiry repository, and the labour pricing constants.
- **Used by**: Internal callers that need a current approved labour price; no current queue route calls these services.
- **External systems**: SQLite business storage. Original time provenance can name Autodata, but the service does not query it.
- **Pricing boundary**: The snapshot contains labour only; parts and quote persistence are outside this service.

## Extension Guide

Resolve the workshop context on the server, keep expected inquiry and plan identities in each command, and expose a protected route only when its request and response contract is defined. A future quote write must check the snapshot's inquiry version atomically with quote persistence; `require_current_snapshot()` by itself is only a read-time check.
