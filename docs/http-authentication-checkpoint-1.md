# Web 4E-3A: Checkpoint 1 — fail-closed HTTP boundary

Implemented: explicit authentication configuration, lifecycle initialization of
the existing authentication foundation and a server-owned HTTP access gate.
There is **no browser login, MFA verification, cookie issuance or recovery route**.
The normal browser interface therefore returns 401 until Checkpoint 2 supplies
the approved login transport. This is intentional, not a development bypass.

## Configuration and startup

All four environment variables are required; missing or invalid values stop
startup before the business database is initialized:

| Variable | Required value |
| --- | --- |
| `CARBIT_AUTH_DATABASE` | Absolute SQLite file path in an existing, access-controlled directory outside `webapp/static`. No default. |
| `CARBIT_ALLOWED_HOSTS` | Comma-separated exact lowercase DNS names or IP addresses, without wildcards, ports or whitespace. |
| `CARBIT_EXTERNAL_ORIGINS` | Comma-separated exact HTTPS origins, including non-default ports, without paths/trailing slashes; omit the default port 443. Every origin's hostname must be allowed. |
| `CARBIT_TOTP_KEYS` | Comma-separated Fernet encryption keys supplied by the operator's protected secret configuration. First key encrypts; the complete list permits decryption during a deliberate rotation. No generated or repository-stored production key. |

The authentication directory is deliberately not created by the application.
Use a separate auth database from the business database, with OS access limited
to the service account and authorized maintenance. Startup safely applies the
existing additive authentication migration; it provisions no accounts. Business
schema, data migration, labour calculations and idempotency remain unchanged.
One AuthService (and its PasswordService concurrency limit) exists per application
lifespan/process. Startup also rejects an unclassified or duplicate route.

Code-driven tests pass a validated `AuthSettings` instance explicitly. There is
no authentication-disabled setting. Existing authentication policy remains:
8-hour absolute session lifetime, 30-minute idle timeout, five-minute challenges,
five attempts per account/client and thirty global attempts per five minutes.
Login rate limiting remains internal; no password/MFA HTTP operation is exposed.

`SessionCookiePolicy` remains `__Host-carbit-session`, Secure, HttpOnly,
SameSite=Strict, Path=/ and no Domain. This checkpoint only reads that cookie;
it never issues one. Its value must resolve through `AuthService.require_session()`
to an enabled, MFA-completed human account. Password challenges and service
identities are not sessions. Browser-supplied identity/role headers are ignored.

## Exact route policy

| Registered operation | OWNER / SERVICE_MANAGER | IT_ADMIN | Anonymous / OPENCLAW |
| --- | --- | --- | --- |
| `GET /api/works` | Read | Read | Denied |
| `POST /api/works` | Write | Denied | Denied |
| `GET /api/works/{work_id}` | Read | Read | Denied |
| `PUT /api/works/{work_id}/labour` | Write | Denied | Denied |
| `GET /` | Read HTML | Read HTML | Denied |
| `/static` mount, including GET/HEAD HTML and assets | Read | Read | Denied |

The whole static mount is private in this checkpoint, including
`/static/index.html` and the fictional local labour-review preview. No production
data is made public through alternate HTML paths. The separate Vercel demo is
unchanged. IT_ADMIN can view the existing UI, but its write requests receive 403;
role-aware UI controls are later work. No labour approval endpoint is introduced.

Interactive docs, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` are disabled
in **all** configurations. Unregistered paths remain 404, unsupported methods 405,
and ordinary router slash redirects carry no business content. Registered routes
without a reviewed policy fail startup; a route added afterward receives 403.
New mounts/administrative routes must pass the same explicit route review.
Protected requests return generic 401 for absent/invalid sessions, 403 for denied
roles, and 503 for unavailable authentication storage. Authorization precedes
business reads, writes and request-body validation. Protected responses have
`Cache-Control: no-store`.

## HTTPS, origins and proxy trust

Every request must have exactly one Host header and an ASGI scheme/authority
matching a configured HTTPS origin. An Origin header, when present, must match
an exact configured origin. Arbitrary Host, X-Forwarded-Host and X-Forwarded-For
are not identity sources. The application does not interpret forwarded headers.
There is no permissive CORS middleware.

For direct TLS, configure Uvicorn with its certificate/key and
`--no-proxy-headers`. For future TLS termination, the reverse proxy must validate
the external host, replace forwarded headers, and be the **only** trusted proxy
address in the ASGI server configuration (`--forwarded-allow-ips`); never use `*`.
Bind the backend to an inaccessible loopback/Unix socket and block direct client
access. ASGI scheme is only trustworthy when that server/proxy boundary is
configured correctly. Tailscale network access does not replace authentication.
No reverse proxy or production service is installed by this checkpoint.

Tests use TestClient with an explicit HTTPS URL, or a local TLS server and
ephemeral self-signed certificate. Chromium ignores certificate verification
**only in the isolated test context**; it still uses a Secure/HttpOnly/Strict
cookie holding a genuine password-and-TOTP-created session. No application
setting weakens HTTPS, MFA or cookie flags for Windows development.

## Remaining boundaries and risks

- **Checkpoint 2:** approved login/MFA transport, cookie issuance/expiry/logout,
  CSRF token transport and rejection of unsafe requests without proof. Exact
  Origin checking when present and SameSite are defense in depth; an absent
  Origin is currently accepted and this is **not complete CSRF protection**.
- **Checkpoint 3:** server-derived audit attribution and the approval-context
  bridge. `actor` and `recorded_by` remain untrusted legacy descriptive fields.
  They have no authorization effect, but authenticated writers can still forge
  their displayed audit labels. They are not compared with usernames/display
  names: historical free text has no stable identity mapping, and changing it now
  would alter legacy persistence/idempotency semantics. These labels must not
  be treated as authenticated audit evidence. No TrustedWorkshopContext is
  constructed from browser input or from this gate.
- Later reviewed checkpoints: authorized user administration, controlled MFA
  recovery, first-run offline bootstrap tooling, notifications, key/backup
  management and deployment/resource hardening. The existing internal
  `provision_identity()` is not published through HTTP; operational bootstrap
  must be reviewed before production. IT_ADMIN has no automatic approval rights;
  OPENCLAW has no human session or user-administration rights.
- Session validation updates SQLite last-seen state in a short BEGIN IMMEDIATE
  transaction, including static requests. The existing five-second SQLite timeout
  remains; locked authentication storage fails closed with 503. Load limits,
  denial audit policy, proxy trust and Ubuntu hardware/TLS testing remain work.
- Revocation is checked on each request, not continuously within an already
  authorized in-flight business transaction. Atomic authorization/version checks
  for final approvals belong to the later approval bridge.

This checkpoint is a security boundary under test, **not production readiness**.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
git diff --check
```

Tests cover anonymous access before storage/body handling, expired/idle/revoked/
disabled/version-invalid/MFA-incomplete sessions, password-only challenges,
the complete role matrix, OPENCLAW isolation, exact Host/Origin checks, ignored
identity/forwarded headers, configuration failures, startup route inventory,
late-added routes, storage failures, absence of new privileged HTTP routes, and
existing API/database/browser/domain/pricing regressions.
