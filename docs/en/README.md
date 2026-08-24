# Tessbrain — Documentation

Tessbrain turns what a company already says — meetings, calls, chats,
documents — into a queryable memory with sources, dates and confidence, and
feeds that memory back as work.

This is the English documentation set. Russian originals live in
[`../ru/`](../ru/).

> **Status.** This documentation describes what runs in code. Features behind
> flags, and things that are planned but not built, are called out explicitly
> where they appear. A product that promises more than it does loses trust at
> the first demo.

---

## Start here

| Document | What it covers |
|---|---|
| [Overview](overview.md) | What the system is, the four-stage loop, what it is not |
| [Capability map](product-capabilities.md) | All 22 sections in business terms, with honest limits |
| [Quickstart](quickstart.md) | Run it locally — infrastructure, dependencies, first request |
| [Architecture](architecture.md) | How the pieces fit: ingestion, graph, retrieval, output |

## Building on it

| Document | What it covers |
|---|---|
| [Data model](data-model.md) | Entity and relationship types, versioning, typed decay |
| [API reference](api-reference.md) | REST endpoints, authentication, request shapes |
| [MCP server](mcp.md) | Connect Claude, Cursor and other assistants — 23 tools |
| [Data bus](data-bus.md) | Controlled external access: API keys, policies, redaction, audit |
| [Integrations](integrations.md) | Where the material comes in from and where results go |

## Running it

| Document | What it covers |
|---|---|
| [Deployment](deployment.md) | Production install, from server prep to smoke test |
| [Configuration](configuration.md) | Environment variables, model providers, keys |
| [Production hardening](production-hardening.md) | What to turn on before real traffic |
| [Migrations](migrations.md) | Schema changes and how to apply them |

## Trust and boundaries

| Document | What it covers |
|---|---|
| [Security](security.md) | Authentication, roles, air-gap enforcement, reporting issues |
| [Access control](access-control.md) | Who sees what, and why it is enforced before the model |
| [Compliance](compliance.md) | Data residency, retention, GDPR erasure |
| [Multi-tenancy](multi-tenancy.md) | Personal spaces, organizations, isolation |

## Evidence

| Document | What it covers |
|---|---|
| [Benchmarks](benchmarks.md) | Measured results, the harnesses used, and sample sizes |
| [What we actually tested](what-we-tested.md) | The honest boundary between measured and assumed |

---

## Shareable pages

Four self-contained HTML pages live in [`../share/en/`](../share/en/) — a
capability map, an explainer with live interactive examples, a moving loop
diagram and a cost calculator. They open with a double click, need no internet,
and can be mailed or dropped in a shared folder. The Russian set is in
[`../share/ru/`](../share/ru/).

---

## A note on honesty in these docs

Two conventions run through everything here, and they are worth stating once:

**Measured is separated from estimated.** Where a number came from a run, the
run is named and its output is in the repository next to the code that produced
it. Where a number is an assumption, it says so. No measured figure is derived
from an estimated one.

**Gaps are documented, not hidden.** Sections titled "Limits" or "Not built"
are part of the documentation, not a footnote. If you are evaluating this
system, those sections are the ones worth reading first.
