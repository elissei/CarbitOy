---
title: Workshop Identity
type: concept
sources:
  - webapp/auth_models.py
  - webapp/auth_repository.py
  - webapp/auth_service.py
  - webapp/business_identity.py
  - webapp/business_authorization.py
  - webapp/workshop_context.py
  - webapp/workshop_identity.py
  - webapp/workshop_repository.py
  - webapp/workshop_migrations.py
  - webapp/database.py
last_verified: 2026-10-19
---

# Workshop Identity

## Business Definition

An employee identity identifies the person using Carbit; a workshop membership determines which repair shop's inquiries that person may access. A valid login alone does not grant access to business data: each operation needs a current, server-resolved workshop context and the required role permission.

## Configuration

| Source | Description |
|--------|-------------|
| `webapp/workshop_context.py` | Maps human roles to workshop permissions. |
| `webapp/auth_repository.py` | Gives each auth database a persistent store ID used to namespace memberships. |

**How to access:**

- `permissions_for_role()` - returns the immutable permission set for a role; unknown roles have no permissions.
- `AuthRepository.store_id()` - returns the persistent identity of the auth store.
- `WorkshopDirectory.resolve()` - resolves the user's one active, usable workshop membership.

The password, MFA, session, and HTTP configuration belongs to [Employee Authentication](../features/employee-authentication.md).

## Data Storage

| Object/Model/Type | Field/Property | Purpose |
|--------|-------|---------|
| `User` | `user_id`, `username`, `display_name` | Stable identity, login name, and display name. |
| `User` | `role`, `kind`, `enabled`, `mfa_enabled`, `auth_version` | Eligibility and the version against which sessions are checked. |
| `User` | `can_approve_labour` | A property reporting role and MFA eligibility, without proving a live session. |
| `SessionRecord` | `session_id`, `user_id`, `auth_version`, `factor` | Server session and the verified second factor. |
| `SessionRecord` | `created_at`, `mfa_verified_at`, `expires_at` | Time-bounded proof of MFA. |
| `ValidatedSession` | `user`, `session_id`, `absolute_expiry`, `idle_expiry` | A server-checked session view without the bearer token. |
| `auth_store_identity` | `store_id` | Immutable namespace for identities in one auth database. |
| `auth_users` | `password_hash`, `totp_ciphertext`, `last_totp_step` | Credential material and TOTP replay state in the auth store. |
| `auth_challenges` | `purpose`, `auth_version`, `expires_at`, `consumed_at` | Versioned password-to-MFA or enrollment transition. |
| `auth_sessions` | `token_hash`, `last_seen_at`, `revoked_at` | Session lookup, idle limit, and explicit revocation. |
| `auth_events` | `actor_user_id`, `subject_user_id`, `details_json` | Authentication audit history. |
| `auth_rate_limits` | `scope_hash`, `window_start`, `attempts` | Persistent authentication request budgets. |
| `auth_recovery_codes` | `user_id`, `auth_version`, `consumed_at` | One-use recovery factor tied to an account version. |
| `auth_notification_outbox` | `notification_id`, `event_id`, `delivered_at` | Pending notification about an MFA reset. |
| `auth_reset_requests` | `actor_user_id`, `request_id` | Idempotency record for server-side reset requests. |
| `repair_organizations` | `organization_id`, `enabled` | Business-side organization scope and availability. |
| `workshops` | `workshop_id`, `organization_id`, `status` | Workshop belonging to an organization. |
| `workshop_principals` | `auth_store_id`, `user_id` | Connects an auth-store user to the business store. |
| `workshop_memberships` | `workshop_id`, `enabled`, `version` | Explicit grant and change counter. |
| `active_user_workshops` | `workshop_id` | Explicit selection when a user can access multiple workshops. |
| `inquiry_workshop_assignments` | `inquiry_id`, `workshop_id`, `organization_id` | Immutable scope of an inquiry. |
| `workshop_audit_events` | `operation`, `operator_reference`, `details_json` | History of trusted offline workshop administration. |
| `WorkshopPermission` | `READ_LABOUR_PLAN`, `SAVE_LABOUR_PLAN`, `APPROVE_LABOUR_PLAN` | Permission vocabulary for labour operations. |
| `WorkshopPermission` | `READ_WORKSHOP_DRAFT`, `WRITE_WORKSHOP_DRAFT` | Permission vocabulary for drafts. |
| `TrustedWorkshopContext` | `employee_id`, `permissions`, `auth_store_id`, `organization_id`, `workshop_id`, `membership_version` | One operation's server-derived authorization snapshot. |
| `TrustedWorkshopContext` | `actor`, `auth_binding` | Historical attribution and a server-only session binding. |
| `BusinessActor` | `user_id`, `display_name` | Captured employee attribution for a business event. |
| `WorkshopAuthBinding` | `service`, `session_id` | Server-only link to the auth session for commit-time validation. |

The authentication tables live in a separate SQLite file from inquiries and workshop tables. `AuthRepository.initialize()` creates auth schema and the store identity; business initialization adds workshop tables but creates no organization, membership, or inquiry assignment. The store ID keeps same-looking user IDs from different auth databases separate.

`UserRole` has `OWNER`, `SERVICE_MANAGER`, `IT_ADMIN`, and `OPENCLAW`. `IdentityKind` distinguishes `HUMAN` from `SERVICE`; the `OPENCLAW` role is service-only and cannot use human MFA. `User.can_approve_labour` describes eligibility only: it is neither session verification nor workshop authorization.

The persistent session holds a hash of its bearer token, not the token itself. The public `SessionGrant` carries the token only at issue time, while `ValidatedSession` carries the verified user and deadlines. A stored `auth_version` mismatch invalidates the session after account security state changes.

Authentication timestamps in `User`, `LoginChallenge`, `SessionRecord`, and related models must have timezone information; the models normalize them to UTC. A session record also requires MFA verification to fall between creation and expiry.

The `User` model is immutable after validation. Updating authentication state means changing the repository record and revalidating a fresh model, rather than editing an in-memory user and assuming that has changed the store.

`BusinessActor` is a historical attribution snapshot. Its serialized form includes `version`, `provenance`, `user_id`, and `display_name`; constructing one independently does not authenticate a request. The HTTP adapter supplies one only after checking the current session and matching it to the trusted context.

`TrustedWorkshopContext` is a per-operation snapshot. Its employee ID, permissions, organization, workshop, and membership version must agree with the current stores at the transaction boundary. The `auth_binding` is an internal server reference rather than a reusable bearer credential.

## Key Services/Functions (PUBLIC/EXPORTED only)

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `AuthService` | `inspect_session()` | Validates a real session and returns the current user and deadlines without refreshing idle time by default. |
| `AuthService` | `require_session()` | Validates and refreshes an active session's idle time. |
| `AuthRepository` | `store_id()` | Returns the immutable namespace for auth users. |
| `AuthRepository` | `initialize()` | Applies the independent auth schema and creates its store identity. |
| `WorkshopDirectory` | `resolve()` | Requires exactly one selected or sole usable membership. |
| `WorkshopDirectory` | `grant_membership()` | Gives an existing human account explicit access to a workshop. |
| `WorkshopDirectory` | `revoke_membership()` | Disables a grant and advances its version. |
| `WorkshopDirectory` | `select_active_workshop()` | Persists a user's explicit selection among usable workshops. |
| `workshop_identity` | `resolve_workshop_context()` | Rechecks a session, role, and membership to build a context for one required permission. |
| `workshop_context` | `permissions_for_role()` | Supplies role permissions for server checks. |
| `workshop_repository` | `active_membership()` | Finds the one selected or sole usable membership in a business connection. |
| `workshop_repository` | `authorize_workshop()` | Rechecks permission and membership within a business transaction. |
| `business_authorization` | `reserve_business_authorization()` | Holds auth validity across the business transaction. |
| `WorkRepository` | `operation()` | Wraps business work in the authorization and scope check. |
| `BusinessActor` | `attribution()` | Serializes the session-derived event attribution snapshot. |

The role mapping gives owners and service managers read, save, approve, and draft permissions. IT admins receive read permission; the service role receives none. Permission alone cannot create a membership. See [Workshop Access](../features/workshop-access.md) for the grant and scope workflow.

The `User` model requires service identities to use `OPENCLAW` and forbids MFA on those identities. Human identity and workshop membership remain distinct records: the auth store does not carry a workshop grant, and a business membership does not carry a password or session.

For a selected membership to resolve, its grant must be enabled, its workshop must be `ACTIVE`, and its organization must be enabled. A recorded selection does not override these current checks. Where no selection exists, the directory accepts a sole usable membership and rejects ambiguity.

`WorkshopDirectory` reads the persistent auth store ID and uses it with the user's UUID. Membership administration accepts existing enabled human accounts; it does not create accounts or infer a grant from a role.

The directory records `operator_reference` in business-side audit events for grants, revocations, and selections. That reference identifies the trusted offline operation; it is not a browser-supplied employee actor.

`WorkshopAccessDenied` is the common denial type for absent, ambiguous, or stale workshop access. Callers should not infer whether a workshop, user, or inquiry exists from that denial.

The business repository joins inquiry rows with their workshop assignment for list and detail reads. The assignment belongs to the inquiry's lasting business record rather than to whichever employee happens to open it.

A trusted context is deliberately narrow: it records the permission set available at resolution time, while each operation requests one specific permission and checks current authorization again.

The context's `membership_version` is compared with the current row. Revoking a grant increments that version, so an earlier snapshot cannot quietly authorize a later business operation even when its workshop ID still matches.

## Internal Implementation

- `webapp/business_authorization.py` holds an auth-store writer reservation while the separate business transaction checks the session and commits. This prevents a concurrent auth mutation from overtaking an accepted business operation; the fixed lock order is auth store, then business store.
- `webapp/workshop_repository.py` checks membership version and the selected workshop inside the business transaction. A context captured before revocation or reassignment cannot authorize a later operation.
- `webapp/database.py` checks the resolved context inside `WorkRepository.operation()`. Business writes commit while the auth reservation is still held; the reservation itself writes no auth data.
- `webapp/auth_repository.py` assigns an immutable store ID during auth migration. Membership keys pair it with a user ID, so a separately initialized auth database (with a different store ID) does not inherit another store's grants. A copied auth database keeps the original store ID; the current mechanism does not detect a clone of the same database, so the copy matches the existing memberships.

## Reference Implementations

- `webapp/workshop_identity.py` - token-only adapter that constructs the trusted context after session, role, and membership checks.
- `webapp/workshop_repository.py` - explicit membership resolution and transaction-time verification.
- `webapp/business_authorization.py` - binding between a validated session and a business commit.
- `WorkRepository.operation()` - authorized transaction boundary used by inquiry operations.
- `BusinessActor.attribution()` - event metadata built from a server-captured employee identity.
- `AuthRepository.store_id()` - persistent identity namespace used by the workshop directory.
- `permissions_for_role()` - central mapping from current user role to workshop permission set.
- `webapp/auth_models.py` - validation that separates human and service identities and requires aware authentication timestamps.
- `webapp/workshop_migrations.py` - additive business schema with versioned memberships and immutable assignment history.

## Related Concepts

- [Employee Authentication](../features/employee-authentication.md) explains how a human receives and loses a session.
- [Workshop Access](../features/workshop-access.md) explains establishment, membership, selection, and inquiry scope.
