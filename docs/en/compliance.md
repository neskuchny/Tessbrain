# Compliance

What the system provides for data-protection obligations, and what remains an
operator responsibility.

Full Russian original: [`../ru/COMPLIANCE.md`](../ru/COMPLIANCE.md).

---

## Data residency

Everything runs on infrastructure you choose. There is no hosted component
that must be used — the data stores are yours, and in
[air-gapped mode](production-hardening.md) the model runs inside your perimeter
too.

This is the point of the architectural bet in [Overview](overview.md): the
model is a consumable, the accumulated memory is the asset.

## Erasure

`backend/core/gdpr/` · [`../ru/GDPR_ERASURE.md`](../ru/GDPR_ERASURE.md)

Erasure covers the graph, vectors, relational records and derived snapshots.
The audit record *of the erasure* is retained deliberately — a deletion you
cannot prove happened is not much use in an audit.

## Retention

Retention cycles run on a schedule and can also be triggered by an
administrator (`POST /admin/retention/run`). Configure windows per data class;
see [Configuration](configuration.md).

## Consent for personal profiles

Employee profile sharing is consent-based and revocable, with each record
storing the version of the consent text it was granted under. Cross-company
exchange is behind a flag that is **off by default** and refuses at the gate
while off.

See [Access control](access-control.md).

## Audit trail

Tenant-scoped `audit_events`, protected by row-level security. Reading another
tenant's log requires `OWNER` and is itself audited.

## What remains yours

- **Classification.** Facts default to "internal"; raising the level is a human
  action. The system will not infer that something is confidential.
- **Lawful basis and retention periods.** The mechanism is provided; the policy
  is yours.
- **Processor agreements** with whichever model provider you configure — unless
  you run locally, in which case there is no third party in the path.
