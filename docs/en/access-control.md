# Access control

The governing idea, stated once:

> Material a person may not see is removed **before it reaches the model**, not
> filtered out of the answer afterwards.

A model cannot disclose what it never received. That is a structurally
different guarantee from instructing an assistant not to mention salaries, and
it is the reason access control here sits in the retrieval path rather than in
the presentation layer.

---

## System roles

`backend/core/auth/rbac.py`

| Role | Value | Typical use |
|---|---|---|
| `NONE` | 0 | Unverified or unknown |
| `VIEWER` | 10 | Read-only |
| `EDITOR` | 20 | Creates, does not delete |
| `ADMIN` | 30 | Administrative endpoints |
| `OWNER` | 40 | Cross-tenant compliance reads |

Roles are an `IntEnum`, so a check reads as "at least this role"
(`current_role() >= Role.EDITOR`).

Anything unrecognised parses to `NONE` — fail-closed, because granting a role
by accident is worse than refusing one.

### Where a role may come from

**Only from a token whose signature was verified.** An unsigned token gets
`Role.NONE` regardless of what its payload claims.

This is enforced in one place, `backend/core/auth/admin_guard.py`, precisely so
that a new administrative route cannot reintroduce the hole by copying an older
pattern. See [Security](security.md).

---

## Per-fact classification

Every fact carries a classification and group membership. The default is
**internal** — nothing is public unless someone made it so.

> **Honest limit, repeated from the overview because it matters here:**
> classification is not assigned automatically. Raising a fact above "internal"
> is a human action. A scenario like "the acquisition discussion will not
> surface for an intern" requires that somebody marked that discussion.

---

## The retrieval boundary

When a question is answered, the pipeline is:

1. Resolve the trusted identity and tenant of the caller.
2. Retrieve candidate material.
3. **Cut everything the caller may not see.**
4. Assemble the model context from what survives.
5. Generate.

Step 3 happens before step 4 — that ordering is the whole design.

`backend/core/access/`, `backend/core/access_control.py`

---

## Tenancy

Personal spaces and organisation spaces are separated at the storage-path level
(`backend/core/store/tenant_paths.py`), and the tenant is resolved per request
by middleware before any route runs.

`user_id` in a query string is not trusted when a valid token is present: the
token's identity overrides it (`UserIdEnforcerMiddleware`). This closes the
case where a browser keeps sending a stale identifier after an account switch.

See [Multi-tenancy](multi-tenancy.md).

---

## Consent for personal profiles

Employee profiles ("digital twins") are governed by explicit consent, held in a
personal account area: who may see it, which slice, until when — revocable in
one click, with each record storing the version of the consent text it was
granted under.

Cross-company profile exchange is behind `ENABLE_PROFILE_EXCHANGE`, **off by
default**. With the flag off, the gate refuses and nothing leaves — the
exchange becomes a one-line change once a legal model is settled, and until
then it cannot happen by accident.

`backend/core/consent/`, `backend/core/twin/`

---

## External access

Partners and external systems do not get a login. They get a scoped key over
the data bus, with a policy, redaction, rate limiting and an audit trail.

See [Data bus](data-bus.md).

---

## Auditing

Administrative and data-affecting actions land in a tenant-scoped
`audit_events` table protected by row-level security.

Reading another tenant's audit log requires `OWNER` — and that read is itself
recorded.

`backend/core/observability/audit_log.py`
