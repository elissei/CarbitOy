---
title: "Feature: Local Work Queue"
type: feature
sources:
  - webapp/main.py
  - webapp/database.py
  - webapp/business_identity.py
  - webapp/business_authorization.py
  - webapp/workshop_context.py
  - webapp/workshop_identity.py
  - webapp/workshop_repository.py
  - webapp/workshop_migrations.py
  - webapp/http_access.py
  - webapp/static/app.js
  - webapp/static/index.html
  - services/labour_pricing.py
  - domain/models/labour_time.py
  - domain/models/labour_cost.py
---

# Feature: Local Work Queue

## Overview

The browser queue records a workshop's work inquiry before its labour time is known. An authorized employee can later enter a manually checked Autodata time and see an inclusive labour calculation and event history; this is not an approved final quote.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work and Part Data](../concepts/work-and-part-data.md) defines the shared labour time and cost vocabulary.
- [Workshop Identity](../concepts/workshop-identity.md) defines the session-derived employee, workshop, and event actor.

## Functional Specification

### User Flow

1. An employee signs in and opens `/`. Access to the queue and its JSON routes follows [Employee Authentication](./employee-authentication.md) and [Workshop Access](./workshop-access.md).
2. An employee with write permission chooses **Uusi tiedustelu**, enters a registration and the customer's original description, then submits. The browser sends a new UUID retry key for that dialog.
3. The inquiry appears in that workshop's list with status `KESKEN`. Opening it loads its original description, current version, and events.
4. After checking Autodata manually, the employee enters positive hours, a work description, and optionally an Autodata reference. The browser submits the version it opened.
5. A successful save displays the latest inclusive labour total, net amount, VAT, and event history. Reopening the inquiry reads these stored values.

The inquiry dialog also offers manually saved editor state; [Inquiry Drafts](./inquiry-drafts.md) owns its save and recovery flow. The labour form remains a manual single-snapshot entry, separate from stored plan review and pricing in [Approved Labour](./approved-labour.md).

### Validation Rules

- `CreateWorkRequest` requires `registration`, `customer_description`, and a UUID `idempotency_key`. Registration is at most 20 characters; description is at most 4000. Both are stripped and must remain nonblank; registration is uppercased.
- `RecordLabourRequest` requires integer `version >= 1`, decimal `hours > 0`, and nonblank `work_description` of at most 2000 characters. `autodata_reference` is optional, at most 500 characters, and blank input becomes absent.
- Both models forbid undeclared fields. They still accept deprecated optional `actor` and `recorded_by` strings of at most 200 characters, but the HTTP handlers do not use them for attribution. The browser sends neither field.
- The write routes derive a `BusinessActor` from the authenticated session and current workshop context. Creating or changing an inquiry requires save permission; listing and reading require read permission.
- Protected writes require a valid session, allowed role and workshop membership, matching request origin, and a session CSRF proof. The browser obtains the proof before POST or PUT.
- Labour is constructed with source `MANUAL_AUTODATA` and the trusted employee ID as recorder. A duration that cannot produce a positive cent-valued labour total returns `422` without a save.
- Invalid request bodies receive `422` before repository mutation. Browser required fields and length limits aid entry, but server validation is authoritative.
- The route trims manual labour text and stores an omitted or blank Autodata reference as an empty string in the snapshot.
- Authenticated HTTP writes use the session actor even when a legacy client supplies the deprecated name fields.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Same workshop and creation UUID, same normalized input and authenticated actor | Returns the stored creation response with `201`, including its original version, without another inquiry or event. |
| Same workshop and UUID, changed normalized input or authenticated actor | Returns `409`; opening a new dialog gives the browser a new UUID. |
| Same details with a new UUID | Creates another inquiry. |
| Inquiry absent from the active workshop on detail or labour write | Returns `404`, without revealing another workshop's inquiry. |
| Labour submitted against an older version | Returns `409`; reopen the inquiry before saving the revised entry. |
| Two labour writes race on one version | One conditional update succeeds; the other conflicts without adding its event. |
| Labour is saved again against the new version | Replaces the current snapshot and adds another event; this PUT has no replay key. |
| Auth, origin, CSRF, or membership check fails | The access gate rejects the request before a business write; status depends on the failure (`400`, `401`, `403`, `409`, or `503`). |
| An old event predates the version column | Its `work_version` remains `null`; newer events carry their resulting versions. |
| A stale browser response arrives after another selection | Selection and list counters keep it from replacing the currently displayed work or list. |
| A list refresh fails after creation or labour save | The browser keeps the accepted save and reports the failed refresh separately. |
| A user has read permission only | The browser hides creation controls and disables the labour form; the gate rejects direct write requests. |
| No manual labour is stored yet | The inquiry stays `KESKEN` and the price summary remains hidden. |

The browser blocks duplicate pending submits. If list refresh fails after a successful save, it reports the save as successful and the refresh as failed.

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `CreateWorkRequest` | `registration`, `customer_description`, `idempotency_key`; deprecated `actor` | Validated inquiry input; the request key is separate from the generated work ID. |
| `RecordLabourRequest` | `version`, `hours`, `work_description`, `autodata_reference`; deprecated `recorded_by` | Validated manual labour input and expected inquiry version. |
| `work_inquiries` | `id`, `status`, `version`, description, labour and price columns | Current workshop inquiry and latest manual labour snapshot. |
| `inquiry_workshop_assignments` | inquiry, workshop, organization IDs | Limits list, detail, and labour updates to assigned inquiries. |
| `work_events` | `work_id`, `event_type`, `actor`, `details_json`, `work_version` | Creation and manual labour history for an inquiry. |
| `scoped_inquiry_creation_requests` | workshop ID, key, fingerprint, response | Durable authenticated creation replay within a workshop. |
| `inquiry_creation_requests` | key, request, work ID, response | Historical unscoped replay data that remains readable when its work has a workshop assignment. |

`WorkRepository.initialize()` creates the base tables, adds nullable `work_version` to an older events table, and applies the business migrations at startup. It keeps existing event history.
Initialization does not assign old inquiries to a workshop; assignment belongs to the offline access workflow in [Workshop Access](./workshop-access.md).

`create_work()` inserts a `KESKEN` version-1 inquiry, its workshop assignment, creation event, and replay response in one transaction. The replay fingerprint includes normalized registration and description plus the authenticated actor ID. A replay returns the response captured at creation, so a later labour update does not change that replayed response.

`list_works()` returns assigned current rows ordered by creation time descending, without events. `get_work()` reads one assigned row and its newest-first decoded events in a single read transaction. The current row holds the latest manual labour and price fields; each successful labour save adds a separate historical event.

Current inquiry fields and events have separate update behavior:

- Creation leaves the labour and price columns empty.
- Recording labour preserves the original customer description.
- Manual labour saves leave the inquiry status as `KESKEN`.

`record_labour()` checks the assigned inquiry, updates it only when `id` and expected `version` match, increments the version, and appends `AUTODATA_TYOAIKA_KIRJATTU` in the same transaction. It uses the trusted actor ID in the current row and event. New authenticated events also include a display-name attribution snapshot; the browser labels older self-reported actors separately.

The route calculates labour cost from entered hours, then splits an inclusive total into net and VAT. It stores decimal price values as text. The queue displays the returned price; JavaScript formats it but does not recalculate it. This price does not grant the approval described in [Approved Labour](./approved-labour.md).

The default rate is 90.00 EUR per hour inclusive of 25.5% VAT. Labour cost rounds the gross amount half up to cents; the VAT split rounds net half up and sets VAT to gross minus net.

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `webapp.main` | `create_app()` | Configures the guarded FastAPI app and business storage. |
| `webapp.database.WorkRepository` | `initialize()` | Initializes the inquiry schema and business migrations. |
| `webapp.database.WorkRepository` | `create_work()` | Creates or replays an inquiry using a trusted actor and workshop context. |
| `webapp.database.WorkRepository` | `list_works()` | Lists inquiries assigned to the current workshop. |
| `webapp.database.WorkRepository` | `get_work()` | Reads an assigned inquiry with its event history. |
| `webapp.database.WorkRepository` | `record_labour()` | Writes a version-checked manual labour snapshot and event. |
| `services.labour_pricing` | `calculate_labour_cost()` | Calculates the cent-rounded inclusive labour cost. |
| `services.labour_pricing` | `split_inclusive_vat()` | Splits that amount into net and VAT. |

Repository calls use a `TrustedWorkshopContext`; HTTP handlers obtain it from the session. `WorkRepository.operation()` holds the authorization reservation across each business transaction and checks workshop permission inside it. Server-side workshop setup and membership rules are covered by [Workshop Access](./workshop-access.md).

The repository writes the resulting inquiry version into each new event. Reading detail returns events newest first, while the list reads current inquiry rows without history.

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `#business-shell` and `#session-user` | Reveal the queue after session confirmation and show the employee or read-only role. |
| `#work-list` | Shows workshop inquiries and opens a selected item. |
| `#new-work-dialog` | Collects inquiry fields and retains one UUID across retries. |
| `#work-detail` and `#labour-form` | Show an inquiry and submit hours against its current version. |
| `#price-summary` and `#event-list` | Show saved price components and authenticated or legacy event attribution. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/works` | GET | Lists inquiries in the active workshop, without events. |
| `/api/works` | POST | Creates or replays an inquiry; `201` on success, `409` for conflicting key reuse. |
| `/api/works/{work_id}` | GET | Reads an assigned inquiry and its event history; `404` if unavailable. |
| `/api/works/{work_id}/labour` | PUT | Saves manual labour against the expected version; `404`, `409`, or `422` for route and data failures. |

Create request body, with the session cookie and write CSRF header supplied by the browser:

```json
{
  "registration": "ABC-123",
  "customer_description": "Customer's original work request",
  "idempotency_key": "21bbbe25-6b60-4732-97ea-430075cbd885"
}
```

The response includes an assigned work ID, `status: "KESKEN"`, `version: 1`, empty labour columns, and a creation event. The UUID in the request is only a retry key.

Manual labour request after reading that version:

```json
{
  "version": 1,
  "hours": "1.60",
  "work_description": "Checked Autodata operation",
  "autodata_reference": "Reference shown in Autodata"
}
```

On success, the response contains the incremented version, latest labour and price strings, and both events. A repeated PUT with version `1` then returns `409`. The [Employee Authentication](./employee-authentication.md) page owns the session and CSRF exchange; the [Inquiry Drafts](./inquiry-drafts.md) page owns its separate `/api/drafts` routes.

## Integration Points

- **Depends on**: FastAPI, Pydantic, SQLite, and authenticated [Workshop Access](./workshop-access.md) for the active membership and permissions.
- **Used by**: The protected browser at `/`, through these four JSON routes. The local queue and manually saved [Inquiry Drafts](./inquiry-drafts.md) share its inquiry form.
- **External systems**: None in this flow. An employee checks Autodata and enters the time; the route makes no Autodata request.

## Extension Guide

Carry the server-resolved context and expected version through any new inquiry editor, and keep each update and event in one transaction. Add a new protected route to the reviewed access inventory in `webapp/http_access.py`. A workflow that needs reviewed hours should call the internal [Approved Labour](./approved-labour.md) gate instead of treating this manual snapshot as approval.
