# API reference

All routes are under `/api/v1`. Interactive schema is served at `/docs` when
the server is running — that is authoritative; this page is orientation.

Full Russian original: [`../ru/API_REFERENCE.md`](../ru/API_REFERENCE.md).

---

## Authentication

Send a bearer token:

```
Authorization: Bearer <token>
```

Two kinds are accepted — a user JWT (from `/auth/login` or your identity
provider) and a service JWT for internal callers. Identity comes from the
verified claim, **not** from a `user_id` query parameter: when a valid token is
present, its identity overrides anything in the query string.

## Health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/ping` | liveness |
| `GET` | `/health` | every dependency, reported separately |
| `GET` | `/info` | build and system information |

## Ingestion

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/analyze/ingest/meeting` | feed a transcript in |
| `POST` | `/analyze/deep-analyze` | deep pass over accumulated material |
| `GET` | `/analyze/meetings` | list meetings |

Use an `Idempotency-Key` header on ingestion. A retry with the same key returns
the cached result; the same key with a different body returns 409.

## Memory

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/entities` | list entities |
| `GET` | `/entities/{id}` | entity detail |
| `GET` | `/entities/{id}/graph` | subgraph around an entity |
| `POST` | `/entities/search` | search entities |
| `GET` | `/snapshots/project/{id}` | project snapshot |
| `GET` | `/snapshots/person/{id}` | person snapshot |
| `GET` | `/snapshots/company` | company snapshot |

## Chat

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat/completions` | ask company memory |
| `POST` | `/chat/v1/chat/completions` | OpenAI-compatible shape |

Retrieval effort is selectable via `strategy`:

| Strategy | Behaviour |
|---|---|
| `auto` | router picks (default) |
| `instant` | answer from snapshots, no model call |
| `quick` | fast retrieval |
| `standard` | full hybrid retrieval |
| `deep` | multi-step with reflection |

## Data bus

Controlled external access by API key rather than login — see
[Data bus](data-bus.md).

| Method | Path | Purpose |
|---|---|---|
| `GET`/`POST` | `/data-bus/query` | query by consumer key |
| `GET` | `/data-bus/snapshot/{type}/{id}` | entity snapshot through the bus |
| `GET` | `/data-bus/health` | bus health |
| `POST` | `/data-bus/admin/consumers` | create a consumer (admin) |
| `POST` | `/data-bus/admin/consumers/{id}/rotate-key` | rotate a key |
| `GET` | `/data-bus/admin/audit` | audit log |

## Administrative

Require a role of `ADMIN` or higher, taken **only** from a signature-verified
token.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/audit-log` | audit events; another tenant requires `OWNER` |
| `POST` | `/admin/retention/run` | run a retention cycle |

## Error shape

Errors return a JSON body with `status_code` and `detail`. Rate limiting and
quota exhaustion return `429` with a `Retry-After` header. Internal errors
return a generic message — exception text is not sent to clients, because it
reveals internal structure.
