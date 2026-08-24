# Overview

A company talks about itself every day — in meetings, calls, chats and
documents. Almost all of it evaporates. What survives lives in people's heads,
in message threads, and in files nobody will open again.

Tessbrain turns that stream into **memory with structure**, and turns that
memory back into **work**: it answers questions with a link to the original
source, computes figures in code rather than guessing them, drafts documents,
runs processes, and notices on its own when parts of the company have drifted
out of sync.

This is not note-taking, and it is not search over documents. It is a **model
of the company** — people, projects, clients, decisions, tasks and metrics,
and the relationships between them — that grows out of ordinary conversation
without anyone filling in forms.

---

## The loop, in four stages

Everything else in this documentation is detail inside one of these four
stages.

### 1. Intake

Material arrives from where it already is: meetings, calls, Telegram and
Slack, documents, links, CSV/Excel exports, CRM systems, 1C via OData, Google
Sheets, BI tools. Nothing has to be entered by hand — the system consumes what
is happening anyway.

### 2. Extraction

Roughly thirty specialised extractors each pull their own layer out of a
transcript: decisions, tasks, participants, KPIs, risks, ideas, opinions,
causal chains, contradictions, outcomes.

Every extracted fact carries three things a summary does not have: **a source,
a date, and a confidence**.

How many model calls this costs depends on the model class you choose. On a
strong model the whole base pass runs in a single call; on a cheaper one it is
split into several.

### 3. Memory

Facts become a **typed graph** — entities (people, projects, clients,
decisions, tasks, risks, goals) connected by typed relationships, rather than a
pile of text chunks.

Two properties matter more than the counts:

- **Facts are versioned, not overwritten.** A new value does not erase the old
  one — it marks it superseded, with the date and the source meeting. You can
  ask "what is it now" and "what was it, when did it change, and why".
- **The system tidies up on its own overnight.** Duplicates are merged,
  unconfirmed material decays, insights are collected.

### 4. Work

Memory comes back as output: chat over company memory, generated documents,
process boards, reports, simulations, controlled feeds to external partners,
and a connection into Claude Code or Cursor as a context source.

---

## The architectural bet

> **The model is a consumable. The asset is what the company has accumulated.**

Company memory, playbooks, data ontology, skills and configured processes are
not tied to any single AI vendor. The model underneath can be swapped — for a
different provider, for your own servers, for a local model inside a closed
perimeter — and everything accumulated stays entirely yours.

This is a direct answer to the main fear in adopting a system like this: *"we
will invest, and in a year the vendor raises the price or shuts down."* The
performer changes; the asset does not.

The opposite arrangement — where company knowledge lives inside somebody
else's assistant — means that when the vendor leaves, the accumulated
knowledge leaves with them.

---

## Why relationships, not just similarity

Semantic search works by turning text into a vector, turning the question into
a vector, and returning the nearest matches. This is excellent when the answer
sits in one place.

It has a **proven mathematical ceiling** when the answer does not. The number
of document combinations a single vector can return is bounded by its
dimensionality (work from Google DeepMind, ICLR 2026). No amount of better
data or a larger model removes that bound.

The clearest case is an enumerative question — *"who worked on both Alpha and
Beta?"* Similarity gives you things that look relevant; the question demands
**completeness**. So retrieval here runs three channels at once — meaning,
exact terms, and graph relationships — and enumerative questions weight the
third channel higher.

This is measured, not argued. See [Benchmarks](benchmarks.md).

---

## Boundaries enforced before the model

Access control is not an interface filter. Material a person may not see is
removed **before it reaches the model**, which is why the model cannot let
something slip that it never received.

That is a stronger guarantee than instructing an assistant not to mention
salaries.

See [Access control](access-control.md).

---

## What this is not

Stating this plainly is part of the product, not fine print.

- **It is not a transcription service.** Recording and speech-to-text happen
  upstream; this system consumes the finished material and makes sense of it.
- **It does not classify sensitivity for you.** Every fact starts at
  "internal". Raising a classification is a human action. A scenario like
  "the acquisition discussion will not surface for an intern" requires that
  somebody marked that discussion.
- **It does not negotiate between agents.** An external executor takes a task
  and returns a result under machine-checked acceptance. That is
  request/response, not a dialogue about how to do the work.
- **Company experience accumulates from the day you switch it on.** Outcomes
  cannot be reconstructed retroactively; for an archive already loaded, that
  layer appears only on reprocessing.

More, in detail: [What we actually tested](what-we-tested.md).

---

## Where to go next

- Want to run it? → [Quickstart](quickstart.md)
- Want to understand the internals? → [Architecture](architecture.md)
- Evaluating it? → [Benchmarks](benchmarks.md) and
  [What we actually tested](what-we-tested.md)
- Connecting an assistant? → [MCP server](mcp.md)
