# Tessbrain

**The company brain: meetings, calls and documents become a memory that answers
with sources — and works.**

🇷🇺 [Русская версия](README.ru.md) · 🌐 [tessent.ai](https://tessent.ai/en) · 🏢 [synlabs.pro](https://synlabs.pro/)

[![Watch the demo](https://img.youtube.com/vi/6LE8CYxQH7w/maxresdefault.jpg)](https://youtu.be/6LE8CYxQH7w)

*▶ 3-minute demo on YouTube*

---

## What it does

A company talks about itself all day — in meetings, calls, chats, documents —
and almost all of it evaporates. Decisions get made twice. Sales commits to
October while engineering plans for December, and nobody notices until it's
expensive. Tessbrain turns that stream into a typed knowledge graph where
every fact carries its **source, date and confidence**, and then puts it to
work:

- **Answers questions with citations.** "What did we promise the bank on
  timing?" → two conflicting dates from two different meetings, with links to
  both.
- **Drafts documents from memory.** Proposals and reports assembled from what
  was already said; what the system genuinely doesn't know is marked in red —
  not invented.
- **Flags divergence on its own.** When two teams have quietly committed to
  different dates or numbers, the executive view shows it before it gets
  expensive.
- **Enforces access *before* the model.** A restricted fact is removed from
  the context before the answer is synthesized — the model never saw what
  your role can't see. Ask about salaries as an intern, and the fact doesn't
  exist. Not "access denied" — invisible.
- **Simulates a real client** from the graph, runs process boards, tracks
  team dynamics from meeting behaviour — the full map is 22 sections long.

**Full capability map:** [docs/en/product-capabilities.md](docs/en/product-capabilities.md)
· visual versions: [capability map](https://neskuchny.github.io/Tessbrain/share/en/tessent_capabilities.html)
· [system diagram](https://neskuchny.github.io/Tessbrain/share/en/tessent_map.html)
· [what Tessbrain is (article)](https://neskuchny.github.io/Tessbrain/share/en/tessent_article.html)
· [cost calculator](https://neskuchny.github.io/Tessbrain/share/en/tessent_cost.html)

---

## Try it without your own data

The repo ships with **Gelion**, a synthetic 85-person company: 20 meetings
with real transcripts, 14 planted storylines and a machine-checked ground
truth. Ingest it and ask "what did we promise Titan Bank on timing?" — then
watch the system surface the conflict, with sources.

→ [demo/gelion](demo/gelion/) (validator included: 20/20 green)

---

## Deployment: three tiers

Same product, three ways to run it — pick by company size.

| | Tier 1 · Zero infrastructure | Tier 2 · Single VPS | Tier 3 · Holding |
|---|---|---|---|
| for | up to ~20 people, trying it out | up to ~50 people | corporation, multiple entities |
| external services | **none** — graph and vectors in local files, local embedding model | Postgres, Redis, Qdrant, Neo4j in containers | managed databases |
| isolation | single org | RLS optional | RLS + holdings console + federation |

→ [deploy/README.md](deploy/README.md) for the full guide, including the
honest limits of each tier (tier 1 today runs the memory/search engine over
data you load yourself; login and meeting ingestion still require a
Supabase-compatible stack — documented, not hidden).

---

## Honest numbers

We publish benchmark results **including the ones we lose**. On
[BrainBench](https://github.com/garrytan/gbrain-evals) — someone else's
corpus, someone else's queries, run in *their* runner:

| | P@5 | R@5 |
|---|---|---|
| our BM25 alone (control) | 0.161 | 0.597 |
| **Tessbrain, default config** | **0.316** | **0.900** |
| Tessbrain, `CONFIDENT_GRAPH=on` (ships off) | 0.729 | 0.991 |
| gbrain, as published by them | 0.491 | 0.979 |

On defaults we are **behind** their graph system and ahead of every
non-graph baseline they publish. The flag that puts us ahead ships **off**,
because all 145 questions in that suite have the same shape, and a number we
won't serve by default has no business in a headline. The full story —
including how we first published an inflated number, found the fitted rules
in our own benchmark script, and corrected it — is in
[docs/en/benchmarks.md](docs/en/benchmarks.md).

Also measured: LongMemEval, LoCoMo, HotpotQA, QMSum, faithfulness —
[docs/en/what-we-tested.md](docs/en/what-we-tested.md).

---

## Documentation

| Start here | |
|---|---|
| [Overview](docs/en/overview.md) | what the system is, in 10 minutes |
| [Quickstart](docs/en/quickstart.md) | first run |
| [Architecture](docs/en/architecture.md) | how the pieces fit |
| [Security](docs/en/security.md) | access control, audit, perimeter |
| [Product capabilities](docs/en/product-capabilities.md) | the full 22-section map |
| [Benchmarks](docs/en/benchmarks.md) | numbers, including losses |

Russian originals: [docs/ru/](docs/ru/) · Shareable HTML pages (both
languages, self-contained): [docs/share/](docs/share/)

---

## Tests

No framework to install — every file runs on its own:

```sh
python tests/unit/test_brainbench_port.py
python tests/unit/test_relational_answer_type.py
python tests/unit/test_interval_algebra.py
```

---

## Who builds this

Tessbrain is built by **[SYNLABS](https://synlabs.pro/)** — the team behind
[tessent.ai](https://tessent.ai/en). We use it ourselves, and we publish the
benchmark numbers we lose along with the ones we win.

If you are deploying this and want something that isn't here — a connector
to a system we don't support, an extractor for your domain, help fitting it
to how your company actually works, or a hand with a serious multi-tenant
install — write to **info@synlabs.pro**. That kind of work is how the open
version stays funded, so the ask is genuine rather than polite.

Bug reports and pull requests need no contract: open an issue.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
