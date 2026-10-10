# Web 4E-3A: Checkpoint 2B-1 — isolated CSRF foundation

Implemented: validated CSRF signing-key configuration, context-bound proofs,
encrypted browser-context envelopes and reusable cookie helpers. **There are no
new HTTP endpoints, cookie-issuing handlers or CSRF enforcement in the access
gate.** The Checkpoint 1 route inventory, MFA requirement and role permissions
remain unchanged. This is not production readiness or a browser login feature.

## Operator-provisioned keys

`CARBIT_CSRF_KEYS` is a JSON array. Each entry has exactly two members, `id` and
`key`. The key is the canonical **padded URL-safe Base64 encoding of 32 random
bytes** (44 ASCII characters, one final `=`). Identifiers contain 1–32 ASCII
letters, digits, underscores or hyphens. `CARBIT_CSRF_ACTIVE_KEY_ID` must select
exactly one entry; list order does not select the signing key. There may be at
most two additional verification-only previous keys.

Missing configuration, unknown active IDs, duplicate IDs/material, duplicate
JSON members, invalid encoding/length and reuse of any configured TOTP key are
rejected at startup. Existing HTTPS, Host, TOTP and database settings are still
required. Secrets are wrapped in `SecretStr`, excluded from object repr and
omitted from configuration/validation errors. No production keys are generated
by the application. Startup validation cannot prove randomness or detect key
reuse in another system: independent secure provisioning is an operator duty.

Provision fresh 256-bit material with an OS cryptographic random generator in a
trusted offline maintenance workflow. Write it directly to a protected secret
store/configuration; do not print it, put it in command arguments, commit it or
copy it into chat/logs. Configure the service account to read it, restrict other
access, and include it in the approved encrypted backup/key-recovery procedure.
CSRF material must not be reused for TOTP, API credentials or other applications.
Deployment tooling and production secret storage remain deferred.

For planned rotation:

1. Provision a new independent key with a new ID. Distribute it to all instances
   for verification before switching signing (the bounded ring permits this).
2. Explicitly switch the active ID on every instance. Previous entries can
   verify/decrypt existing objects, but cannot issue new objects.
3. Keep the old key for at least 10 minutes after the **last old-key issuance on
   any instance**. No proof/context lifetime exceeds 10 minutes; clocks must be
   synchronized. Then remove the old entry. Never reuse an ID for new material.
4. For suspected compromise, remove the old key immediately. Its proofs and
   envelopes fail closed; pending authentication must restart. Session tokens
   are separate: removing a CSRF key does not revoke server-side sessions.

Restart/reconfiguration and the grace period are operator-controlled; no hidden
key generation, timed retirement or key-management integration is implemented.

## Proofs and encrypted envelopes

`CsrfProtector(settings, clock=...)` accepts validated settings and an injectable
timezone-aware server clock. Public proofs have this exact form:

```text
v1.<key-id>.<purpose>.<issued-at>.<expires-at>.<nonce>.<MAC>
```

HMAC-SHA-256 authenticates a canonical JSON array containing the protocol domain,
all six public fields, the exact configured target origin, and private bindings.
JSON arrays provide unambiguous field boundaries. A fresh 128-bit nonce allows
multiple independently valid proofs in the same context. MAC comparison uses
`hmac.compare_digest` on equal-length bytes. No private binding is included in
the returned proof. Versions, purposes, key IDs, ASCII timestamps, field count,
input lengths, Base64 pad bits and expiry are strictly checked. Binary fields
in proofs/envelopes use **unpadded** canonical URL-safe Base64.

| Purpose | Private binding | Deadline |
| --- | --- | --- |
| `preauth` | Secret from a verified encrypted anonymous context | At most 600 seconds and context expiry |
| `mfa` | Verified context secret and its encrypted current challenge token | At most 600 seconds, bounded by context and challenge expiry (challenge at most 300 seconds) |
| `session` | Validated server-side opaque session token | At most 600 seconds, bounded by server-supplied absolute and idle expiry |

There is no clock-skew grace: `issued_at <= now < expires_at`; expiry is exclusive.
Deadlines are UTC integer Unix seconds, rounded down. Invalid/missing contexts,
conflicting purpose arguments and lifetimes outside 1–600 seconds are rejected.
Proofs are reusable until individual expiry to support concurrent tabs. They
are not authentication grants, authorization decisions or one-time MFA proofs.

Anonymous and challenge cookies use this protected envelope format:

```text
e1.<key-id>.<purpose>.<issued-at>.<expires-at>.<nonce>.<ciphertext-and-tag>
```

Both envelopes use AES-256-GCM with fresh random 96-bit nonces and full 128-bit
tags. HKDF-SHA-256 derives separate purpose/key-ID encryption keys from CSRF
material, distinct from the HMAC signing key. Associated data binds the envelope
protocol, all public fields, exact origin and (for MFA) the originating context
secret. The pre-authentication plaintext is 32 random bytes; the MFA plaintext
is the canonical opaque challenge token. Neither is readable from the envelope.
This uses the existing `cryptography` library's
[authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/)
and [HKDF](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/).

`create_preauth()` creates a 600-second context; `open_preauth()` authenticates
and decrypts it. `seal_mfa()` requires that verified context and the actual
server challenge expiry, taking the minimum of that expiry, context expiry and
now + 300 seconds. `open_mfa()` requires the matching pre-authentication cookie.
Replacing that context invalidates the old MFA envelope/proof **when used with
the replacement cookie**. These envelopes are stateless: possession of both old
cookies can still pass their cryptographic checks until expiry. The later HTTP
layer must also verify the challenge's consumed/revoked/version state through
AuthService; encryption is not server-side challenge revocation.

`issue_proof()` and `verify_proof()` take a purpose, exact target origin and only
that purpose's bindings. Session calls require `session_token`,
`session_absolute_expiry` and `session_idle_expiry` from a validated server-side
session, never from request JSON or browser text. This module deliberately does
not query or update session storage. If an idle deadline moves forward later,
already issued proofs retain their original shorter deadline.

Protected outputs/internal decrypted values use secret wrappers and redacted
repr. The later adapter must never serialize decrypted contexts, challenge
tokens or session tokens into JSON, URLs, logs or browser-readable storage.

## Cookie helpers

`set_preauth_cookie`, `set_mfa_cookie` and `set_session_cookie` operate only on a
supplied response; no existing handler calls them. They issue respectively
`__Host-carbit-preauth`, `__Host-carbit-mfa-challenge` and `__Host-carbit-session`.
Every cookie is Secure, HttpOnly, SameSite=Strict, Path=/, with no Domain.
`Max-Age` and UTC `Expires` match the supplied deadline. Expired values and
overlong lifetimes are rejected.

Session issuance accepts a server-created `SessionGrant` and retains its original
absolute expiry (at most eight hours). Reissuing the same grant reduces Max-Age;
it never extends that deadline. The existing AuthService still enforces its
30-minute idle timeout; no remember-me or sliding absolute expiry exists.
`clear_auth_cookies()` deletes all three cookies with matching security flags,
path and host-only scope. When to issue/clear cookies, including safe handling
of concurrent responses and logout failures, belongs to Checkpoint 2B-2.

## Tests and remaining integration work

Focused deterministic-clock tests cover all purposes, malformed and valid-MAC
invalid timestamps, expiry boundaries, canonical encoding, field tampering,
origin/context/session/purpose replay, challenge substitution, replacement
contexts, encrypted payloads, key activation/verification/retirement, exact
cookie flags/expiry/clearing, missing configuration and redacted errors.
Existing test fixtures supply independent test-only CSRF keys; authentication
is never disabled and business access tests still require real MFA sessions.

Checkpoint 2B-2 must implement the separately reviewed HTTP transport: bootstrap,
Origin/Host/header/cookie cardinality checks, password/MFA state transitions,
server session inspection, CSRF on unsafe requests, controlled errors and rate
limits, fixation prevention, logout/revocation and cookie rotation. It must
distinguish unavailable storage from absent sessions and never bypass MFA.
The access gate currently permits absent Origin and performs no proof checks;
these helpers do not change that. Account administration, recovery, enrollment,
business identity attribution and approval-context bridging remain unavailable
or deferred to their approved checkpoints.
