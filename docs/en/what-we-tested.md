# What we actually tested

This page exists because the most useful thing a benchmark section can say is
what it does **not** cover. If you are evaluating this system, read this before
[Benchmarks](benchmarks.md).

---

## The layer question: memory, or just an LLM?

For a long stretch of our evaluation history the honest answer was: **just an
LLM plus prompt discipline. Memory was not tested at all.**

In four early runs (reasoning diagnostics, QMSum, TruthfulQA, RAGTruth) all the
context a model needed was handed to it directly in the prompt — the
transcript, the source, the answer options. The model never once reached for
the thing that actually is the system:

| Our layer | Exercised in those benchmarks? |
|---|---|
| Company graph (per-user `GraphBuilder`) | No |
| Business Wisdom Engine / accumulated experience | No |
| Accumulation across many meetings | No |
| Claim guard against the event graph | No |
| Blind-spot / desync detection over history | No |
| Nightly consolidation | No |

**Why it happened.** Public benchmarks have no "company" in them: no
accumulated history, no graph. There is nothing for memory to ground against.
So the most portable layer got measured — discipline on a stateless LLM.

**What follows.** The faithfulness improvement in those runs is real, but it
belongs to **the LLM and the prompt, not to our memory**. The main
differentiator — accumulation plus grounding — was left unmeasured.

LongMemEval (see [Benchmarks](benchmarks.md)) was the first run that actually
measured memory, and it is reported with its weak spots intact.

---

## Where the gap between claim and measurement sits

**Layer mismatch.** Stateless benchmarks cannot test a thesis about
accumulation. On such sets our central claim is structurally unverifiable —
not unverified, *unverifiable*. The fix is not a better score; it is a
different benchmark.

**Sample sizes.** 20–200 questions per run. Real measurements, not statistics.
Small differences are noise and are not reported as wins.

**Judges.** Where an LLM judge scores answers, it can be fooled. It was, once:
a model emitted prompt scaffolding, the judge scored much of it correct, and
the resulting number was physically impossible. That run is marked `INVALID`
and a detector now guards against the same failure. Assume judge-scored
figures carry that class of risk.

---

## Things measured on cloud models only

Every published number was produced against cloud models.

The harness for measuring against a local model in a closed perimeter exists
and works — an OpenAI-compatible base URL is enough. **The measurement has not
been run.** So the honest answer to "how much quality do I lose in an
air-gapped deployment?" is: *we do not know yet*.

---

## Things not built, and stated as such

- **Sensitivity classification is manual.** Facts default to "internal";
  raising the level is a human action.
- **Agents do not negotiate.** An external executor takes a task and returns a
  result under machine-checked acceptance — request/response, not dialogue.
- **Company experience starts accumulating when you switch it on.** Outcomes
  cannot be reconstructed retroactively; for an already-loaded archive, the
  experience layer appears only on reprocessing.
- **Cross-deployment company discovery is not built.** It appears as a dashed
  line on the architecture diagram for a reason.

---

## Why this page is in the public documentation

A system that answers questions for a board of directors is judged on whether
its claims survive contact with a skeptic. Publishing the boundary is cheaper
than having someone discover it in a pilot.

If you find a claim elsewhere in this documentation that is not backed by
either code or a named run, that is a bug — please open an issue.
