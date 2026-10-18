# Web 4E-3A CP3C: server-owned business and workshop identity

Based on `feature/web-app` at
`b2105209299218c26b4f9856bff59eb4b16d63f4`, the independently reviewed CP3B.
This checkpoint adds attribution and an internal trusted identity bridge, not
production-readiness approval. Changes remain uncommitted for independent review.

## Inventory and affected paths

No standalone CP3A inventory document is committed. The checkpoint-1 identity
deferrals, CP3B inventory and current code establish the relevant paths:

- `POST /api/works`: CP3B gate -> FastAPI body validation -> `create_work()` ->
  `WorkRepository.create_work()` -> inquiry/event/idempotency transaction.
- `PUT /api/works/{work_id}/labour`: same gate/body boundary -> `record_labour()`
  -> unchanged manual Autodata/domain pricing and VAT -> repository conditional
  version update and event transaction.
- Previously, request `actor` and `recorded_by` became event actors, and creation
  retry fingerprints included the supplied actor label. Those HTTP trust paths
  are removed here. The gate's server-owned User was not a client identity DTO.
- Internal `LabourPlanService` already uses `TrustedWorkshopContext.employee_id`
  for operation events, reviewer IDs and service retry ownership. It has no HTTP
  endpoint. The new bridge supplies that ID from a real persisted human session.
  Original Autodata recorder descriptions remain original source metadata; this
  checkpoint does not relabel historical source records or change planning rules.
  Nested original-time `recorded_by` values are not operation actor identity or
  verified source-recorder claims. Future plan HTTP transport must review source
  provenance separately; none is exposed by this checkpoint.

No database migration, schema change, historical event UPDATE, historical
idempotency rewrite, approval endpoint or parallel authentication system is added.

## Attribution representation and provenance

For newly authenticated operations, existing string fields `events[].actor`,
`labour_recorded_by` and labour-event `details.recorded_by` contain the canonical
auth-user UUID. They contain no supplied actor label or mutable display name.
The existing JSON-compatible event `details` gains:

```json
{
  "actor_attribution": {
    "version": 1,
    "provenance": "authenticated_session",
    "user_id": "stable-auth-user-uuid",
    "display_name": "display-name-at-operation"
  }
}
```

`BusinessActor` is a frozen server-captured UUID/name snapshot. Its constructor
requires a UUID and a nonblank name of at most 200 characters. Constructing that
object is not authentication. The only HTTP source is the token-only bridge over
the configured server AuthService; no identity object is parsed from request JSON.
The operation captures the persisted current name once and stores it with the
event. Renaming the account never rewrites earlier events or retry responses.
New events can therefore share a UUID while carrying different historical names.

The actor UUID must match the metadata UUID. For internal operations, a context
with attribution also requires its employee ID to match that UUID. New bridge
contexts enrich save/approval events with the same provenance snapshot; reviewer
and service retry IDs remain the stable UUID. Internal legacy contexts retain
their previous behavior and do not acquire authenticated provenance implicitly.

Old event rows, including UUID-looking or JSON-looking actor text, are unchanged.
Absence of this server-written version/provenance means legacy/unverified
attribution, never an authenticated identity inferred by matching a name or UUID.
The browser displays matching version-1 snapshots as the captured name and UUID,
with a signed-in-user label; legacy actors have an explicit self-reported-history
label. It uses `textContent`, including for names, UUIDs and legacy text.

This provenance describes the server's creation path; it is not a signature or
proof against later database tampering. Ordinary business events still lack
stronger append-only protection. Historical snapshots are immutable through these
application operations; stronger storage guarantees remain separately deferred.

## HTTP compatibility

The two routes, success/error statuses, work/event response shapes, monetary
strings, timestamps, UUID creation keys and optimistic versions are preserved.
Response actor/recorder strings now mean UUIDs for new authenticated operations;
clients that display names must use `details.actor_attribution.display_name`.
Legacy responses retain their original strings and details. The new metadata is
additive; no existing string field becomes an object.

Request `actor` and `recorded_by` are deprecated, optional and ignored for
attribution and fingerprinting. Omission or JSON null is accepted. Supplied strings
retain their previous 1..200-character limit and nonblank validation; wrong types,
oversized/empty/blank strings fail 422. Extra `actor_user_id`, attribution, User,
context, permissions, `approved_by` or `created_by` fields remain forbidden.
They cannot override the resolved session identity. The browser removes both
editable actor fields and submits neither field, while retaining forms, draft
preservation, submit locks, creation keys, version-conflict handling and CP3B's
in-memory CSRF proof transport. No bootstrap or write retry is introduced.

## Creation idempotency transition

New authenticated creation fingerprints use the existing `request_json` column:

```json
{
  "contract": "carbit.create-work.authenticated.v1",
  "actor_user_id": "stable-auth-user-uuid",
  "registration": "NORMALIZED-REGISTRATION",
  "customer_description": "validated-business-content"
}
```

The stable UUID and explicit contract are mandatory in this new path. Display
names, supplied labels and attribution display snapshots are excluded. Sorted
JSON comparison retains the existing exact canonical-content matching behavior.
The global key constraint and `BEGIN IMMEDIATE` serialize concurrent creation.

- Same user/key/content: return the original saved response, including its original
  name snapshot, after renaming, resource changes or process restart. No extra event.
- Different user with the same key: 409 even for the same content/display name or
  a supplied label matching the first UUID. No mutation and no idle refresh.
- Changed registration/description with the same key: 409. Changing only an ignored
  valid legacy label does not conflict. Distinct keys remain distinct requests.
- Legacy fingerprints have the old actor-label contract. An authenticated request
  cannot match them, even if that label happens to be the current UUID. Return 409;
  do not infer ownership, adopt the old response or rewrite old data. Old callers
  must inspect existing work and use a fresh key only for an intentional new inquiry.

Offline repository callers may still explicitly use a legacy string actor. This
preserves existing internal fixtures/import-style APIs and their old retry contract;
it never emits authenticated provenance. HTTP handlers always pass `BusinessActor`.
Labour PUT retains its existing expected-version concurrency mechanism, with no
new key or automatic retry. A stale version remains 409, with no extra event.

Authenticated internal workshop save/approval calls also use an explicit
`carbit.workshop.authenticated.v1` request fingerprint containing operation,
stable `actor_user_id` and the original command. No display snapshot is included.
Their existing key scope is inquiry + operation + key: reuse in that scope by
another actor, ambiguous multiple records, or an unversioned legacy record fails
with `IdempotencyConflictError` before mutation. `BEGIN IMMEDIATE` makes that
lookup and record insertion atomic. Same-user exact retries retain historical
responses and events after a rename. Explicit offline legacy contexts preserve
the old per-actor contract; they cannot acquire authenticated provenance from
this compatibility path. No old service retry record is rewritten.

## Trusted workshop boundary and concurrency

`resolve_workshop_context(service, session_token, required_permission=...)` takes
only a real opaque session token as authentication proof. The configured service
and required permission are server-owned. The resolver performs a fresh non-touching
`inspect_session()` and derives an immutable permission set:

| Role | Internal workshop permissions | Human business HTTP policy |
| --- | --- | --- |
| OWNER | Read, save, approve | Read/write |
| SERVICE_MANAGER | Read, save, approve | Read/write |
| IT_ADMIN | Read only | Read only |
| OPENCLAW | None; no human session | Denied |

An untyped permission, User/UUID/dictionary/context as token, invalid session,
disabled or MFA-incomplete identity, auth-version mismatch, expiry or revocation
cannot produce a trusted context. Context construction by trusted Python callers
remains an internal contract, not a public authentication API.

The HTTP handlers require workshop writer eligibility (SAVE_LABOUR_PLAN permission)
solely to derive their actor; they do not invoke planning/save/approval operations.
This fresh bridge check occurs after body validation and before pricing/mutation,
in addition to CP3B's pre-body session/role/Origin/cookie/header/proof checks.
It closes changes observed between the gate and handler: role denial 403, invalid
session 401, unavailable/invalid auth data 503, all without mutation or idle touch.
No bearer, permission object or context is returned in business responses.

Business mutation, event metadata and creation retry response remain atomic in
their existing business transaction. CP3B's only write-path idle refresh remains
after successful handler completion, revalidating current session and role under
the auth database write lock. Rejections, validation failures and conflicts do not
touch. Session lifetimes, CSRF lifetimes, cookies and all auth budgets are unchanged.

Authorization remains per-operation. Auth and business databases are separate:
a role change/revocation committed after the final bridge check cannot cancel an
already-running mutation. A captured internal context must be newly resolved for
each operation; it must not be cached as a lasting capability. A later future
approval/final quote endpoint needs separately reviewed atomic authorization and
version guarantees. No cross-database lock protocol or token stored in context is
introduced. A post-commit refresh failure preserves committed success and skips
refresh, as documented in CP3B.

## Verification and remaining deferrals

Run using the existing `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_business_identity.py tests/test_webapp_business_csrf.py tests/test_webapp_http_access.py tests/test_webapp_http_auth.py tests/test_webapp.py tests/test_webapp_labour_service.py tests/test_webapp_auth_service.py tests/test_webapp_browser.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Regressions cover actor/recorder spoofing and extra identity DTOs, bounded deprecated
fields, omitted labels, current UUID/name capture, historical snapshots, same-user
retries and restarts, cross-user/legacy-key conflicts, concurrent key reuse, event
rollback, internal permission/identity mapping and attributed internal operations,
session/role/status/version/storage changes at the handler bridge, browser field
removal and legacy provenance display. Existing CP3B protections, browser races,
lost responses, role isolation, workshop persistence and domain/pricing checks remain.
The restart retry fixture now preserves the original real session rather than
authenticating a newly provisioned account. The old actor-label conflict assertion
now verifies safe replay for an ignored label; business-content conflict tests remain.
Browser validation continues to check Finnish errors on the business description
field after removing the editable actor field; draft/error checks remain intact.

Verification in the existing Windows AMD64 / CPython 3.13.5 `.venv`:

- Initial attribution/business/browser run: **114 passed, 1 warning in 66.55s**.
- Expanded final focused command above: **529 passed, 1 warning in 196.03s**,
  including all 80 new identity/bridge cases and 17 Chromium browser tests.
- Full suite: **943 passed, 1 warning in 232.14s**.
- Sole pytest warning: existing Starlette/HTTPX TestClient deprecation.
- `git diff --check` plus tracked/untracked whitespace and final-newline checks:
  passed. No dependency changes; existing `.venv` used throughout.
- Desktop/mobile Chromium screenshots inspected: actor inputs removed and
  historical UUID/name attribution readable within the existing responsive layout.
- Branch/HEAD remain `feature/web-app` at
  `b2105209299218c26b4f9856bff59eb4b16d63f4`. Final scope is eight modified
  files and four new files, all unstaged/uncommitted:
  `webapp/business_identity.py`, `webapp/workshop_identity.py`, `webapp/main.py`,
  `webapp/database.py`, `webapp/labour_service.py`, `webapp/static/app.js`,
  `webapp/static/index.html`, `tests/test_webapp_business_identity.py`,
  `tests/test_webapp.py`, `tests/test_webapp_browser.py`,
  `docs/http-authentication-checkpoint-3b.md`,
  `docs/http-authentication-checkpoint-3c.md`.
- Protected CP3B auth/CSRF/session/rate-limit files, schema/migration files,
  domain/services/dependency definitions and demo files remain unchanged. The
  changed-file credential-marker scan found no secrets, credentials, local
  database or environment files. No commit, push, merge or deployment performed.

Remaining deferrals: stronger append-only/tamper-resistant business auditing,
business denial auditing, future approval/final-quote HTTP atomic authorization,
account administration and lifecycle/UUID non-reuse policy, operational TLS/proxy,
key/backup/resource hardening, MFA enrollment/recovery/login UI, remembered devices,
WebAuthn, WhatsApp and scoped OpenClaw service integration. No Vercel demo file,
domain ontology, Autodata rule, pricing/VAT/parts logic or schema changes are made.
