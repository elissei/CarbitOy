---
title: "Feature: Labour Planning"
type: feature
sources:
  - domain/config/pair_replacement.py
  - domain/models/work_plan.py
  - domain/models/labour_plan.py
  - domain/models/labour_time.py
  - services/labour_planning.py
---

# Feature: Labour Planning

## Overview

The domain labour planner selects original, full-scope time records for identified vehicle work and reports where a person must resolve coverage. An explicit human decision then establishes final hours for a particular plan snapshot; this service does not calculate a quote.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work Plans](../concepts/work-plans.md) defines work IDs, original times, snapshot identity, and review records.
- [Work and Part Data](../concepts/work-and-part-data.md) defines the underlying work and labour-time vocabulary.

## Functional Specification

### Mechanism

1. A caller supplies a `WorkPlan`, original `LabourTimeEntry` records, and optionally explicit `selected_time_ids` to `build_labour_plan()`.
2. The builder deep-copies the supplied work and time records into a `LabourPlan`. Explicit IDs remain selected, including invalid references so the plan can report them as issues.
3. For opposite-side works with the same operation and axle, the builder checks the confirmed pair practice. It automatically selects one exact full-group time only for a confirmed group with exactly one such candidate and no explicitly covered work in that group.
4. For each remaining work outside a split-side group and not explicitly covered, it selects a single candidate only when that record names exactly the work ID and covers its full side scope. A combined time across unrelated works is never chosen implicitly.
5. The plan computes coverage issues. `READY` means no issues, so `total_hours` sums selected original times once each as `Decimal` values. It does not deduct a possible overlap across distinct operations or round the hours.
6. The plan may also show `MULTI_WORK_REVIEW_REQUIRED` when it contains more than one work. This flag does not make coverage ready or approve hours; `POSSIBLE_LABOUR_OVERLAP` is reserved and currently never emitted.
7. A trusted caller chooses unchanged or adjusted approval, passing the current `content_id` and a reviewer ID. The function validates the plan again, rejects a stale expected ID or incomplete coverage, and returns a new plan with a current decision and prior decision appended to history.
8. Downstream code reads `require_approved_final_hours()` or checks `require_final_approval_ready()` before using approved hours. Approved Labour owns persistence and pricing in the web service.

The selection rule depends on the shape of the requested work:

| Work shape | Automatic candidate | Explicit choice |
|------------|---------------------|-----------------|
| One work with a single side | One original time naming that work and the same side. | Resolves multiple valid alternatives. |
| One `BOTH` work | One original time naming that work with `side=BOTH`. | Resolves multiple full-pair alternatives. |
| Confirmed split left/right pair | One original time naming exactly both work IDs. | Resolves multiple group alternatives or selects a larger combined record. |
| Unconfirmed split left/right group | None. | A covering group time is required. |
| Distinct operations with a combined time | Unique individual times can be selected for uncovered work. | The combined time must be named explicitly. |

The pair lookup is descriptive until the builder sees a confirmed split group. It does not add a second side to a one-sided work. A selected combined record can cover more than a pair; when it covers both split-side works, the group requirement is met without selecting another pair entry.

The builder copies the supplied selection list before adding automatic choices. It tracks work covered by explicitly selected existing records, so an explicit combined time suppresses automatic single-work choices for those IDs. An unknown explicit ID remains in the selection and becomes an issue. Original entries remain unchanged, including unselected alternatives and their provenance.

### Validation Rules

- A selected single-work time must have the same `side` as the work. A multi-work time uses `UNKNOWN` for its own side and explicitly names all covered work IDs.
- Selected coverage must give every work exactly one full-scope time; duplicate selection, unknown references, incomplete scope, and overlapping coverage block the total.
- A `Side.BOTH` work needs its own full-scope time. Separate left and right entries are never added to manufacture a pair time.
- Split left/right works of the same operation and axle need an explicitly selected covering group time unless the configured practice allows automatic choice of a unique exact group time. Separate side times cannot satisfy this group requirement.
- An unchanged decision stores the original total and leaves `final_hours` unset. An adjusted decision needs the original total, explicit nonnegative final hours, and a nonblank reason; zero adjusted hours are accepted by the domain decision.
- Decision records require a nonblank reviewer ID, timezone-aware review timestamp, nonblank rules version, and a 64-character lowercase hexadecimal content ID. `PENDING` is a state, not a valid approval decision.
- The current review requires matching content ID, rules version, and original total. A legacy or stale decision remains readable but yields `PENDING` and no approved hours.

Issue codes distinguish failures a caller may need to resolve:

| Code | Meaning for the current plan |
|------|------------------------------|
| `UNKNOWN_WORK` | An original entry names a work ID absent from the plan. |
| `UNKNOWN_TIME` | A selected time ID has no original entry. |
| `DUPLICATE_SELECTION` | One time ID appears more than once in the selection. |
| `INCOMPLETE_SCOPE` | A selected entry names a work but does not cover its full side scope. |
| `OVERLAPPING_COVERAGE` | More than one selected entry fully covers the same work. |
| `MISSING_TIME` | An uncovered work has no valid selected single-work time. |
| `MISSING_PAIR_TIME` | A `BOTH` work or split group lacks required full coverage. |
| `AMBIGUOUS_TIME` | More than one full-scope candidate requires an explicit choice. |

`issues` are derived from the current plan whenever accessed. A missing work can produce more than one issue, such as a missing single-work time and a missing split-group time. `status=REVIEW_REQUIRED` whenever the issue list is nonempty; `total_hours` is then `None`, and `require_total_hours()` raises `ValueError`.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No original time, or no full-side match | Reports `MISSING_TIME`; for a `BOTH` work without a full pair candidate, reports `MISSING_PAIR_TIME`. |
| More than one full single-work candidate | Reports `AMBIGUOUS_TIME` until an explicit reference chooses one. |
| More than one exact time for a split pair | Reports `AMBIGUOUS_TIME` for the group; no candidate is guessed. |
| Unconfirmed rear shock pair with an available group time | Requires explicit selection of that group time. |
| A selected time names an unknown work or time ID | Reports `UNKNOWN_WORK` or `UNKNOWN_TIME`; no total is available. |
| Selected times cover one work twice | Reports `OVERLAPPING_COVERAGE`, even if the entries are otherwise valid. |
| The same time ID is selected twice | Reports `DUPLICATE_SELECTION` and withholds the total. |
| A combined time is explicitly selected | Counts the combined record once for its covered works; other uncovered works may receive unique single-work times. |
| A selected side-only entry targets a `BOTH` work | Reports `INCOMPLETE_SCOPE`; side hours are not substituted. |
| A reviewer passes an old expected content ID | Approval raises `ValueError` and does not return a reviewed plan. |
| Original data or review rules change after approval | `review_status` becomes `PENDING`; the old decision is not current authority. |

Review status has a separate gate from coverage status:

| Coverage and decision | Exposed result |
|-----------------------|----------------|
| Coverage has issues | `is_billable=False`; no original or approved total. |
| Coverage ready, no decision | Original `total_hours` exists; `review_status=PENDING`; approved hours absent. |
| Coverage ready, unchanged current decision | `approved_final_hours` equals the original total. |
| Coverage ready, adjusted current decision | `approved_final_hours` equals the recorded final hours, including zero. |
| Coverage ready, stale or malformed decision | `review_status=PENDING`; approved hours absent. |

`is_billable` says only that original coverage is complete. The higher `is_ready_for_final_approval` flag additionally requires a current unchanged or adjusted decision. A review flag is a prompt; it does not change either gate.

## Technical Implementation

### Data Model

The planning functions use the model contracts in [Work Plans](../concepts/work-plans.md).

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `services.labour_planning` | `build_labour_plan()` | Deep-copies inputs, selects eligible unique times, and returns a plan with derived issues. |
| `services.labour_planning` | `approve_labour_plan_unchanged()` | Records acceptance of the unchanged original total for an exact snapshot. |
| `services.labour_planning` | `approve_labour_plan_adjusted()` | Records explicit final hours and a reason for an exact snapshot. |

The `LabourPlan` model interface is documented in [Work Plans](../concepts/work-plans.md).

The approval functions revalidate a serialized copy of the plan before recording a decision. They do not mutate the caller's plan. The returned plan retains all original times, including unselected alternatives; its new decision names the current content and rules version, and a replaced decision moves into `review_history`.

Both approval functions use the current `total_hours` as `original_total_hours`. Unchanged approval does not write an adjusted value. Adjusted approval stores the caller's exact final hours and reason without rewriting original entries or selected references. This keeps source time provenance distinct from the reviewed billing decision.

A source change that changes the plan's identifier makes a prior decision pending; [Work Plans](../concepts/work-plans.md) defines that identifier.

Callers should use the model's gates for the operation they are performing:

| Caller needs | Read or call | Outcome when unavailable |
|--------------|--------------|--------------------------|
| Coverage feedback | `issues` and `status` | Structured issue codes identify missing or conflicting scope. |
| Original selected hours | `require_total_hours()` | Raises when coverage is incomplete or contradictory. |
| Review prompts | `review_flags` | A multi-work prompt does not block or grant coverage. |
| Current review state | `review_status` | Returns `PENDING` for absent, stale, or invalid decisions. |
| Approved billing hours | `require_approved_final_hours()` | Raises when no current decision grants hours. |
| Final approval readiness | `require_final_approval_ready()` | Raises unless both coverage and review are current. |

Approval takes an expected content ID supplied from the version the human reviewed. The caller must pass that exact ID rather than recomputing an ID from new editor input. A mismatch fails before a new decision is made. The caller also supplies the reviewer identity; this module does not verify that identity.

The adjusted decision may be zero hours, while the original `LabourTime` contract requires positive hours. Pricing and any zero-hour business policy belong to the later pricing boundary, not this domain function.

Replacing a decision retains the previous decision in append-order history while leaving the original time records untouched.

## Integration Points

- **Depends on**: Domain work and time contracts in [Work Plans](../concepts/work-plans.md), including the configured pair lookup.
- **Used by**: Callers that need domain labour coverage and explicit reviewed hours; the stored review and price boundary belongs to Approved Labour.
- **External systems**: Original time provenance can name Autodata, but this module makes no Autodata API call. It neither authenticates a reviewer nor persists a decision; its caller must supply trusted identity, permissions, and atomic storage.

## Extension Guide

Add a confirmed pair practice only after the workshop confirms its canonical part type and axle. Keep full work scope explicit in every new time entry; do not infer it from a part quantity or a pair rule. If adding a review rule, change the rules version so old decisions become pending, and have the calling application authorize and persist each new decision. Any future quote or send path must consume `require_approved_final_hours()` and apply its own pricing and permission rules; this domain service produces neither a quotation nor a customer-facing endpoint.
