# Architecture

This page maps the four-stage loop from [Overview](overview.md) onto actual
code, so you can find the thing you want to change.

Paths are relative to the repository root (`tessent_brain/`).

---

## Stack

| Layer | Choice |
|---|---|
| API framework | LiteStar, served by Granian (ASGI) |
| Background work | Taskiq + Redis |
| Relational store | PostgreSQL (TimescaleDB for time series) |
| Graph store | Neo4j, with an in-process NetworkX fallback |
| Vector store | Qdrant |
| Cache / counters | Redis |
| Language | Python 3.11+ |
| Web UI | Next.js (React) |

The Neo4j fallback is real, not aspirational: `GraphBuilder` selects NetworkX
automatically when Neo4j is unreachable (`backend/core/store/graph_builder.py`).
That makes a first run possible without standing up a graph database.

---

## Stage 1 — Intake

**Where:** `backend/core/capture/`, `backend/core/integrations/`,
`backend/api/routes/analyze.py`

Material enters through several doors:

- **Meetings** — transcripts arrive already recorded and transcribed from an
  upstream system. This service consumes finished material; it does not do
  speech-to-text.
- **Messengers** — Telegram and Slack (`backend/core/messengers/`).
- **Documents and links** — files and URLs are absorbed into memory on the
  same footing as meetings (`backend/core/documents/`).
- **Structured data** — CRM systems, 1C via OData, Google Sheets, BI exports
  (`backend/core/integrations/`, `backend/core/ontology/`).
- **External parties** — partners can push facts in over the data bus, and the
  server, not the sender, stamps the provenance
  (`backend/core/data_bus/`).

### Idempotency

Retries are expected — a network blip on a meeting ingest must not double the
extraction cost. An `Idempotency-Key` header makes a non-GET request replayable:
the first call is cached, an identical retry returns the cached response, and a
retry with the *same key but a different body* returns 409.

`backend/api/middleware/idempotency.py`

---

## Stage 2 — Extraction

**Where:** `backend/core/capture/agents/` — 34 modules

Each extractor pulls one layer out of a transcript: decisions, tasks,
participants, KPIs, risks, ideas, opinions, causal chains, contradictions,
outcomes. They are deliberately separate: a single "summarise this" prompt
produces prose, while separate extractors produce *typed facts*.

Every fact carries **source, date and confidence**. That triple is what makes
an answer defensible later — it is the difference between "the budget is 300k"
and "the budget is 300k, said on 11 March, confidence medium because the
figure was approximate".

### Model routing

Cost and quality are traded per workload, not globally
(`backend/core/llm/router.py`):

- **Heavy and rare** — meeting extraction, nightly consolidation, spec
  generation, final synthesis of a deep search → strong model.
- **Frequent and interactive** — chat, ordinary retrieval, intermediate steps
  → cheaper, faster model.

Provider and key are chosen by the operator; the tier for a given task is
chosen by the system.

Spend is metered and capped (`backend/core/llm/quota.py`), including an atomic
reservation that prevents a burst of parallel calls from all passing the same
check before any of them has recorded its cost
(`backend/core/llm/quota_reservation.py`).

---

## Stage 3 — Memory

**Where:** `backend/core/store/`, `backend/core/temporal/`,
`backend/core/sleep/`

The graph is typed: entities such as Person, Project, Client, Task, Decision,
Risk, Goal, Process and Department, connected by typed relationships such as
`ASSIGNED_TO`, `DECIDED` and `RELATES_TO`. See [Data model](data-model.md).

Three properties define how it behaves over time:

### Facts are versioned, not overwritten

A new value marks the previous one superseded, with the date and source
meeting. Versions are dated by **meeting date, not ingest date**, so an
archive loaded in a single day does not collapse into one point in time.

`backend/core/temporal/`

### Decay is typed

Memory that never forgets bankrupts its owner — what kills a long-lived system
is not its starting size but its growth slope. So contextual edges
("mentioned near each other") weaken over time, while **structural facts —
decided, assigned, changed role — never decay**. Untyped forgetting would eat
the org chart and the decision history.

`backend/core/sleep/`, `backend/core/corrections/`

### The system tidies up overnight

Duplicates merge, unconfirmed material weakens, insights are collected. This is
scheduled work, not a request-time cost.

`backend/core/automations/scheduler.py`,
`backend/core/corrections/nightly_consolidation.py`

---

## Stage 4 — Retrieval and output

**Where:** `backend/core/search/`

Retrieval runs **three channels at once** and fuses them:

| Channel | Module | Good at |
|---|---|---|
| Semantic | Qdrant vectors | "something like this" |
| Lexical | `bm25_searcher.py` | exact terms, names, identifiers |
| Graph | `graph_trace_builder.py` | relationships, multi-hop, completeness |

Fusion is Reciprocal Rank Fusion (`rrf_fusion.py`). An enumerative question —
"list everyone who…", "how many in total…" — is detected
(`enumerative_detect.py`) and weights the graph channel higher, because that
class of question needs *completeness*, which similarity cannot provide.

`hybrid_search_orchestrator.py` is the entry point;
`search_mode_router.py` picks how much work to spend
(instant / quick / standard / deep).

### Output surfaces

- Chat over company memory — `backend/api/routes/chat.py`
- Generated documents — `backend/core/documents/`
- Process boards and automations — `backend/core/automations/`, `board/`
- Reports and simulations — `backend/core/reports/`, `sima/`
- Controlled external feeds — `backend/core/data_bus/`
- MCP tools for external assistants — `mcp_server.py` ([MCP](mcp.md))

---

## Cross-cutting: the access boundary

Access control is not applied to the answer. It is applied to the **material**,
before that material reaches the model.

`backend/core/access/`, `backend/core/access_control.py`

The perimeter — whether a request may leave the network at all — has a single
definition used by the model router, the link fetcher and web search alike, so
a closed deployment cannot leak through a component that forgot about it.

`backend/core/security/perimeter.py`

See [Access control](access-control.md) and [Security](security.md).

---

## Request lifecycle

An HTTP request passes through the middleware stack in this order
(`backend/api/app.py`):

1. `ExceptionLoggerMiddleware` — diagnostic; logs only outside production
2. `MetricsMiddleware` — request metrics
3. `LocaleResolverMiddleware` — response language
4. `TenantResolverMiddleware` — establishes the tenant context
5. `UserIdEnforcerMiddleware` — a valid token's user id overrides any
   `?user_id=` in the query, so a stale client id cannot read somebody else's
   data
6. `RateLimitMiddleware` — per-user, per-IP and per-tenant sliding windows
7. `IdempotencyMiddleware` — replay protection for non-GET
8. Route handler

`backend/api/middleware/`

---

## Where to go next

- The types themselves → [Data model](data-model.md)
- Running it for real → [Deployment](deployment.md)
- What is actually verified → [Benchmarks](benchmarks.md)
