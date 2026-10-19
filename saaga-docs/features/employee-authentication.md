---
title: "Feature: Employee Authentication"
type: feature
sources:
  - webapp/auth_models.py
  - webapp/auth_crypto.py
  - webapp/auth_csrf.py
  - webapp/auth_repository.py
  - webapp/auth_service.py
  - webapp/admin_offline_recovery.py
  - webapp/user_mfa_recovery.py
  - webapp/auth_settings.py
  - webapp/http_auth.py
  - webapp/http_access.py
  - webapp/main.py
  - webapp/browser_auth/login.html
  - webapp/browser_auth/login.js
  - webapp/static/app.js
  - webapp/static/index.html
last_verified: 2026-10-19
---

# Feature: Employee Authentication

## Overview

An enrolled employee signs in with a password and a second factor, then uses a secure session cookie to reach Carbit's protected pages and APIs. The browser can log out; account provisioning, MFA enrollment, reset, and offline recovery are trusted Python procedures without HTTP routes.

## Key Concepts

Before working with this feature, understand these concepts:

- [Workshop Identity](../concepts/workshop-identity.md)
- [Workshop Access](./workshop-access.md)

## Functional Specification

### User Flow

1. The browser opens `/login`, which loads the dedicated login script and styles. It requests `GET /api/auth/csrf` with `X-Carbit-Auth: bootstrap` to receive a cookie-bound preauthentication proof.
2. The employee enters a username and password. The browser sends `POST /api/auth/login` with JSON and the preauthentication CSRF proof. A valid password creates a short-lived login challenge and rotates the anonymous cookie into MFA state.
3. The employee enters a TOTP code or a single-use recovery code. `POST /api/auth/mfa` verifies the challenge and MFA proof, consumes the challenge, clears the temporary cookies, and issues the session cookie.
4. The browser calls `GET /api/auth/session` for current user and expiry information. The protected work page then loads only through the route gate and a usable workshop membership.
5. On logout, `POST /api/auth/logout` verifies a session CSRF proof, revokes the server session, clears authentication cookies, and returns 204.

Offline recovery is separate from that browser flow:

| Procedure | Authorization and completion |
|-----------|------------------------------|
| `OWNER` or `SERVICE_MANAGER` MFA recovery | A recently TOTP-verified IT admin records an in-person identity check, reason, request ID, and expected account version. `start_user_mfa_recovery()` invalidates the old factor, sessions, challenges, codes, and pending enrollment, then returns a 15-minute grant. The user presents that grant and their own password to `begin_user_recovery_enrollment()`, then proves the new TOTP to `complete_user_mfa_recovery()`. |
| IT-admin own-MFA recovery | With the application service stopped (an operator precondition the function does not verify), `start_admin_offline_recovery()` records either a separate external authorizer and in-person check or explicitly selected local self-recovery confirmations. It closes the IT-admin account, invalidates old authentication state, and returns a 15-minute grant. The admin uses the grant and own password with `begin_admin_recovery_enrollment()`, then the new TOTP with `complete_admin_offline_recovery()`. |

Both completion calls issue fresh recovery codes once and no login session; the person signs in normally afterward. The in-person check and local confirmations are recorded statements, not checks the program can perform. Notification outbox rows record intent only, not delivery.

### Validation Rules

- Login and MFA requests require exact JSON object shapes. Authentication POST bodies are capped at 4096 bytes; duplicate JSON keys, malformed JSON, and mismatched content length are rejected.
- Usernames are normalized to lowercase for account lookup; the public HTTP request bounds them to 1–64 characters.
- Passwords must be 15–256 characters and at most 1024 UTF-8 bytes. Password hashes use Argon2id; a stored hash may be upgraded after successful verification.
- TOTP is six digits, accepts one adjacent 30-second step for clock skew, and rejects a step already used by that account. Recovery codes are consumed once.
- A session requires an enabled human user with MFA still enabled and a matching `auth_version`; revocation, absolute expiry, and idle expiry invalidate it.
- The default policy caps a login challenge at 300 seconds, a session at eight hours, and idle time at 30 minutes. HTTP settings reject longer lifetimes.
- Authentication traffic must use a configured HTTPS origin and exact host. POST requests require a matching `Origin`; auth routes also reject cross-site fetch metadata and unsupported authorization headers.
- A state-bound `X-CSRF-Token` is required for authentication POSTs and protected business writes. The session token is held in a secure, HTTP-only, same-site, host-only cookie.
- CSRF proofs bind to the configured origin and current preauth, MFA, or validated session state. The preauth and MFA cookies are replaced or cleared as the flow advances.
- The gate accepts only registered route and method pairs; app startup fails when the inventory differs. `/login`, `/login.js`, and `/login.css` are the public browser files, still subject to transport checks.
- The app disables the generated OpenAPI, Swagger, and ReDoc routes, so they are not extra public authentication surfaces.
- Recovery requests require the current account version and a unique request ID; the business-user path requires an enabled target in an allowed role. Grants are single-use, account-version-bound, and expire after 15 minutes. Password and TOTP attempts spend persistent rate-limit budgets.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Password valid but MFA is not enrolled | Ordinary first enrollment can receive an internal enrollment challenge; after reset, `mfa_recovery_required` blocks password-only enrollment. HTTP login returns generic failure and no session in either case. |
| Business-user recovery grant expires or is superseded | The grant cannot complete; the account remains recovery-required until a newly authorized recovery completes. |
| IT-admin recovery expires or is cancelled | The grant cannot complete; the account remains disabled until a new offline recovery completes. |
| Recovery start or completion is replayed | Start returns current state without the grant token; completed-grant replay returns `ALREADY_COMPLETED` without codes or a session. A changed request under the same ID conflicts. |
| Recovery enrollment is repeated | It replaces the pending TOTP secret; the earlier secret cannot complete recovery. |
| Unknown or disabled account, wrong password, reused MFA step, invalid recovery code | No session; authentication failure does not identify which factor or account failed. |
| Session, preauth, and MFA cookies appear in an invalid combination | HTTP auth state conflict or invalid context; no fallback to a weaker state. |
| Session was revoked or expired before CSRF bootstrap | The CSRF endpoint can clear the stale session cookie and return a new preauthentication proof. Storage failures return 503 instead. |
| HTTP or credential rate limit is exhausted | 429 with a retry interval when available; limits are persisted in the auth store. |
| Valid session but missing workshop membership | Protected business request is denied; see [Workshop Access](./workshop-access.md). |
| Correct credentials for a service identity | Human login fails; service identities have no password/MFA login path. |
| MFA challenge is expired, consumed, or tied to an old account version | MFA completion fails without issuing a session. |
| Another request changes the account or stored password during verification | Password verification is retried at most once against a replacement hash without crossing an auth-version change. |
| A business write has no valid session proof or has extra auth cookies | The gate rejects it before the handler runs. |
| Logout is replayed after revocation | Session validation fails; no second successful logout is reported. |
| Browser requests an unregistered route | The router can return 404 or 405, but the request gains no business permission. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `LoginChallenge` | `token`, `requires_enrollment`, `expires_at` | Short-lived password-verification result. |
| `SessionGrant` | `token`, `user`, `session` | Server result after MFA verification. |
| `AuthPolicy` | `challenge_seconds`, `session_seconds`, `idle_seconds`, rate-limit fields | Configurable authentication lifetimes and budgets. |
| `AuthSettings` | `database_path`, `allowed_hosts`, `external_origins`, TOTP and CSRF keys | Required HTTP authentication configuration. |
| `UserMfaRecoveryRequest`, `UserRecoveryState`, `UserRecoveryCompletion` | `target_user_id`, `expected_auth_version`, `request_id`, `status`, `grant_token`, `recovery_codes` | Offline business-user request, grant state, and one-time completion result. |
| `AdminRecoveryStartRequest`, `AdminRecoveryState`, `AdminRecoveryCompletion` | `target_user_id`, `expected_auth_version`, `request_id`, `status`, `grant_token`, `recovery_codes` | Offline IT-admin request, grant state, and one-time completion result. |

The `ValidatedSession` interface, persistent auth tables, roles, and identity fields are owned by [Workshop Identity](../concepts/workshop-identity.md).

`AuthSettings.from_environment()` requires these deployment values; no secret is synthesized on startup:

| Environment variable | Purpose |
|----------------------|---------|
| `CARBIT_AUTH_DATABASE` | Absolute path to the separate authentication SQLite file. |
| `CARBIT_ALLOWED_HOSTS` | Comma-separated exact DNS names or IP addresses. |
| `CARBIT_EXTERNAL_ORIGINS` | Comma-separated exact HTTPS origins using allowed hosts. |
| `CARBIT_TOTP_KEYS` | One or more server-held Fernet keys for MFA secret encryption. |
| `CARBIT_CSRF_ACTIVE_KEY_ID` | Identifier of the active CSRF signing key. |
| `CARBIT_CSRF_KEYS` | JSON array of active and optional previous signing keys. |

The CSRF key list holds one to three distinct keys, and its active identifier must exist in the list. CSRF and TOTP key material must be independently provisioned. The configured auth database must be separate from the business database and outside the static-file tree.

The auth service records rate-limit counters and audit events in the auth database. Its request budgets use the server connection address as the client key; forwarded IP headers do not supply identity to this policy.

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `AuthSettings` | `from_environment()` | Loads required store path, exact hosts/origins, and server-held keys; missing values fail startup. |
| `AuthRepository` | `initialize()` | Applies additive auth schema and store-identity migrations. |
| `AuthService` | `begin_login()` | Checks the password and records a login or enrollment challenge. |
| `AuthService` | `complete_login()` | Validates a TOTP or recovery code and grants a session. |
| `AuthService` | `inspect_session()` | Revalidates a session, optionally refreshing idle time. |
| `AuthService` | `reserve_http_auth()` | Applies per-client and global HTTP authentication budgets before handling a request. |
| `AuthService` | `reserve_logout_global()` | Applies the logout global budget after a valid session and CSRF proof. |
| `AuthService` | `audit_http_auth()` | Records HTTP authentication results, sampling selected repeated denials. |
| `AuthService` | `refresh_session_for_roles()` | Rechecks role and session before post-write idle refresh. |
| `AuthService` | `revoke_session()` | Revokes a validated session and records the event. |
| `AuthService` | `provision_identity()` | Trusted offline identity creation; never a registration route. |
| `AuthService` | `begin_mfa_enrollment()` | Starts server-side TOTP enrollment for an enrollment challenge. |
| `AuthService` | `confirm_mfa_enrollment()` | Confirms TOTP enrollment and returns one-time recovery codes. |
| `AuthService` | `reset_user_mfa()` | Audited, idempotent reset of an owner or service manager after recent IT-admin TOTP verification; leaves enrollment grant-required and excludes IT-admin targets. |
| `user_mfa_recovery` | `start_user_mfa_recovery()` | Authorizes and starts a business user's grant-bound recovery. |
| `user_mfa_recovery` | `begin_user_recovery_enrollment()` | Checks grant and user's password; returns a pending TOTP secret. |
| `user_mfa_recovery` | `complete_user_mfa_recovery()` | Verifies new TOTP, consumes the grant, and returns one-time codes. |
| `admin_offline_recovery` | `start_admin_offline_recovery()` | Starts stopped-host IT-admin own recovery and disables the target account. |
| `admin_offline_recovery` | `begin_admin_recovery_enrollment()` | Checks grant and admin password; returns a pending TOTP secret. |
| `admin_offline_recovery` | `complete_admin_offline_recovery()` | Verifies new TOTP, consumes the grant, and reopens the account. |
| `admin_offline_recovery` | `cancel_admin_offline_recovery()` | Closes an open grant while leaving the account disabled. |
| `http_auth` | `register_auth_routes()` | Registers the five concrete auth endpoints. |
| `http_access` | `validate_route_coverage()` | Ensures every registered route has one reviewed access policy. |

### Screens/Components

| Component | Purpose |
|-----------|---------|
| `webapp/browser_auth/login.html` | Public login shell with a script-rendered authentication panel. |
| `webapp/browser_auth/login.js` | Drives CSRF bootstrap, password, MFA, session confirmation, and logout. |
| `webapp/static/index.html` | Protected work page with session identity and logout controls. |
| `webapp/static/app.js` | Caches short-lived session CSRF proofs and attaches them to business writes. |

### API Calls

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/auth/csrf` | GET | Returns a proof for the current preauth, MFA, or session state. |
| `/api/auth/login` | POST | Verifies username and password and begins MFA. |
| `/api/auth/mfa` | POST | Verifies TOTP or recovery code and issues a session cookie. |
| `/api/auth/session` | GET | Returns current user and session deadlines. |
| `/api/auth/logout` | POST | Revokes the current session and clears cookies. |

Transport and CSRF failures return 400 or 403, state conflicts 409, malformed bodies 413/415/422, missing or invalid authentication 401, rate limits 429, and storage or crypto unavailability 503. The auth middleware records HTTP results; these endpoints are registered without an OpenAPI page.

`/api/auth/csrf` uses a custom bootstrap header so ordinary navigation cannot create preauthentication context. Once a session exists, its proof is bound to the validated session token and deadlines; the browser refreshes that short-lived proof when needed.

The login route exposes neither enrollment secrets nor recovery codes. The password step creates a challenge, not a session; the HTTP adapter exposes only the ordinary MFA path.

The auth gateway audits HTTP endpoint results, while the service records password, MFA, session, reset, and recovery events in the authentication store.

The browser uses the HttpOnly session cookie without reading it; it receives only a public CSRF proof and a bounded user/session view from the API. Protected business requests additionally need the membership described in [Workshop Access](./workshop-access.md).

## Integration Points

- **Depends on**: The auth SQLite store, configured HTTPS origin and host, TOTP encryption keys, CSRF signing keys, and a provisioned human account with enrolled MFA.
- **Used by**: The route gate, [Workshop Access](./workshop-access.md), and protected inquiry and draft operations.
- **External systems**: Browser cookies and a server deployment that presents the configured HTTPS origin; no external identity provider is called by this code.

## Extension Guide

Provisioning and both recovery procedures have no HTTP transport. A future operator interface must add an explicit route policy, preserve server-side authorization, and keep MFA secrets and recovery codes out of ordinary login responses. New protected routes must be added to `ROUTE_PERMISSIONS`, or startup rejects them. A reset alone leaves the target grant-required; use the grant-bound recovery procedure for re-enrollment.
