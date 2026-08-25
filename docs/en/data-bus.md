# Data bus

Controlled access to company memory for parties that should not have a login:
partner systems, external agents, downstream services.

`backend/core/data_bus/` · `backend/api/routes/data_bus.py`

---

## The shape of it

A consumer gets an **API key**, not an account. Attached to that key:

| Control | What it does |
|---|---|
| Policy | which slice of memory is reachable |
| Redaction | strips fields before they leave |
| Rate limiting | per-consumer, enforced in Redis |
| Audit | every query recorded |

Retrieval behind the bus is the same hybrid pipeline used internally — semantic,
lexical and graph channels fused with RRF — with a `search_depth` control
(`quick` / `standard` / `deep`).

## Querying

```bash
curl -H "X-API-Key: <consumer key>" \
  "https://your-host/api/v1/data-bus/query?query=status+of+project+Alpha"
```

`POST` accepts the same thing in a body for longer or structured queries.

## Administration

Consumers are created, deactivated and rotated through admin routes that
require `ADMIN` (see [API reference](api-reference.md)). Key rotation does not
interrupt an active consumer — issue, switch, then deactivate the old key.

## Inbound facts

External parties can also **push facts in**. Provenance is stamped by the
server, not taken from the sender, so an external party cannot claim its data
came from somewhere more trusted than itself.

## Why not just give them a login

A login carries a person's whole access surface and changes as their role
changes. A bus key carries exactly one policy, is independently revocable,
independently rate-limited, and leaves an audit trail that is about the
integration rather than about a person.

## Where it leads

This page is the operational reference. What the bus is *for* — external
agent chains orchestrated by the brain, the brain as a source for other
systems, a network of company brains with new HR processes at the
intersection — with the honest status of each mechanic, lives in
[data-bus-vision.md](data-bus-vision.md).
