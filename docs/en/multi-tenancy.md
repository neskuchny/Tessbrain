# Multi-tenancy

Two kinds of space exist side by side: a **personal** one and an
**organisation** one.

`backend/core/store/tenant_paths.py` ·
`backend/api/middleware/tenant_resolver.py`

---

## Isolation

Separation happens at the storage-path and schema level, not by filtering
query results — a missed filter is a leak, a missing path is an absence.

The tenant is resolved **per request, before any route runs**, from the
verified token and the `X-Tenant-Id` header.

`STRICT_MULTITENANT` makes a request without a resolvable tenant fail with 400
rather than fall back to a default. Turn it on for SaaS; leave it off for a
single-tenant install.

## Identity cannot be spoofed by query parameter

When a request carries a valid token, that token's user id **overwrites** any
`user_id` in the query string before routing
(`UserIdEnforcerMiddleware`).

This exists because of a real failure mode: after switching accounts, a browser
keeps sending the previous identifier. Chasing that through every component is
whack-a-mole; overriding it in one place is not.

Administrative prefixes (`admin`, `orgs`, `scim`, `gdpr`) are exempt, because
there `user_id` is a filter operating under its own role check rather than a
claim of identity.

## Organisations

Membership, roles and org metadata can be stored in files (single node,
default) or in PostgreSQL (multi-node, transactional) — `ORG_STORE_BACKEND`.

A personal brain and a company brain coexist: a person keeps their own space,
and the organisation has a shared one, with sharing between colleagues governed
by [access control](access-control.md).

## Row-level security

PostgreSQL RLS carries tenant scoping for audit and other shared tables. See
[`../ru/RLS_ROLLOUT_RUNBOOK.md`](../ru/RLS_ROLLOUT_RUNBOOK.md) for the rollout
procedure.
