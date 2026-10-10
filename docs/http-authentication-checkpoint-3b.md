# Web 4E-3A CP3B: business-write CSRF and browser transport

CP3B extends the existing CP2B session-purpose proof to the two existing
business writes. It introduces no authentication mechanism, route, schema,
business identity bridge or pricing change. This is not production-readiness
approval. Changes require independent security review before a local commit.

## HTTP contract

| Operation | Required transport | Existing business body/result |
| --- | --- | --- |
| `POST /api/works` | Valid human writer session; exact HTTPS Origin; session-bound `X-CSRF-Token` | Registration, description, legacy actor and UUID idempotency key; 201 or existing validation/conflict response |
| `PUT /api/works/{work_id}/labour` | Same transport | Expected version, hours, description, legacy recorder and optional reference; 200 or existing validation/conflict response |

The existing mandatory JSON fields, text limits, Decimal calculations, source,
pricing, idempotency records and version checks are unchanged. The auth endpoint's
4096-byte body cap is not applied to business bodies. Resource/body identifiers
and legacy actor text confer no authorization. Business reads retain their
existing HTTP contract. No PATCH, DELETE or approval operation is added.

All requests still require one Host matching a configured HTTPS authority.
For business writes, exactly one Origin must equal that request's exact configured
authority. Missing Origin, `null`, lists, noncanonical values and an Origin for a
different configured authority are rejected. There is no CORS grant or new proxy
trust. Browsers supply Origin; JavaScript does not invent a trusted Origin header.

Raw cookie parsing rejects multiple Cookie headers, duplicate security-cookie
pairs and malformed/quoted/overlong security values. Writes require an unambiguous
session-only security-cookie state: session plus preauth/MFA cookies is rejected.
Duplicate Host, Origin, X-CSRF-Token, X-Carbit-Auth, Content-Type, Content-Length and
Authorization headers are rejected. Authorization and Transfer-Encoding are
unsupported, matching the authentication boundary. No lossy cookie mapping or
combined proof is accepted as authoritative.

Business Content-Length, when supplied, must be one bounded decimal value;
combined/malformed lengths and combined Content-Type values are rejected. Wire
framing and actual body-length enforcement remain the ASGI server's responsibility.

Authorization and CSRF validation happen before FastAPI parses the business body
or dispatches its handler. An enabled MFA-completed human OWNER or SERVICE_MANAGER
is required. IT_ADMIN remains read-only and OPENCLAW remains denied. Invalid
session state returns 401, denied roles 403, malformed headers/cookies 400,
conflicting session cookie states 409, missing/invalid Origin or proof 403, and
missing CSRF runtime/auth storage failures 503. Errors contain generic detail
strings, retain `Cache-Control: no-store`, and issue/delete no cookies.

## Existing proof and session lifecycle

Clients call `GET /api/auth/csrf` with `X-Carbit-Auth: bootstrap` and the existing
HttpOnly cookie. They must receive `state: session`; anonymous or MFA-state proofs
cannot authorize a business write. The existing proof is bound to the canonical
session bearer, exact Origin, `session` purpose, server-validated absolute/idle
deadlines, signing key and nonce. MAC and expiry validation remain CP2B primitives.

Proofs are reusable, not single-use mutation capabilities. The same session proof
can protect logout and either business write. Proof possession never overrides
roles, revocation, idempotency or optimistic version checks. Expiry remains at most
ten minutes and cannot exceed either session deadline. Human sessions remain at
eight hours absolute and thirty minutes idle; cookie policy is unchanged.

Both pre-dispatch session inspections are read-only. After transport and proof
verification, the second inspection revalidates the current session and writer
role before FastAPI body validation and handler dispatch. Invalid proofs, role
denials, malformed/invalid bodies, domain validation errors, missing resources
and business conflicts do not refresh idle time. Only a successful 2xx handler
response attempts a refresh through `AuthService.refresh_session_for_roles()`.

The refresh acquires the existing auth repository's SQLite `BEGIN IMMEDIATE`
write transaction, samples the clock after acquiring that lock, and reuses
`_session()` to recheck revocation, absolute/idle expiry, current human identity,
enablement, MFA status and auth version. It checks the current OWNER or
SERVICE_MANAGER role in that same transaction before updating `last_seen_at`.
Role changes and revocation cannot commit between those checks and the update.
Session inspections also sample time inside their transaction, preventing a
queued inspection from using a pre-lock expiry snapshot. Absolute and idle
lifetimes remain unchanged.

This remains per-request authorization: later revocation cannot cancel an
in-flight body/handler/business transaction. Separate auth/business databases
are not held under a new coordinated locking protocol. If identity, role or
session validity changes during an already-authorized successful handler, the
post-success refresh declines to touch and preserves the committed business
success response. An auth storage/runtime failure during that refresh logs a
generic warning without request data, skips refresh, and also preserves the
committed result. It does not report a rejection after a successful mutation or
introduce an automatic retry. Pre-dispatch auth storage failures still fail
closed before any mutation. A cross-database authorization/mutation/refresh
transaction would require a separate reviewed architecture change.

No auth rate-limit budgets or audit behavior are changed. Password/MFA account,
client/global budgets and per-operation auth HTTP budgets remain. Business writes
do not consume a new password/MFA/HTTP-auth budget; obtaining a proof uses the
existing rate-limited bootstrap endpoint.

## Browser transport and compatibility

The existing same-origin API helper lazily fetches a proof before a mutation.
Reads and page loading do not bootstrap. A single shared pending promise coordinates
concurrent refresh callers. Only session-state proofs are cached, only in memory;
no cookies, localStorage or sessionStorage are read for a bearer/proof cache.

Cache validity uses wall-clock expiry and a monotonic elapsed-time deadline,
bounded by the proof's advertised lifetime, with a five-second expiry margin.
The browser's checks are usability checks; the server performs authentication,
role and cryptographic validation. Clock skew may cause a server rejection; it
never extends server validity. Refresh has no background timer or polling, keeping
existing bootstrap budgets intact. Multiple tabs retain separate caches/budgets.

401/403 API responses invalidate the local cache and its refresh generation.
A pending older refresh cannot populate the cache after invalidation. Bootstrap
failures and non-session responses prevent the pending business request entirely.
No bootstrap or mutation is automatically retried. A user may explicitly submit
again after an error; a later submission can fetch a fresh proof.

Existing forms, editable legacy actor fields, pending-submit locks, selected-work
epochs, UUID creation keys and version-conflict handling remain unchanged. Failures
leave draft content and the creation key/expected version intact. A lost creation
response can be manually retried with the same key. A lost labour response is not
replayed automatically; a manual retry with its old version conflicts if the first
write committed. Existing Finnish field errors and server detail strings retain
their display path.

Old callers must add Origin and a session proof; there is no compatibility bypass
for the former unprotected write transport. Deploy/release coordination belongs
to a later approved step. Updating identity bodies or relabelling old history is
CP3C, not CP3B. The fictional local review preview and Vercel demo are unchanged.

## Remaining CP3C and architecture deferrals

Client actor/recorded_by still reach storage and displayed events unchanged. They
are self-reported labels, never trustworthy audit identity. CP3C must derive a
stable business actor from the validated human session, define historical
provenance, and review creation-retry compatibility. The internal
TrustedWorkshopContext bridge and approval HTTP operations remain absent.
Ordinary business events are not protected by the decision-linked event
immutability triggers; stronger append-only auditing needs an approved migration.
Business denial auditing and atomic authorization for final approval need design
review. Existing auth concurrency, proxy/TLS, key management, production resource
hardening and deployment deferrals remain.

Approved future architecture requirements, documented only:

- Human users should eventually have remembered-device login and
  passkey/WebAuthn-based biometric reauthentication after longer inactivity where
  supported. A separately reviewed device/reauthentication design must cover trust,
  enrollment, revocation, expiry, recovery and fallback. CP3B preserves current
  eight-hour/thirty-minute limits and adds no device credential or new lifetime.
- OpenClaw should remain continuously available through WhatsApp independently
  of human browser sessions. It remains a separate nonhuman identity with no human
  role or business-route access here. Future integration needs explicitly scoped
  service permissions, connection monitoring, reconnect/recovery behavior and a
  reviewed service-credential lifecycle. No WhatsApp integration is implemented.

## Independent review finding and targeted correction

Copilot's independent review reported **BLOCKED — FIX REQUIRED**, with a
confirmed Low finding: the original second session inspection touched idle time
before its writer-role check and before FastAPI business-body validation. A
concurrent role downgrade could therefore return 403 while refreshing the
session, and invalid bodies could return 422 while refreshing it. No business
authorization bypass or mutation was confirmed.

Regression tests reproduced the finding before the correction: **8 failed,
6 passed, 90 deselected, 1 warning in 6.59 seconds**, covering both business
operations. The correction above removes all pre-dispatch write-path touches
and uses an atomic, current-role-validated post-success refresh. It changes no
business handler, body model, pricing rule, schema, role matrix, CSRF primitive,
browser behavior or authentication rate limit. Other intentional session touches
on authenticated business reads and authentication session/bootstrap GETs remain.

Additional regressions assert unchanged persisted `last_seen_at` on rejection,
successful refresh for both writer roles, role/status/MFA/version/revocation and
expiry changes after proof verification, domain validation and conflict failures,
and concurrent changes before/after the refresh lock. Real independent SQLite
connections verify the write lock spans session validation and update. A queued
refresh must observe committed state and the clock after lock acquisition.
Post-handler invalidation and refresh-storage failures preserve the committed
response without touching. This correction remains uncommitted for another
independent review; prior review approval must not be inferred.

## Verification

Use the existing `.venv` without changing dependency definitions:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_business_csrf.py tests/test_webapp_http_access.py tests/test_webapp.py tests/test_webapp_http_auth.py tests/test_webapp_auth_service.py tests/test_webapp_browser.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Dedicated security tests use raw HTTPS TestClient instances. Legacy business
regressions use genuine MFA sessions and explicitly generated, validated-session
proofs in their helper, avoiding artificial changes to HTTP rate limits. Browser
tests use the real bootstrap endpoint, local TLS and genuine MFA sessions.
Security coverage includes exact Origin, header/cookie ambiguity, wrong contexts,
tampering/expiry, cross-session/origin substitution, fail-closed runtime/storage,
revocation/role changes before dispatch, rejected-write idle deadlines, proof reuse
with concurrent creation/version guards, and existing long business text limits.
Browser coverage includes lazy caching, concurrent refreshes, expiry, bootstrap
failure, 401/403 invalidation, form/key preservation, non-session rejection and lost
mutation responses without automatic replay.

Final verification on Windows AMD64 / CPython 3.13.5, existing `.venv`,
2026-10-11, based on `d340e25d28a127ef6e59838879bb6a4c7bff154b`:

- Targeted correction focused command above: **393 passed, 1 warning in
  174.90 seconds**. This includes all 146 dedicated business security cases,
  authentication/service regressions and all 15 Chromium browser tests.
- Targeted correction full suite: **861 passed, 1 warning in 210.35 seconds**.
- Previous CP3B verification, before the review finding: focused **287 passed,
  1 warning in 127.04 seconds**; full **811 passed, 1 warning in 190.80 seconds**.
- `pip check`: no broken requirements; no installation/dependency changes.
- `git diff --check` and new-file whitespace/final-newline checks: passed.
- Final branch/HEAD unchanged: `feature/web-app` at
  `d340e25d28a127ef6e59838879bb6a4c7bff154b`. Working scope: eight modified
  tracked files and two untracked CP3B files, all unstaged. The targeted fix changes
  only `webapp/http_access.py`, `webapp/auth_service.py`, this document and
  `tests/test_webapp_business_csrf.py`; the six other original CP3B files are
  preserved. No commit, push, merge, deployment or demo change was performed.
- Sole pytest warning: existing Starlette/HTTPX TestClient deprecation. Git's
  LF-to-CRLF notices reflect the existing Windows configuration.

Changes remain unstaged/uncommitted for independent security review. Remembered
devices, WebAuthn, WhatsApp, CP3C, project AGENTS.md and skill installation are
not implemented by this checkpoint.
