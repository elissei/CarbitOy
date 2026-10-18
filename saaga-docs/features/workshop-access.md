---
title: "Feature: Workshop Access"
type: feature
sources:
  - webapp/auth_repository.py
  - webapp/auth_service.py
  - webapp/business_authorization.py
  - webapp/business_identity.py
  - webapp/workshop_admin.py
  - webapp/workshop_context.py
  - webapp/workshop_identity.py
  - webapp/workshop_migrations.py
  - webapp/workshop_repository.py
  - webapp/http_access.py
  - webapp/main.py
  - webapp/database.py
  - webapp/static/app.js
  - webapp/static/index.html
last_verified: 2026-10-19
---

# Feature: Workshop Access

## Overview

Workshop access connects an authenticated employee to one active repair workshop and limits inquiry operations to that workshop. An offline operator establishes workshops and grants membership; the browser cannot choose or grant its own scope.

## Key Concepts

Before working with this feature, understand these concepts:

- [Workshop Identity](../concepts/workshop-identity.md)
- [Work and Part Data](../concepts/work-and-part-data.md)

## Functional Specification

### Mechanism

1. A trusted offline operator establishes one organization and one active workshop in an already initialized, empty business database. This creates neither a user nor a membership.
2. A separate offline operation grants an existing enabled human account membership in an existing workshop. If the employee has more than one usable membership, the operator records an active-workshop selection.
3. For a protected request, the access gate validates the human session and role, then confirms a usable workshop membership. The handler resolves a fresh `TrustedWorkshopContext` from the server-held session token and required permission.
4. The business repository obtains an auth-store reservation, checks the current session and role, then checks selected membership and version in its own transaction. It revalidates before business commit.
5. Inquiry creation assigns the new inquiry to the context's workshop. Listing and detail reads join that immutable assignment, so another workshop's inquiry is outside the caller's view.

### Validation Rules

- Membership is explicit: a role, login, or database migration never grants it automatically. Only enabled memberships in active workshops under enabled organizations are usable.
- An explicit selection must still be usable. With no selection, exactly one usable membership is required; zero or multiple memberships fail closed.
- The access gate checks the route's role before the handler checks its workshop permission; [Workshop Identity](../concepts/workshop-identity.md) defines role permissions. IT admins can read scoped inquiries and labour plans but cannot access drafts.
- A request-supplied workshop ID, actor, or permission set cannot establish authority. The server resolves them from the session and its own stores.
- The auth reservation is held through business commit. The business connection checks membership version and permission within its transaction; stale context is denied.
- New inquiries receive a workshop assignment in the same transaction as creation. Legacy inquiries need an explicit approved assignment before scoped reads can return them.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Identical establishment retry | Returns `ALREADY_ESTABLISHED` after verifying the existing establishment event and database integrity. |
| User belongs to two active workshops without selection | Resolution fails instead of choosing one implicitly; the HTTP request returns 403 `Permission denied`. |
| Membership revoked or changed after context creation | The version or selected-membership check denies the later transaction; the HTTP request returns 403 `Permission denied`. |
| Inquiry belongs to another workshop or has no assignment | Detail lookup returns 404 `Työtä ei löytynyt` within the caller's scope. |

## Technical Implementation

### Data Model

Workshop access uses the workshop, membership, inquiry-assignment, trusted-context, and actor contracts defined in [Workshop Identity](../concepts/workshop-identity.md).

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `workshop_admin` | `establish_initial_workshop()` | Establishes the first organization/workshop offline, with audited idempotent retry. |
| `WorkshopDirectory` | `grant_membership()` | Grants an existing enabled human explicit workshop access. |
| `WorkshopDirectory` | `revoke_membership()` | Disables and versions a membership. |
| `WorkshopDirectory` | `select_active_workshop()` | Records a usable selected workshop for a human. |
| `workshop_identity` | `resolve_workshop_context()` | Revalidates session, role, and membership for one permission. |
| `WorkRepository` | `operation()` | Authorizes and checks scope through business commit. |
| `WorkRepository` | `list_works()` | Returns inquiries assigned to the current workshop. |

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `webapp/static/index.html` | Protected inquiry workspace. |
| `webapp/static/app.js` | Displays session user and loads only server-scoped inquiry results. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/works` | GET | Lists inquiries in the resolved workshop. |
| `/api/works` | POST | Creates an inquiry and assigns it to that workshop. |
| `/api/works/{work_id}` | GET | Reads one scoped inquiry or returns 404. |
| `/api/works/{work_id}/labour` | PUT | Records the legacy manual labour snapshot within the scoped inquiry. |

## Integration Points

- **Depends on**: [Employee Authentication](./employee-authentication.md), separate auth and business SQLite stores, an established workshop, and explicit membership.
- **Used by**: [Local Work Queue](./local-work-queue.md), inquiry drafts, and stored labour operations.
- **External systems**: A trusted offline operator supplies establishment, membership, and migration decisions; no external directory is queried.

## Extension Guide

New business operations should request a specific `WorkshopPermission`, resolve a fresh context from the session, and run through the repository authorization boundary. Additional workshop administration needs an explicit trusted operation and audit record; HTTP requests must not accept a caller-selected workshop as authority.
