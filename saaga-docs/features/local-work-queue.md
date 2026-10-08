---
title: "Feature: Local Work Queue"
type: feature
last_verified: 2026-10-09
sources:
  - webapp/main.py
  - webapp/database.py
  - webapp/static/app.js
  - webapp/static/index.html
  - services/labour_pricing.py
  - domain/models/labour_time.py
  - domain/models/labour_cost.py
---

# Feature: Local Work Queue

## Overview

The local browser screen records a work inquiry before its labour time is known. A person can later enter an Autodata time they checked, view an inclusive labour calculation, and inspect the work's event history. The calculation is not an approved final quote.

Supported use is local at `127.0.0.1`, without login or access control. Autodata time is entered manually; there is no Autodata API integration. Vercel deployment and a persistent cloud database remain open design decisions, not implemented capabilities of this web version.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work and Part Data](../concepts/work-and-part-data.md) owns the shared `LabourTime` and `LabourCost` contracts.

## Functional Specification

### User Flow

1. Open `/` and choose **Uusi tiedustelu**. Enter a registration, the customer's original description, and the recorder's name. Labour time is not required at creation.
2. Submit the form. The browser sends a UUID created when the dialog opened and keeps it if submission fails, so retrying the same form can recover a response lost after the server committed it.
3. The new inquiry appears in the list with status `KESKEN`. Open it to view its original description, creation time, and event history.
4. Check the work time in Autodata yourself. Enter the hours as given, the Autodata work description, the recorder's name, and an optional reference. The application does not fetch Autodata data, infer a diagnosis, or decide whether a time covers one or both sides.
5. Save the labour entry. The work now shows the inclusive labour total, its net and VAT portions, and a new history event. Reopening the work reads the saved values.

### Validation Rules

- Creation requires `registration`, `customer_description`, `actor`, and a UUID `idempotency_key`; extra request fields are rejected.
- Registration is at most 20 characters, customer description at most 4000, and actor at most 200. Each must contain non-whitespace text after trimming. Registration is converted to uppercase before storage and before the idempotency comparison.
- Labour submission requires integer `version >= 1`, decimal `hours > 0`, nonblank `work_description` of at most 2000 characters, and nonblank `recorded_by` of at most 200. Extra fields are rejected.
- `autodata_reference` is optional and at most 500 characters. Omitted or blank input normalizes to `None` in `RecordLabourRequest`, then is saved and returned as an empty string. The other labour text fields are trimmed before construction of `LabourTime` with source `MANUAL_AUTODATA`.
- The computed labour total must be positive at cent precision. Invalid decimal input, a positive duration that rounds to zero cents, or a value too large for the calculation produces HTTP `422` without changing the work or its history. The route translates expected `ValidationError`, `InvalidOperation`, and decimal `Overflow` exceptions; unexpected programming errors are not hidden by a catch-all handler.
- The browser uses required fields and length limits from the HTML form and renders FastAPI field errors in Finnish. Server validation still applies to API callers.

The request models reject fields they do not declare. A malformed UUID, missing required field, invalid number, or overlong text receives FastAPI's HTTP `422` validation response before a repository write.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Same creation UUID and same normalized request fields | Returns the original creation response with HTTP `201`, even after a later update or server restart; it does not create another work or event. |
| Same creation UUID with different normalized fields or actor | Returns HTTP `409`; the browser keeps the failed form and its key until a new dialog is opened. |
| Deliberate new inquiry with identical details | A newly opened dialog generates another UUID, so a separate inquiry can be created. |
| Unknown work ID on detail or labour request | Returns HTTP `404`. |
| Submitted labour version differs from the current version | Returns HTTP `409`; the user must reopen the work to load its latest version, replacing unsaved form values. |
| Two labour updates race on one version | The conditional update accepts one; the other gets `409`, with no second labour event. |
| A create or labour request is pending | The corresponding form blocks duplicate submissions. Creation also disables and keeps its dialog open while the request is pending. |
| A response arrives after the user selected another work | Selection counters stop an older detail or save response from replacing the visible work or its draft. |
| A list response arrives after a newer list request | A list counter discards the older result. |
| A list refresh fails after a successful save | The browser reports that the save succeeded but the list refresh failed. |
| The inquiry has no labour entry yet | Price details remain hidden; its status is still `KESKEN`. |
| A successful labour entry is saved again using its new version | Another version and history event are created; labour PUT has no replay key. |
| An old event predates the `work_version` column | Its response has `work_version: null`; existing history is preserved. |
| A work is selected while a different work's save is pending | The new work can be viewed, but its save control stays disabled until the pending request finishes. |

The browser's history list shows event labels, times, and actors. The JSON detail response also carries each event's `details` and `work_version` for callers that need the recorded snapshot.

The visible labour form binds `data-work-id` and `data-version` to the selected work. Submission checks that binding and captures the selection counter before awaiting a response. Only a response for the still-selected work and counter can replace the form. Pending-submit guards also reject synthetic duplicate submissions, not just repeated button clicks.

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `CreateWorkRequest` | `registration`, `customer_description`, `actor`, `idempotency_key` | Validated creation input; its UUID is stored as a string. |
| `RecordLabourRequest` | `version`, `hours`, `work_description`, `recorded_by`, `autodata_reference` | Validated labour input and expected work version. |
| `work_inquiries` | `id`, `registration`, `customer_description`, `status`, `version`, timestamps | SQLite record for each inquiry; creation sets `KESKEN`, version `1`, and empty labour columns. |
| `work_inquiries` | `labour_hours`, `labour_source`, `labour_work_description`, `labour_recorded_by`, `autodata_reference` | Latest manually entered labour snapshot. |
| `work_inquiries` | `hourly_rate`, `total_price`, `net_price`, `vat_price`, `vat_rate`, `currency` | Latest pricing snapshot, stored as text to preserve decimal strings. |
| `work_events` | `work_id`, `occurred_at`, `event_type`, `actor`, `details_json`, `work_version` | Creation and labour history; details hold the registration and description or the full labour and price snapshot. |
| `inquiry_creation_requests` | `idempotency_key`, `request_json`, `work_id`, `response_json` | Durable replay record for the normalized creation request and its original response. |

The repository creates UUID work IDs and UTC timestamps. A detail response includes the inquiry columns and decoded events, newest first; a list response contains inquiry rows without events, ordered by creation time descending.

The list is a queue view, not an event feed. It reads each work's current row, including the latest labour and price columns when present. Opening one work obtains its event history separately. Creation has no labour snapshot, and a later labour save replaces the current snapshot without changing the original customer description.

History retains every successful labour entry as a separate event. Its `details` object holds the snapshot supplied to the repository at that save, including the inclusive total and VAT split. The event's `actor` is the labour recorder; the inquiry creation event uses the creation form's actor. The current work row holds only the most recent labour values.

`WorkRepository.initialize()` creates missing tables and indexes when the app starts. For an older `work_events` table, it adds a nullable `work_version` column; old events retain `null`, while new creation and labour events record the resulting version.

Initialization performs the schema additions in a transaction. It does not delete or recreate the existing database or its tables, and does not backfill historical version numbers.

Creation inserts the inquiry, `TIEDUSTELU_LUOTU` event, and replay record in one SQLite write transaction. Labour uses `WHERE id = ? AND version = ?`, increments the version, and writes `AUTODATA_TYOAIKA_KIRJATTU` with the resulting version in the same transaction. A failed operation rolls back its writes. Detail reads the inquiry and history in one read transaction.

The replay record compares the serialized registration, description, and actor after request validation. It stores the response assembled at creation, so a retry returns version `1` even if that inquiry now has newer labour data. Call `GET /api/works/{work_id}` to read current state after a replay.

The route constructs `LabourTime` from the supplied time and provenance, then calls `services.labour_pricing.calculate_labour_cost()`. At the default inclusive rate of `90.00` EUR per hour, it multiplies hours by the rate and rounds half up to cents. `split_inclusive_vat()` divides that total by `1.255` and rounds the net half up to cents; VAT is the total minus net. VAT is not added to the total. For `1.60` hours, the stored total is `144.00` EUR, net `114.74`, and VAT `29.26`.

This web pricing path is separate from the library's [Proposal Decisions](./proposal-decisions.md) calculation. The shared model contracts are in [Work and Part Data](../concepts/work-and-part-data.md).

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `webapp.main` | `create_app()` | Builds the FastAPI app, chooses its SQLite path, registers routes, and initializes storage at startup. |
| `webapp.database.WorkRepository` | `initialize()` | Creates the schema and migrates an older event table. |
| `webapp.database.WorkRepository` | `create_work()` | Atomically creates an inquiry and its replay and history records, or returns a saved response for an identical UUID request. |
| `webapp.database.WorkRepository` | `list_works()` | Reads inquiry rows for the queue. |
| `webapp.database.WorkRepository` | `get_work()` | Reads one inquiry and its history together. |
| `webapp.database.WorkRepository` | `record_labour()` | Conditionally replaces the labour and pricing snapshot and adds an event. |
| `services.labour_pricing` | `calculate_labour_cost()` | Produces a cent-rounded EUR `LabourCost` from supplied hours and rate. |
| `services.labour_pricing` | `split_inclusive_vat()` | Splits an inclusive total into net and VAT amounts. |

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `#work-list` | Shows inquiries and opens a selected work. |
| `#new-work-dialog` | Collects creation details and manages a UUID across retries. |
| `#work-detail` and `#labour-form` | Show the selected inquiry and submit labour against its current version. |
| `#price-summary` | Shows the saved total, net price, and 25.5% VAT in Finnish currency format. |
| `#event-list` | Shows creation and labour events with time and actor. |
| `#toast`, `#new-work-error`, `#labour-error` | Report saves, request failures, and field errors. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/works` | GET | Lists stored inquiries, without event details. |
| `/api/works` | POST | Creates an inquiry and returns its initial detail with HTTP `201`; can replay by UUID or reject a conflicting reuse with `409`. |
| `/api/works/{work_id}` | GET | Reads an inquiry and its event history; returns `404` if absent. |
| `/api/works/{work_id}/labour` | PUT | Validates and saves labour against an expected version; returns updated detail, `404`, `409`, or `422`. |

A creation request has this shape:

```json
{
  "registration": "ABC-123",
  "customer_description": "Customer's original work request",
  "actor": "Recorder",
  "idempotency_key": "21bbbe25-6b60-4732-97ea-430075cbd885"
}
```

The server trims the text and stores the registration as `ABC-123`. A successful response includes `id`, `status: "KESKEN"`, `version: 1`, empty labour fields, and a creation event. The submitted UUID is a retry key, not the work ID.

After reading that version, a labour request has this shape:

```json
{
  "version": 1,
  "hours": "1.60",
  "work_description": "Checked Autodata operation",
  "recorded_by": "Recorder",
  "autodata_reference": "Reference shown in Autodata"
}
```

The response has version `2`, the latest price fields, and both events. Decimal fields in the stored work are strings, including `labour_hours`, `hourly_rate`, `total_price`, `net_price`, `vat_price`, and `vat_rate`. Sending another labour request with version `1` now produces `409`; read the work again before trying to save a revised entry.

## Integration Points

- **Depends on**: FastAPI and Pydantic for HTTP validation, SQLite for local persistence, and the shared labour models in [Work and Part Data](../concepts/work-and-part-data.md). The module placement is described in [Architecture](../ARCHITECTURE.md).
- **Used by**: The browser served at `/`, which calls these four JSON operations. Its JavaScript uses the same server's `/static` assets.
- **External systems**: None in this flow. A person supplies the Autodata time; the app does not call Autodata or the separate library intake and quote workflow.

The actor and recorder names come from the forms. These routes do not authenticate a user or enforce permissions, so a caller supplied name is the provenance stored with each event.

The module exposes `app = create_app()` for an ASGI server. The browser is served by that same app; it has no separate backend configuration.

JavaScript displays the server's decimal price strings with `Intl.NumberFormat("fi-FI")`; it does not recalculate labour prices or VAT.

## Extension Guide

Use `create_app(database_path=...)` to point an instance at another SQLite file; otherwise it reads `CARBIT_DATABASE` or defaults to `var/carbit.db` under the repository root. Keep the work update and event insert in one transaction, and carry the expected `version` through any new editor so concurrent changes still surface as conflicts. A new persisted labour or price field must be added to the SQLite snapshot, event details, route snapshot, and browser rendering together. Changes to the shared labour contracts belong in [Work and Part Data](../concepts/work-and-part-data.md); changes to the separate proposal path belong in [Proposal Decisions](./proposal-decisions.md).
