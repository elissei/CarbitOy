# Carbit Web CP4A: browser authentication and safe session UX

Based on `feature/web-app` at `d19d691a81b40dd5263351ed644abc9269505d34`.
The project owner explicitly approved the new anonymous browser-delivery boundary
before implementation. This checkpoint is not deployment or production approval.
Carbit's existing CRM is autokorjaamo.fi; its integration belongs to CP4B, not here.

## Exact public boundary

Only GET `/login`, `/login.js` and `/login.css` serve public, fixed files in
`webapp/browser_auth`. There is no public directory mount, wildcard file resolver,
business data injection, bearer credential or identity in these assets. HEAD/POST
are not newly authorized. Root, all `/static` files (including alternate index
and the labour-review demo) and all business routes remain session/role protected.
Anonymous users must open `/login`; root deliberately continues to return 401.

All three paths are explicitly classified in the fail-closed route inventory.
Required auth runtime configuration, exact HTTPS/Host checks, supplied Origin
validation and raw-cookie ambiguity rejection run before file delivery. Public
delivery does not inspect/touch a session, issue cookies or reserve an auth budget.
Existing auth endpoints still enforce their unchanged budgets and audit policy.

Responses declare HTML, JavaScript or CSS content types, `Cache-Control: no-store`,
`Pragma: no-cache`, `Expires: 0`, nosniff, no-referrer, DENY framing, restricted
Permissions-Policy and CSP: no default resources, same-origin scripts/styles/fetch,
no form navigation, base override, embedding or objects. No inline scripts/styles
or external dependencies are needed. This CSP protects the public login page;
it does not claim to add a new CSP to the existing private application/demo.

## Existing HTTP contract, unchanged

| Action | Contract |
| --- | --- |
| Bootstrap | GET `/api/auth/csrf`, `X-Carbit-Auth: bootstrap`; `{state, csrf_token, expires_at}` for preauth, MFA or session. |
| Password | POST `/api/auth/login`, JSON `{username,password}`, preauth proof; returns rotated MFA proof/cookies. |
| MFA | POST `/api/auth/mfa`, exactly one of `{totp_code}` or `{recovery_code}`, MFA proof; session state is followed by a real GET session confirmation. |
| Session | GET `/api/auth/session`; validates UUID/role and absolute/idle deadlines; this endpoint touches idle time. |
| Logout | POST `/api/auth/logout`, JSON `{}`, session proof; exact 204 confirms server revocation and cookie clearing. |

Authentication POSTs retain strict Origin, cookie and CSRF validation. Cookies
remain Secure, HttpOnly, SameSite=Strict, host-only. Passwords are not trimmed.
There is no enrollment, reset, recovery administration, remembered-device or
service-account browser flow. The server's existing recovery-code verification
contract is exposed as an alternative single-use MFA verification method, not an
account recovery/reset flow.
Lifetimes remain eight hours absolute, thirty minutes idle, five minutes MFA and
ten minutes CSRF. No auth service, crypto policy, schema or business rule changed.

## Browser state and failure handling

The Finnish responsive interface exposes checking, unauthenticated, MFA-pending,
authenticated, expired, authorization-denied and uncertain states. Forms have
labels, autocomplete hints, keyboard controls, a focused heading on transitions
and a live status region. IT_ADMIN gets read-only controls; the backend remains
authoritative. OPENCLAW cannot log in as a human.

Auth operations are serialized in each document, with a fifteen-second timeout.
Credentials are cleared from inputs on submission and failures; password/MFA
inputs are also cleared on pagehide. Auth failures use fixed Finnish text, never
server detail, submitted username, password, code or URL query data. Network
loss, malformed response, timeout, 409, CSRF failure or unavailable storage never
implies success. An explicit "Tarkista kirjautumistila" action reconciles current
server state before another attempt. No POST is automatically retried. A 429
instructs the user to wait at least five minutes; there is no retry countdown
request or polling. The server budget remains the actual authority.

MFA requires both a successful MFA response and a validated session response.
Login-page session confirmation navigates only to fixed `/`, never a supplied
return URL. A session proof found during explicit reconciliation is reused by
the private application's existing in-memory CSRF cache after session confirmation.
Normal app entry does one GET session inspection and keeps lazy, coordinated
business proof refresh; it does not bootstrap for every read or poll in background.

An in-memory timer uses the confirmed absolute/idle deadlines. Accepted business
responses conservatively estimate idle refresh from request start, not response
arrival; the absolute deadline never moves. Timers send no requests. Server 401
or confirmed non-session bootstrap locks the UI immediately. Permission denial
is separate from session expiry; a generic proof rejection remains a failed
write with invalidated proof and no automatic retry.

No password, TOTP secret/code, bearer token or CSRF proof is put in localStorage,
sessionStorage, URLs or console logs. Browser cookies are written only by the
existing server. Browser history restoration locks the private UI until explicit
server confirmation. BroadcastChannel messages contain only a lock notification,
not an identity or secret, and cannot authorize another tab. Pending auth and
business responses are fenced by an in-memory generation counter.

## Draft ownership and business semantics

On expiry/uncertainty the private shell is hidden and inert, an open inquiry
dialog is closed without resetting it, and cached proof/read generations are
invalidated. Unsent fields, inquiry key and original labour version stay only
in that document's memory/DOM. Same-UUID server confirmation restores them
without re-rendering fields, changing the key/version or replaying a mutation.
IT_ADMIN cannot resume a write dialog. A different authenticated UUID discards
the prior user's fields, selected work, list, history, price display and key
before the new user's list is loaded. Confirmed logout discards these immediately.
Reload/navigation loses in-memory drafts; no durable draft storage is introduced.

Drafts are unavailable while locked but are not encrypted against someone with
physical/devtools access to that already-open browser. This is intentional
in-memory preservation, not confidential storage on a shared workstation.

Existing CP3B request locks, optimistic version handling, idempotency and CP3C
attribution remain. Lost business responses are ambiguous: a write may already
have committed. No auth check, reauthentication or timeout replays it. The user
must inspect current work or deliberately retry the existing safe creation key.

## Administrator-side provisioning prerequisite

Real employees need individually provisioned accounts and completed MFA before
using this UI. A trusted offline operator uses the existing internal
`provision_identity`, `begin_login`, `begin_mfa_enrollment` and
`confirm_mfa_enrollment` services and securely hands recovery codes to the user.
This checkpoint adds no operator CLI, registration grant, administrator HTTP
permission or enrollment shortcut. The existing lifecycle/recovery documentation
still governs these trusted operations. Unenrolled/reset accounts receive the
same generic login failure as other rejected accounts. Do not use test fixture
tokens, shared employee accounts or injected cookies as operational provisioning.

Operational use also requires all six validated auth environment settings,
independent TOTP/CSRF keys, access-controlled auth storage and trusted HTTPS.
Plain HTTP README startup instructions are not sufficient. TLS/proxy, clocks,
backups, secrets and resource limits remain separately reviewed operational work.

## Limitations and independent-review focus

Cookie updates across documents have no browser compare-and-set. Notifications
only lock cooperative tabs; simultaneous server cookie responses may still
require explicit reconciliation. In-flight authorized mutations retain CP3C's
per-operation revocation limitation. No cross-database locking was added.
Local UI clocks/timers are conservative UX, not authentication authority.
Suspended timers are checked again before an application request; this does not
promise remote revocation is detected while an idle page makes no requests.

A rejected/consumed MFA flow cannot be cancelled/replaced through the current
API. Its cookies must expire before starting a fresh password flow. The UI does
not delete HttpOnly cookies or invent a reset endpoint. Enrollment/recovery
administration and operational provisioning tooling remain
deferred. Remembered devices, WebAuthn, WhatsApp/OpenClaw, CRM and suppliers are
outside this checkpoint. Existing audit/storage/deployment deferrals remain.

Independent review should focus on the exact public file allowlist, CSP/cache
headers, uncertain-response reconciliation, draft UUID binding, stale responses,
logout, cross-tab ordering, auth budgets and preservation of private routes.

## Verification

Run focused and full tests using the existing `.venv`; tests use isolated
temporary SQLite stores and local TLS/Chromium fixtures. No real business data,
schema changes, dependency installs or deployment are involved.
Verification on Windows / CPython 3.13.5 in the existing `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_public_login.py tests/test_webapp_browser_auth.py tests/test_webapp_browser.py tests/test_webapp_http_access.py tests/test_webapp_http_auth.py tests/test_webapp_business_csrf.py tests/test_webapp_business_identity.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_public_login.py tests/test_webapp_browser_auth.py tests/test_webapp_browser.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_browser_auth.py::test_explicit_reconciliation_recovers_initial_business_read_failure
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

- Expanded auth/CSRF/attribution focused run: **440 passed, 1 warning in 224.41s**.
- Subsequent browser/public-boundary run: **62 passed, 1 warning in 77.81s**.
- Final added initial-read recovery regression: **1 passed, 1 warning in 2.96s**.
- Final full suite, after all code/test changes: **989 passed, 1 warning in 303.25s**.
  This includes 46 new CP4A cases (20 HTTP and 26 Chromium browser cases),
  plus the preserved existing tests. Desktop/mobile login screenshots inspected.
- Sole warning in these runs: existing Starlette/HTTPX TestClient deprecation.
  No dependencies were changed to suppress it.
- Final tracked/untracked whitespace and newline checks and `git diff --check`
  passed. Credential-marker scan found no embedded credentials. Protected auth
  contracts/services, schemas/migrations, business storage/handlers, attribution,
  pricing/domain, dependencies and demo files verified unchanged.

Final scope: seven modified and six new files, all unstaged and uncommitted:

```text
tests/test_webapp_browser.py
webapp/README.md
webapp/http_access.py
webapp/main.py
webapp/static/app.js
webapp/static/index.html
webapp/static/styles.css
docs/browser-authentication-checkpoint-4a.md
tests/test_webapp_browser_auth.py
tests/test_webapp_public_login.py
webapp/browser_auth/login.html
webapp/browser_auth/login.js
webapp/browser_auth/login.css
```

Branch/HEAD remain the starting values above. No commit, push, merge, deployment,
Vercel change or CP4B implementation performed. Independent security review is
still required; passing tests do not constitute that verdict.
