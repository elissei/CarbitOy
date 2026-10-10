# Web 4E-3A CP2B-2: authentication HTTP lifecycle

Historical checkpoint snapshot. [CP3B](http-authentication-checkpoint-3b.md) now
extends session-purpose CSRF verification to the existing business writes and
adds browser proof transport. Business identity attribution remains deferred.

The CP2A baseline was approved in the project planning conversation and restated
by the project owner for this implementation. It authorizes the five operations
below, using the committed authentication foundation and CP2B-1 primitives.
This document records the concrete transport decisions for independent review.
It is not production-readiness approval.

## HTTP contract

All requests use an exact configured HTTPS authority and exactly one Host header.
Authentication POSTs require exactly one `Origin` equal to that request authority
(and therefore to a configured external origin), and exactly one `X-CSRF-Token`.
GETs permit an absent Origin; a supplied Origin must also equal the authority.
`GET /api/auth/csrf` additionally requires exactly one `X-Carbit-Auth: bootstrap`
header. This fixed public marker is not a secret or a proof: it prevents ordinary
cross-site navigations/forms/images from starting a cookie flow when SameSite has
withheld the existing session. Cross-origin JavaScript needs a preflight to send
the custom header, and the application grants no such preflight/CORS permission.
There is no CORS grant or interpretation of forwarded headers in the application.
The ASGI server/proxy must preserve raw duplicate headers and be configured with
the trusted HTTPS and connection-address boundary described in Checkpoint 1.

POST bodies are UTF-8 JSON objects, at most 4096 bytes. Content-Type is
`application/json` or `application/json; charset=utf-8`. Duplicate JSON members,
nonfinite numbers, extra fields, wrong types and ambiguous framing are rejected.
Query parameters, Authorization headers and Transfer-Encoding are unsupported
on these operations. Passwords are neither trimmed nor logged.

| Operation | Input/state | Successful response |
| --- | --- | --- |
| `GET /api/auth/csrf` | `X-Carbit-Auth: bootstrap`; no auth cookies, one preauth cookie, preauth + MFA cookies, or one session cookie | 200: `{ "state": "preauth" / "mfa" / "session", "csrf_token": "public-proof", "expires_at": unix_seconds }` |
| `POST /api/auth/login` | Preauth cookie + its proof; `{ "username": "...", "password": "..." }` | 200: MFA-state proof in the same format; new preauth and encrypted MFA cookies |
| `POST /api/auth/mfa` | Matching preauth + MFA cookies and MFA proof; exactly `{ "totp_code": "..." }` or `{ "recovery_code": "..." }` | 200: `{ "state": "session" }`; fresh server session cookie; preauth/MFA cookies deleted |
| `GET /api/auth/session` | One valid session cookie | 200: `{ "user": { "user_id": "uuid", "username": "...", "display_name": "...", "role": "OWNER" }, "absolute_expires_at": unix_seconds, "idle_expires_at": unix_seconds }` |
| `POST /api/auth/logout` | One valid session cookie + session proof; `{}` | 204, empty body; server session revoked and all three cookies deleted |

The session response fields above name the concrete JSON keys; user values are
strings, including the UUID. All expiry values are UTC integer Unix seconds.
Proofs are public context-possession proofs, not authentication credentials.
No endpoint returns a session token, challenge token, decrypted anonymous secret,
TOTP secret, recovery-code list, password hash or configured signing key in JSON.
The MFA challenge and session bearer value are transported only in HttpOnly
cookies. A client obtains its logout proof through `GET /api/auth/csrf` after MFA.

Cookie names and options remain those defined by CP2B-1:
`__Host-carbit-preauth`, `__Host-carbit-mfa-challenge`, `__Host-carbit-session`;
Secure, HttpOnly, SameSite=Strict, Path=/, with no Domain. Issuance uses CP2B-1
helpers and matching Max-Age/Expires. Deletion preserves the security flags and
scope. A response never emits two Set-Cookie values for the same name.

## State transitions and server validation

Anonymous bootstrap creates a random ten-minute preauth context. Repeating
bootstrap in an intact preauth context issues another proof without replacing
the context. Successful password verification uses the existing Argon2id service
and replaces that context with an independently random context before sealing
the server challenge. Its envelope is bound to the new context and exact origin.
The server challenge identifies the verified user and auth version; no username,
user ID or raw challenge from the MFA request can select or substitute an account.

MFA reuses the existing TOTP/recovery verification and atomic challenge
consumption/session issuance transaction. Bootstrap in MFA state also checks the
persisted challenge's purpose, consumption, user enablement, MFA state, version
and expiry. A cryptographically valid envelope is not treated as an unrevoked
challenge. Retained consumed cookies cannot grant another session or obtain
another usable MFA proof. TOTP-step and recovery-code reuse protections remain.
Unenrolled/reset accounts receive the same generic login failure as other
rejected credentials. Their internal undisclosed ENROLL challenge grants no HTTP
access; no enrollment or recovery/reset route exists in this checkpoint.

Login in MFA or session state is rejected. The client must finish the pending
challenge or wait for its expiry; an authenticated account must log out before
logging in again. A session mixed with either anonymous cookie, or an MFA cookie
without its preauth cookie, is rejected. This avoids silently replacing an
authenticated identity or adopting a browser-selected bearer token.

`AuthService.inspect_session()` returns a validated user and persisted absolute
and idle deadlines, without exposing a bearer token. Session/CSRF GETs refresh
idle time only after validation. Logout validation and failed proof checks do
not refresh idle time. Session proofs last at most ten minutes and never exceed
either validated deadline. Absolute session time is never extended by polling,
bootstrap, proof issuance or cookie reissuance. HTTP configuration rejects limits
above eight hours absolute, thirty minutes idle, or five minutes for challenges.

When bootstrap confirms a syntactically valid session cookie is expired,
revoked or otherwise invalid in server storage, it creates a fresh anonymous
context. Only the confirmed invalid session cookie is deleted; anonymous
requests issue a preauth cookie without
deleting absent authentication cookies. Storage failures are distinct:
they return 503 without creating an anonymous context or deleting cookies.
Malformed encrypted contexts are rejected, not silently accepted or replaced.

Logout first validates the session and its proof, then calls server revocation,
which rechecks validity in its transaction. Only successful revocation emits
cookie deletion. On storage failure the cookie is retained for a later retry;
there is no claim that browser deletion alone revoked a server credential.
Already revoked/expired sessions receive 401 without deletion; bootstrap permits
the explicit restart described above.

## Boundary controls and failures

Raw Cookie headers are parsed before Starlette's cookie mapping. Multiple Cookie
headers, repeated security-cookie names, malformed/quoted security-cookie values,
and overlong values are rejected. Host and Origin cardinality are checked before
operation handling; duplicate bootstrap-marker, CSRF, Content-Type, Content-Length or Authorization
headers are also rejected. A combined Origin list or combined proof fails exact
validation. Origin validation is stricter for authentication than the historical
optional-Origin business gate.

All five authentication boundaries reserve persistent per-client and global
budgets through the existing rate-limit implementation, including malformed,
bad-Origin and CSRF-failing attempts: five per connection address and thirty
globally per operation per five-minute window. Successful requests also count.
Password/MFA service account, client and global budgets remain separate and
unchanged. The client key uses the ASGI peer address, not a cookie, request-body
field, port or X-Forwarded-For. Proxy trust and shared-address availability require
deployment review. Polling clients must respect these limits.

`AUTH_HTTP_RESULT` records only the allowlisted operation name and response status,
using the server clock, in the existing immutable event table. Existing password,
MFA, session-grant/revocation and rate-limit events retain server-derived subjects.
No request contents or raw security headers are recorded. If required storage or
audit writing is unavailable, the response is 503. A credential grant/revocation
may already have committed before a later boundary-audit/response failure; retry
must inspect current state. No successful response is claimed in that case.

All authentication responses use `Cache-Control: no-store`. Failure bodies contain
only a generic `detail`, never submitted values:

| Status | Meaning |
| --- | --- |
| 400 | Invalid Host/cookies, duplicate security headers or unsupported framing/Authorization |
| 401 | Rejected credentials, challenge or server session |
| 403 | Invalid/missing Origin, bootstrap marker, context or cookie-bound CSRF proof |
| 409 | Conflicting authentication cookie state or attempted login during an existing flow/session |
| 413 / 415 / 422 | Oversized body / unsupported content type / invalid bounded JSON contract |
| 429 | Existing persistent attempt budget exhausted |
| 503 | Missing configuration/runtime, unavailable auth/audit storage, crypto capacity or invalid encrypted MFA storage |

The exact route inventory now includes the five auth operations. All business,
HTML and static operations retain their existing server-session and role checks:
OWNER/SERVICE_MANAGER read/write, IT_ADMIN read only, OPENCLAW denied. Client role
and identity headers confer no authority. Client `actor`/`recorded_by` remain
untrusted legacy descriptive fields; this checkpoint supplies no approval bridge
or authenticated business-event attribution.

## Remaining deferrals and independent-review concerns

MFA enrollment, account/recovery/reset administration, recovery notifications,
password change/reset, browser login UI, trusted business/audit attribution and
approval-context integration remain deferred. The existing business-write CSRF
transport is unchanged: those routes still permit an absent Origin and do not
verify a proof. The approved baseline here applies mandatory Origin/proofs to
authentication state changes. Business-write CSRF/UI integration needs its own
reviewed follow-up before production use.

Anonymous envelopes/proofs remain stateless and reusable until their deadlines.
An old anonymous context cannot be cryptographically revoked by replacing its
cookie. Retaining a complete independent, unconsumed MFA flow still allows its
correct user to complete it until expiry; substituting a challenge, proof or
context across flows fails. Concurrent anonymous/password flows can coexist;
server MFA consumption prevents replay of each challenge. This retains CP2B-1's
model and introduces no parallel session/flow registry or schema.

Clients must serialize authentication transitions and discard outdated responses.
Browsers do not provide a compare-and-set cookie operation: concurrent successful
login/logout/bootstrap responses can arrive in a different order. An older
response cannot restore a revoked server session, but may overwrite the cookie
jar and require another bootstrap/login. In-flight authorized business requests
retain Checkpoint 1's per-request revocation limits. This checkpoint has no UI to
enforce client sequencing and does not claim to solve that browser ordering issue.

Independent review must examine the raw-header/parser boundary, exact flow/user
bindings, MFA consumption, safe logout/storage-error handling, additive HTTP
rate-limit/audit behavior, and these documented concurrency/business-CSRF limits.
Deployment, TLS/proxy trust, synchronized clocks, ingress limits, audit retention,
keys/backups, Ubuntu/hardware validation and production resource hardening remain
separate work. No deployment or Vercel demo change is part of this checkpoint.

## Verification commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_http_auth.py tests/test_webapp_http_access.py tests/test_webapp_auth_service.py tests/test_webapp_auth_csrf.py tests/test_webapp_auth_settings.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Verification on Windows AMD64 / CPython 3.13.5 using the existing `.venv`,
2026-10-10, with uncommitted CP2B-2 changes based on
`191154cb29a2d8a1c83658e63b977ae64481eb30`:

- Final focused suite: **331 passed, 1 warning in 78.54 seconds**.
- Final full suite: **706 passed, 1 warning in 134.21 seconds**.
- `pip check`: no broken requirements. No dependency installation or changes.
- `git diff --check`: passed; Git's LF-to-CRLF notices are unchanged environment
  behavior. New files were also checked for trailing whitespace and final newline.
- The sole pytest warning is the existing `StarletteDeprecationWarning` about
  HTTPX TestClient use. It was not addressed by changing the approved lock.

Changes remain uncommitted for independent GitHub Copilot security review.
