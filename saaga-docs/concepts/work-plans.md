---
title: Work Plans
type: concept
sources:
  - domain/models/enums.py
  - domain/models/work_item.py
  - domain/models/work_plan.py
  - domain/models/labour_plan.py
  - domain/models/labour_time.py
  - domain/models/vehicle.py
  - domain/config/pair_replacement.py
  - app/work_plan_contract.py
  - app/work_plan_adapters.py
  - webapp/draft_models.py
  - webapp/http_drafts.py
  - webapp/main.py
terms:
  - PlannedWork
  - LabourTimeEntry
  - WorkPlanSnapshot
  - plan revision
---

# Work Plans

## Business Definition

A `WorkPlan` identifies the work requested for one vehicle, while a `LabourPlan` keeps the original labour times and the selection used to assess that work. A separate `WorkPlanSnapshot` holds potentially incomplete editor state and revision context before it can be projected into those domain models.

## Configuration

| Source | Description |
|--------|-------------|
| `domain/config/pair_replacement.py` | Lists confirmed same-axle pair practices; currently front shock absorbers only. A missing rule means the practice is unspecified. |

**How to access:**

- `get_pair_replacement_rule()` - returns an exact canonical part-type and axle match, or `None`.
- `PAIR_REPLACEMENT_RULES` (constant) - immutable tuple of confirmed rules; it does not change work scope or part quantities.

## Data Storage

These Pydantic contracts do not themselves persist plans. The application snapshot's schema version is `2`; it is distinct from the web draft payload's version `1` and is not exposed by current web routes.

| Object/Model/Type | Field/Property | Purpose |
|--------|-------|---------|
| `WorkPlan` | `vehicle`, `work_items` | One vehicle and at least one identified work. |
| `PlannedWork` | `work_id`, `work_item` | Stable ID paired with a `WorkItem`; IDs must be nonblank and unique within the plan. |
| `LabourTimeEntry` | `time_id`, `labour_time`, `covered_work_ids`, `side` | Original time, provenance, explicit full-work coverage, and single-work side. |
| `LabourPlan` | `work_plan`, `original_times`, `selected_time_ids` | Source snapshot and chosen time references; alternatives remain present. |
| `LabourPlan` | `review_decision`, `review_history` | Latest decision and prior decisions in append order. |
| `LabourPlan` | `content_id`, `review_rules_version` | Computed identity of work, all original times, selection, and review rules. |
| `LabourPlan` | `issues`, `status`, `is_billable`, `total_hours` | Derived coverage assessment and original selected-time total. |
| `LabourPlan` | `review_flags`, `review_status` | Derived review prompts and current approval state. |
| `LabourPlan` | `approved_final_hours`, `is_ready_for_final_approval` | Current approved time and final-approval readiness. |
| `LabourReviewDecision` | `plan_content_id`, `rules_version`, `original_total_hours` | Binds a decision to reviewed source content and its original total. |
| `LabourReviewDecision` | `status`, `reviewer_id`, `reviewed_at`, `final_hours`, `reason` | Human decision, supplied reviewer, aware timestamp, and any adjustment. |
| `WorkPlanSnapshot` | `organization_id`, `workshop_id`, `inquiry_id`, `plan_id` | Application ownership and stable plan identity. |
| `WorkPlanSnapshot` | `revision_id`, `revision`, `previous_revision_id`, `source_revision` | Revision chain and independent draft or inquiry source version. |
| `WorkPlanSnapshot` | `customer_description`, `vehicle`, `works`, `original_times`, `selected_time_ids` | Editable source data; incomplete values are permitted. |
| `WorkPlanSnapshot` | `work_reviews`, `historical_labour_approval` | Per-work checks and a historical decision link, neither a current labour approval. |
| `WorkData` | `work_id`, `display_order`, `part_needs` | Stable work identity, presentation order, and work-local part needs. |
| `TimeData` | `time_id`, `covered_work_ids`, `hours`, `source`, `side` | Full time scope; missing hours or source can remain in editor state. |
| `PlanRevisionRef` | ownership IDs, `revision_id`, `revision`, `content_id` | Exact snapshot revision referenced by a work check or historical approval. |
| `WorkReviewRecord` | `review_id`, `plan_revision`, `work_id`, `reviewer_id`, `reviewed_at` | One employee's check of a work in an exact revision. |
| `LabourApprovalRecord` | `decision_id`, `labour_revision_id`, `plan_revision`, `decision` | Historical link to a domain decision. |

`PlannedWork.work_item` uses the `WorkItem` and `Side` contracts defined in [Work and Part Data](./work-and-part-data.md).

`WorkPlan` and `PlannedWork` forbid extra fields. `LabourTimeEntry` also forbids extras, requires a nonblank unique `time_id` within its plan, nonempty coverage without duplicate or blank work IDs, and finite original hours. A time covering multiple work IDs must use `side=UNKNOWN`; a single-work time covers the work fully only when its side matches the work's side.
`LabourPlan` permits unselected overlapping alternatives. It computes coverage and review state from its current contents; see [Labour Planning](../features/labour-planning.md) for selection and approval steps. Its `content_id` includes every original time, even unselected alternatives, but excludes decisions. Sorting of work, time, coverage, and selection lists and equivalent decimal spellings do not change that ID.

`WorkPlanSnapshot` rejects extra fields and requires unique work IDs, display orders, time IDs, selected time IDs, and review IDs. Its time coverage and selection must reference known IDs. Revision `1` has no predecessor; later revisions require one different from their own `revision_id`. IDs belong to the caller to allocate and retain across edits; display order is not identity.
The snapshot's `content_id` includes ownership, source, revision, editor content, and time selection, but excludes work reviews and historical labour approval. It is therefore a different identifier from `LabourPlan.content_id`. `revision_ref` packages the snapshot scope and hash. A work check counts only for a matching current revision and content ID; `all_works_checked` requires at least one work and every current work checked. It does not approve labour or a customer offer.

`historical_labour_approval` accepts the older input key `labour_approval` for reading, then serializes under its current name. The adapter never transfers that historical record to the projected `LabourPlan`. A historical decision remains readable even when its rules or content no longer grant current approval.

The snapshot's nested records preserve incomplete input without inventing domain defaults:

| Object/Model/Type | Field/Property | Purpose |
|--------|-------|---------|
| `SourceRevision` | `kind`, `source_id`, `revision` | Identifies a `V1_DRAFT` or `INQUIRY` origin independently of the plan revision. |
| `VehicleData` | `registration`, `make`, `model`, `year` | Allows vehicle details to be absent while an editor is incomplete. |
| `WorkData` | `description`, `operation`, `axle`, `side` | Keeps an optional description and incomplete operation scope. |
| `PartNeed` | `requirement_id`, `canonical_product_type`, `axle` | Gives each work-local part need its own stable ID and optional vocabulary. |
| `PartNeed` | `required_quantity`, `required_unit` | Allows unknown quantity and unit; a supplied quantity must be a positive strict integer. |
| `TimeData` | `work_description`, `autodata_reference`, `recorded_by` | Keeps original-time provenance, including manual entry details. |
| `WorkReviewRecord` | `status`, `note` | A `CHECKED` work record may include a note; absence means unchecked. |

All `ContractRecord` descendants reject extra fields, freeze assigned fields, and reject blank strings where optional text should be `None`. A supplied `TimeData.hours` must validate as a positive finite `Decimal`; booleans and floats are rejected. Each work's part-need IDs must be unique, and one time may not repeat a covered work ID.

`WorkReviewRecord` requires an aware timestamp. `unchecked_work_ids` revalidates copied review records before counting them; a malformed, foreign, or old-revision check does not count. A new snapshot revision retains historical records for reading, while its changed revision reference makes prior checks stale.

The application adapter checks these requirements before projection:

| Projection | Required editor data | Result when absent |
|--------|----------------------|--------------------|
| Work plan | Vehicle registration, make, model, and at least one work. | Reports missing field paths or `works`. |
| Each work | Operation, axle, side, and all part-need domain fields. | Reports a path under `works[work_id]`. |
| Labour plan | A complete work plan plus each time's hours and source. | Reports a path under `original_times[time_id]`. |
| Manual Autodata time | Work description and recorder in addition to hours and source. | Reports the missing provenance path. |

Projection sorts works by `display_order` and drops application-only part-need IDs when constructing domain requirements. It passes through only explicit time selections. The resulting domain labour plan starts without a review decision, even if the snapshot records all works checked or links a historical labour approval.

The domain and application records express different readiness thresholds:

| Record | May be incomplete? | Approval meaning |
|--------|--------------------|------------------|
| `WorkPlanSnapshot` | Yes; vehicle, works, and times can be partial. | Work checks are per work; historical approval is only a link. |
| `WorkPlan` | No; it needs an identified work and vehicle. | It contains no review decision. |
| `LabourPlan` | Coverage may still have issues. | Only a current domain decision can expose approved hours. |

## Key Services/Functions (PUBLIC/EXPORTED only)

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `WorkPlan` | `split_side_groups` (property) | Finds opposite-side works with the same operation and axle. |
| `WorkPlan` | `confirmed_split_pairs` (property) | Narrows those groups to the configured front-shock practice. |
| `LabourTimeEntry` | `covers_full_work()` | Checks explicit work ID and side scope. |
| `LabourPlan` | `require_total_hours()` | Returns the selected original total only for complete, nonoverlapping coverage. |
| `LabourPlan` | `require_approved_final_hours()` | Returns current approved hours or raises `ValueError`. |
| `LabourPlan` | `require_final_approval_ready()` | Raises `ValueError` unless coverage and review are current. |
| `WorkPlanSnapshot` | `content_id` (property) | Computes editor snapshot identity, excluding review outcomes. |
| `WorkPlanSnapshot` | `revision_ref` (property) | Builds the scoped revision reference. |
| `WorkPlanSnapshot` | `unchecked_work_ids` (property) | Returns current unchecked IDs in display order. |
| `WorkPlanSnapshot` | `all_works_checked` (property) | Reports whether all current works have matching checks. |
| `app.work_plan_adapters` | `missing_work_data()` | Lists missing vehicle, work, and part fields needed for projection. |
| `app.work_plan_adapters` | `to_domain_work_plan()` | Revalidates and projects complete editor work into a domain `WorkPlan`. |
| `app.work_plan_adapters` | `to_domain_labour_plan()` | Projects complete original times and explicit selections without carrying approvals. |

The adapters revalidate snapshots because Pydantic `model_copy(update=...)` can bypass validators. Missing required work or time data raises `IncompleteWorkPlanError` with structured `MissingData` paths. Projection does not infer time selections, multiply time by part quantity, or attach historical review outcomes.

## Reference Implementations

- `domain/models/work_plan.py` - defines stable work IDs and split-side grouping.
- `domain/models/labour_plan.py` - defines original time, coverage, identity, and review contracts.
- `app/work_plan_contract.py` - defines editor revision and work-check contracts.
- `app/work_plan_adapters.py` - projects complete editor data into domain plans.
- `domain/config/pair_replacement.py` - records the confirmed pair practice.

## Related Concepts

- [Work and Part Data](./work-and-part-data.md) defines `WorkItem` and the legacy proposal and quote models.
- [Labour Planning](../features/labour-planning.md) owns time selection and domain review flow.
- Approved Labour owns persisted approval and pricing at the web service boundary.
