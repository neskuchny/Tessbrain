# Security

This page describes what the system enforces, what it leaves to the operator,
and where the honest gaps are.

To report a vulnerability, see [`SECURITY.md`](../../../SECURITY.md) in the
repository root.

---

## Identity

### Tokens

Two kinds of bearer token are accepted:

- **User JWT** — issued by `/auth/login`, or a Supabase-issued token. Identity
  comes from the verified `sub` claim.
- **Service JWT** — issued by trusted internal services, carrying a `user_id`
  claim, checked for signature, audience, issuer and expiry.

Verification is HMAC-SHA256 only. `alg` is pinned, so an `alg: none` or
algorithm-substitution token is rejected outright
(`backend/core/auth/service_token.py`).

### A role is never taken from an unverified token

This is worth stating on its own, because it is the rule that governs every
administrative endpoint:

> **Identity may, in compatibility mode, come from an unverified token.
> A role may not — ever.**

An unsigned token gets `Role.NONE` and hits a 403. Otherwise anyone could
assemble three base64 segments containing `{"sub": "anyone", "role": "owner"}`
and read administrative data.

The rule lives in one place — `backend/core/auth/admin_guard.py` — so that a
new admin route cannot start from a copy of a leaky version.

### Production refuses to start without a verifiable secret

Strict authentication disables *itself* when there is no key to verify
signatures with — otherwise it would reject every real token and take the UI
down. That is reasonable, but it is silent: a deployment without a secret
quietly slides into a mode where identity is not checked.

So the decision is moved to startup. In production, `Settings` will not load
without a JWT secret, and a placeholder value copied from `env.example` does
not count as one (`backend/config.py`).

Development and staging are unaffected.

---

## Brute force and credential stuffing

Two independent mechanisms, because either alone is insufficient:

**Per-address rate limiting.** Sliding windows per user, per IP and per
tenant (`backend/api/middleware/rate_limit.py`). Defaults:

| Bucket | Requests / minute |
|---|---|
| Authenticated user | 600 |
| Anonymous (per IP) | 120 |
| Tenant | 5000 |

**Per-account throttling.** Address limits do not help when addresses are
cheap — proxies, mobile networks, botnets. So failed logins are counted
against the account: 10 failures in 15 minutes holds further attempts
(`backend/core/auth/login_guard.py`, configurable, `0` disables).

The email is never stored in the key — only a SHA-256 fingerprint — so neither
logs nor Redis reveal who has an account.

> **The deliberate trade-off:** any per-account counter hands an attacker a
> lever — hammer somebody's address with wrong passwords to lock them out. So
> there is **no lockout**, only a temporary hold with a short window and a
> generous threshold, and it clears itself. Permanent locks are never set.

### Client address determination

The rate limit bucket is only as trustworthy as the address it keys on.

`X-Forwarded-For` is written by the client. A reverse proxy typically
*appends* rather than replaces, so a forged entry stays leftmost — which means
reading the first entry lets an attacker land in a fresh bucket on every
request.

The address is therefore read **from the right**: the trailing entries are the
ones your proxies appended, and a client cannot write there. How many entries
are yours comes from `TRUSTED_PROXY_HOPS`; with no setting, it is inferred
from the peer address — a private-network peer means one proxy in front, a
public peer means the header is not trusted at all.

The shipped Caddy configuration also sets
`header_up X-Forwarded-For {remote_host}`, so the proxy overwrites rather than
appends.

---

## The perimeter

"Internal" has **one definition for the whole product**
(`backend/core/security/perimeter.py`), used by the model router, the link
fetcher, web search and the multimodal endpoint alike.

It fails closed: no resolvable address means "outside".

In enterprise/air-gapped mode the check runs at two moments — at boot
(a settings validator refuses to start with a cloud provider configured) and
at call time (before each request leaves). One rung alone is not enough:
model profiles and per-user keys live in the database and can change after
boot.

---

## Access control

Material a person may not see is removed **before it reaches the model** —
not filtered out of the answer afterwards. A model cannot disclose what it
never received.

Detail: [Access control](access-control.md).

---

## Input handling

**Stored XSS.** Content arriving from meetings is written by people, so it is
treated as untrusted throughout. Text is escaped before markup on the server
(`backend/core/documents/document_writer_agent.py`), and rendered HTML is
sanitised client-side against an allow-list built on `DOMParser`
(`frontend/lib/sanitizeHtml.ts`) — allow-list rather than blocklist, because
blocklists are routinely bypassed.

**Uploads.** Both the stored content type and the on-disk extension come from
a server-side allow-list, never from the uploading client, and files land in a
per-owner directory rather than one shared pool.

**SQL and Cypher.** Values are always bound. The few places that build a
fragment dynamically interpolate only from fixed whitelists.

---

## Auditing

Administrative and data-affecting actions are written to a tenant-scoped
`audit_events` table with RLS (`backend/core/observability/audit_log.py`).
Reading another tenant's audit log requires `Role.OWNER` and is itself
audited.

---

## Supply chain

Run on pull request and nightly (`.github/workflows/security.yml`):

- `pip-audit` and `npm audit`
- Trivy filesystem scan
- `gitleaks` secret scan over full history
- CodeQL
- SBOM (SPDX) as a build artifact

Unfixed vulnerabilities in Python dependencies fail the build.

> **Known gap:** requirements are pinned with `>=`, not `==`, and there is no
> Python lockfile. The audited version is therefore not guaranteed to be the
> installed one.

---

## What is left to the operator

These are off or permissive by default so local development is not painful.
Turn them on before real traffic — see
[Production hardening](production-hardening.md).

| Setting | Default | Why it matters |
|---|---|---|
| `STRICT_USER_AUTH` | off | Rejects token-less requests carrying `?user_id=` on per-user routes |
| `TRUSTED_PROXY_HOPS` | inferred | Set explicitly if your topology is unusual |
| `TESSENT_QUOTA_STRICT` | off | Blocks spend when usage cannot be metered, instead of allowing it |
| CORS origins | localhost | Must be an explicit allow-list; `*` with credentials is refused |

---

## Honest limits

- **Sensitivity is not classified automatically.** Every fact starts at
  "internal"; raising it is a human action.
- **Quota checks and spend recording are separate moments.** An atomic
  reservation closes the gap, but it reserves an *estimate* — the exact cost
  is known only after the call.
- **The closed-perimeter quality cost has not been measured.** All published
  numbers come from cloud models. The harness for measuring on a local model
  exists; the measurement has not been run.
