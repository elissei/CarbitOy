---
title: "Feature: Inquiry Drafts"
type: feature
last_verified: 2026-10-19
sources:
  - webapp/draft_*.py
  - webapp/http_drafts.py
  - webapp/http_access.py
  - webapp/main.py
  - webapp/database.py
  - webapp/static/inquiry_drafts.js
  - webapp/static/app.js
  - webapp/static/index.html
---

# Feature: Inquiry Drafts

## Overview

Employees with draft permission can save incomplete work inquiries for later editing. The service also stores labour-entry editor drafts, but the current browser panel edits only inquiry drafts. A draft is editor state: saving it neither creates an inquiry nor records labour.

## Key Concepts

Before working with this feature, understand these concepts:

- [Workshop Identity](../concepts/workshop-identity.md) defines the trusted employee and workshop context.
- [Employee Authentication](./employee-authentication.md) explains the session and CSRF gate used by draft requests.

## Functional Specification

### User Flow

1. A permitted employee opens the inquiry form and enters a registration and customer description. The panel says that saving is manual.
2. **Tallenna luonnos** creates a `WORK_INQUIRY` draft, or saves an opened draft against its current revision. The browser sends a new UUID request ID and retains the exact request for an uncertain retry.
3. On acknowledgement, the browser reads the draft again. A receipt describes an accepted historical operation; the fresh read supplies the editable revision and detects content changed elsewhere.
4. **Näytä / päivitä luonnokset** fetches up to 100 active workshop drafts and displays only inquiry drafts. Selecting one loads its saved fields into the form.
5. The employee can retry an uncertain write with **Uusi sama tallennus**, or load the server version after a conflict. The HTTP API also supports discarding a draft; this browser panel has no discard button.

Submitting **Tallenna tiedustelu** uses the separate [Local Work Queue](./local-work-queue.md) creation request. That action clears the browser's draft editor state; it does not convert or delete an existing server draft. There is no autosave or completed draft-to-inquiry submission bridge.

### Validation Rules

- Draft HTTP operations require the current authenticated workshop context and draft read or write permission. Writes also pass the origin and session CSRF gate.
- `CARBIT_DRAFT_API_ENABLED` defaults to `0`. Enabling it with `1` requires `CARBIT_DRAFT_HMAC_KEYS` and `CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID`; an optional legacy key ID supports old receipts. The routes remain registered when disabled and return `503` from the handler after the access gate.
- Create accepts `WORK_INQUIRY` or `LABOUR_ENTRY`, a canonical UUID `request_id`, and a JSON object of editor fields. A labour draft additionally requires `work_id` and the current positive `original_work_version` of that workshop's inquiry.
- Inquiry payload keys are `registration` (20 characters) and `customer_description` (4000). Labour keys are `hours` (64), `source` (32), `work_description` (2000), and `autodata_reference` (500). Values may be strings or null; unknown keys, NULs, nested values, and payloads above 32 KiB are rejected. Incomplete values are allowed because final business validation happens elsewhere.
- Save and discard require `expected_revision >= 1` and a new canonical UUID request ID. The JSON body is at most 64 KiB, rejects duplicate keys and undeclared top-level fields, and uses JSON content type.
- List accepts only an optional `limit` from 1 to 100; its default is 50. Other draft-route query parameters are rejected.
- Create and save acknowledge a receipt without echoing customer text; read and list return the active payload.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Same requester reuses a request ID with the same operation and normalized content | The stored signed receipt is returned; it does not repeat the write. |
| A request ID is reused with different content, or against another draft | Returns `409` for a request conflict. |
| Save uses an older revision | Returns `409`; the current content is not overwritten. |
| Save has unchanged editor content | Returns an accepted receipt without advancing revision or renewing expiry. |
| Draft is expired, discarded, unknown, or outside the workshop | Read or mutation returns the same `404` unavailable response. |
| One employee saves another employee's draft in the same workshop | The operation can succeed with the requester's own active membership and permission; the receipt identifies that requester. |
| Browser loses a write response | It offers the original request again, preserving later local edits; after one day it stops retrying an old uncertain request and asks the employee to inspect the list. |
| Browser receives a receipt but fresh content differs | It blocks another save and keeps the employee's form text until the server version is opened. |
| Saved content reaches its deadline | Read and list exclude it even before physical cleanup removes its row. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `DraftSnapshot` | `draft_id`, `draft_type`, `revision`, `payload`, timestamps | Active editor state returned by read and list. |
| `DraftReceipt` | `draft_id`, `revision`, `state`, expiry, termination | Content-free acknowledgement of create, save, or discard. |
| `draft_registry` | workshop and auth-store IDs, creator, type, optional work reference | Durable draft identity and workshop scope. |
| `draft_contents` | `payload_json`, `revision`, `last_content_saved_at`, `expires_at` | Active editor content and its seven-day deadline. |
| `draft_request_receipts` | requester, request ID, HMAC fingerprint, accepted state | Durable per-requester replay record without payload text. |
| `draft_terminations` and `draft_retention_policy` | reason, time, metadata duration | Terminal status and bounded later metadata cleanup. |

The business SQLite initialization applies draft migrations even when the HTTP feature is disabled. The active-draft view includes unexpired, undiscarded content for an active workshop and organization; the repository adds the resolved workshop and auth-store scope. Request fingerprints bind the operation and input to organization, workshop, auth store, and requester. Server HMAC keys stay outside SQLite, and old keys must remain available while retained receipts need them.

Content expires seven days after its last actual change. Discard removes content immediately after recording a terminal receipt. `DraftRepository.cleanup_expired()` handles a bounded owner-scoped batch; trusted offline `DraftMaintenance` can purge expired content and later terminal metadata in batches of at most 100. Metadata retention defaults to 90 days after termination and can be configured from 1 to 90 days. These cleanup methods have no HTTP route or scheduled invocation in `create_app()`.

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `DraftApiSettings` | `from_environment()` | Requires explicit API opt-in and configured key material. |
| `DraftService` | `create_work_inquiry()`, `create_labour_entry()` | Creates bounded editor drafts. |
| `DraftService` | `list()`, `read()`, `save()`, `discard()` | Exposes workshop-scoped CRUD through the repository. |
| `DraftService` | `cleanup_expired()` | Exposes bounded owner cleanup internally. |
| `DraftMaintenance` | `cleanup_expired()`, `cleanup_metadata()` | Performs trusted offline retention cleanup. |
| `http_drafts` | `register_draft_routes()` | Registers the five guarded draft routes. |

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `#inquiry-drafts` and `#draft-list` | Manual refresh and selection of active inquiry drafts. |
| `#draft-save`, `#draft-retry`, `#draft-reload` | Explicit save, identical retry, and conflict recovery. |
| `CarbitInquiryDrafts()` | Keeps editor content, request envelope, and revision handling in browser memory. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/drafts` | POST | Creates a typed draft; returns a receipt with `201`. |
| `/api/drafts` | GET | Lists active workshop drafts as snapshots. |
| `/api/drafts/{draft_id}` | GET | Reads an active draft snapshot. |
| `/api/drafts/{draft_id}` | PUT | Saves content against the expected revision. |
| `/api/drafts/{draft_id}/discard` | POST | Terminates an active draft against the expected revision. |

Invalid draft input returns `422` (or `400`, `413`, `415` for framing, size, or media type); inaccessible drafts return `404`, revision or request conflicts `409`, and unavailable service or storage `503`. Authentication and permission failures are enforced by the shared access gate described in [Employee Authentication](./employee-authentication.md).

## Integration Points

- **Depends on**: The separate auth store and the trusted workshop authorization boundary in [Workshop Identity](../concepts/workshop-identity.md), plus business SQLite draft migrations.
- **Used by**: The manual inquiry-draft panel and internal callers that need labour-entry editor drafts.
- **External systems**: None; key material is supplied by server configuration.

## Extension Guide

Build a submission bridge as an explicit transaction that validates final inquiry or labour input and records conversion separately. The existing `draft_conversion_receipts` schema anticipates this, but the current service exposes no conversion method. Keep content changes revision checked, preserve exact retry envelopes, and add any new HTTP route to the reviewed access inventory.
