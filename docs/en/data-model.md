# Data model

Memory is a **typed graph**, not a pile of text chunks. That choice is what
lets the system answer "who worked on both projects" rather than "here are five
passages that mention projects".

---

## Entities

Entity labels seen across the graph layer include:

`Person` · `Client` · `Company` · `Organization` · `Department` · `Team` ·
`Project` · `Product` · `Feature` · `Task` · `Process` · `Decision` · `Risk` ·
`Goal` · `Resource` · `Technology` · `Document` · `Event` · `Idea` · `Concept`

> **A note on counts.** Product material quotes a specific number of entity and
> relationship types. There is no single canonical list in code — different
> layers declare the subset they work with (`entity_agent.py`,
> `graph_export.py`, `insight/communities.py`). The list above is the union of
> what those declarations contain. If you need an exact, enforced schema for
> your own integration, read those modules rather than trusting a headline
> figure.

## Relationships

Relationships are typed, and the type carries meaning the system depends on.
Examples that appear directly in the graph layer:

| Relationship | Meaning |
|---|---|
| `ASSIGNED_TO` | task → responsible person |
| `DECIDED` | decision → who made it / where |
| `RELATES_TO` | generic association, the weakest form |

The distinction between *structural* relationships (`DECIDED`, `ASSIGNED_TO`)
and *contextual* ones matters for decay — see below.

`backend/core/store/graph_builder.py`

---

## Every fact carries provenance

Three attributes travel with an extracted fact, and they are the reason an
answer can be defended rather than merely believed:

| Attribute | Why it exists |
|---|---|
| **Source** | which meeting or document it came from |
| **Date** | when it was said — not when it was loaded |
| **Confidence** | how firm the extraction was |

"The budget is 300,000" is an assertion. "The budget is 300,000, stated on
11 March, confidence medium because the figure was given approximately" is
something you can take to a board meeting.

---

## Versioning: facts are not overwritten

A new value does not erase the previous one. It marks it **superseded**, with
the date and the source.

That makes two different questions answerable:

- *What is it now?* → current version
- *What was it, when did it change, and why?* → the chain

Numeric values are normalised, so "2M ₽" from one meeting and "2 500 000 rub"
from another are comparable — growth shows as a number rather than an
impression.

### Dated by meeting date, not ingest date

An archive loaded in a single day does not collapse into one point in time.
This is what makes "how did the company look on 15 March" a real query rather
than an approximation.

`backend/core/temporal/`

---

## Typed decay

Memory that never forgets bankrupts its owner. What kills a long-lived system
is not its starting size but its **growth slope** — so the system measures its
own volume and rate of growth, not just its size.

Forgetting is **typed**:

- **Contextual edges decay.** "These two were mentioned near each other" weakens
  over time, because co-mention is weak evidence that ages badly.
- **Structural facts never decay.** `DECIDED`, `ASSIGNED_TO`, role changes —
  these are the org chart and the decision history. Untyped forgetting would
  eat them.

This distinction is the difference between a memory that gets cleaner over time
and one that quietly loses its backbone.

`backend/core/sleep/`, `backend/core/corrections/`

---

## Confidence comes from outcomes, not repetition

Worth stating because it is counter-intuitive and easy to get wrong.

Ten discussions of a topic whose outcome nobody knows produce **low**
confidence, not high. Otherwise the number would mean "we talked about this a
lot" while reading as "we verified this".

Confidence rises from **confirmed outcomes** and decays with time.

`backend/core/wisdom/`

---

## Storage layout

| Store | Holds |
|---|---|
| Neo4j (or NetworkX fallback) | the graph itself |
| PostgreSQL | relational records, audit, versions, billing |
| Qdrant | embeddings for the semantic channel |
| Redis | counters, rate limits, idempotency, quota reservations |

Per-tenant separation happens at the path and schema level
(`backend/core/store/tenant_paths.py`). See
[Multi-tenancy](multi-tenancy.md).
