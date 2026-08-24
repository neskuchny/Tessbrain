# Benchmarks

Every number here comes from a run whose harness is in this repository. Where
a result is weak, it is reported as weak — a benchmark page that only contains
wins is marketing, not evidence.

Read this together with [What we actually tested](what-we-tested.md), which
describes what these runs do **not** cover.

---

## Memory: LongMemEval

The first three-arm measurement of memory itself.

**Setup:** oracle (evidence sessions only) · n=80, stratified across all six
question types · judge: `gpt-4o-mini` · harness:
`backend/core/eval/benchmark_longmemeval.py` · date: 2026-06-08

### Overall accuracy

| Arm | gpt-4o-mini | gemini-3.1-flash-lite |
|---|---|---|
| **A — none** (no memory) | 0.025 | 0.038 |
| **B — dump** (whole history into context) | 0.488 | 0.600 |
| **C — retrieval** (BM25 stand-in) | 0.513 | 0.575 |

### By question type

| Type | none | dump | retrieval |
|---|---|---|---|
| single-session-user | 0.0 | 0.91 / 0.91 | 0.91 / 0.91 |
| single-session-assistant | 0.0 | 0.75 / 1.00 | 0.75 / 1.00 |
| knowledge-update | 0.0 | 0.79 / 0.90 | 0.84 / 0.90 |
| single-session-preference | 0.0 | 0.00 / 0.33 | 0.11 / 0.56 |
| temporal-reasoning | 0.05 | 0.33 / 0.29 | 0.29 / 0.33 |
| **multi-session** | 0.06 | 0.25 / 0.50 | 0.31 / 0.19 |

### What this honestly shows

**The benchmark is valid — memory is genuinely required.** The `none` arm
scores about 0.03 on both models. A bare LLM cannot answer questions about a
history it never saw, so this measures memory rather than parametric knowledge.

**Easy types are solved by both arms** (0.75–1.00). When the fact is local to
one session, dump and retrieval are indistinguishable.

**The battleground is multi-session and temporal** (0.19–0.50). Connecting
*different* sessions is the hardest class, and the one that matters most for a
company with a long conversation history.

Detail: [`../ru/BENCHMARK_LONGMEMEVAL.md`](../ru/BENCHMARK_LONGMEMEVAL.md),
[`../ru/BENCHMARK_LONGMEMEVAL_S.md`](../ru/BENCHMARK_LONGMEMEVAL_S.md)

---

## Public sets: LoCoMo and HotpotQA

Answering strategies compared on public question sets. The headline finding is
about *profile*, not a single score:

On one configuration `strict` and `analysis` reach the **same overall score
(0.433)** with completely different profiles — strict is precise but narrow,
analysis is broader but looser. A single averaged number would have hidden
that entirely.

On HotpotQA multi-hop questions (where an answer exists), the score moves from
0.567 to 0.767 (+0.20).

Detail:
[`../ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md`](../ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md)

---

## Someone else's yardstick: BrainBench (gbrain-evals)

Comparing our metrics against another system's metrics proves nothing — the
formulas differ. So we ran our retrieval stack on
[`garrytan/gbrain-evals`](https://github.com/garrytan/gbrain-evals): their
240-page corpus, their 145 relational queries, their P@5 / R@5.

Results as a ladder — each row differs from the previous by one feature,
measured in **their own runner** (a thin adapter bridge lives in
`integrations/gbrain-evals/`; N=5 page-order shuffles, stddev 0.0):

| Variant | P@5 | R@5 |
|---|---|---|
| our BM25 without the graph (control) | 0.161 | 0.597 |
| graph via RRF, no relational detector | 0.240 | 0.806 |
| **production defaults: + relational detector** | **0.316** | **0.900** |
| + CONFIDENT_GRAPH=on (flag, off by default) | 0.729 | 0.991 |
| gbrain, as published by them | 0.491 | 0.979 |
| their best non-graph baseline | 0.178 | 0.651 |

With production defaults we are behind their graph system and ahead of
every non-graph baseline they publish. With the flag on we are ahead of
their published number too (257/261 correct vs their 248/261) — but the
flag ships **off** until the mode is validated on our own data, and every
place that quotes the number says so.

The number's history, told in full: we first published 0.729 from rules
hardcoded in the benchmark script ("*Who* → person-typed neighbours only",
"graph hits only, no top-up"), corrected it down to 0.240 as
corpus-fitting, and only then brought the same ideas back — as product
features with Russian-language tests and kill-switches: a relational-question
detector (`RELATIONAL_DETECT`, "кто был на встрече" / "who attended" with
no "all" required), expected-answer-type ("кто/who" → person), and a
confident structural answer (`CONFIDENT_GRAPH`, default OFF). The benchmark
script no longer contains a single ranking rule of its own; contract tests
keep it that way. The corpus served as validation for these rules, not as
their source — but its 145 questions are homogeneous (all "Who …?"), so
the confident mode's edge will be smaller on diverse questions, and we say
that out loud.

Caveats we cannot remove: no vector channel in our run (theirs has one);
port-vs-runner drift is ~1 point (control: their grep-only 0.171/0.624 vs
our BM25 port 0.161/0.597); edges on this corpus are untyped links, so our
production extraction pass is not exercised at all.

Detail, ablations and reproduction steps:
[`../ru/BENCHMARK_BRAINBENCH.md`](../ru/BENCHMARK_BRAINBENCH.md)

---

## Other runs

| Run | Document |
|---|---|
| Head-to-head vs Mem0 | [`../ru/BENCHMARK_MEM0_H2H.md`](../ru/BENCHMARK_MEM0_H2H.md) |
| Faithfulness | [`../ru/BENCHMARK_FAITHFULNESS.md`](../ru/BENCHMARK_FAITHFULNESS.md) |
| Meeting summarisation (QMSum) | [`../ru/BENCHMARK_QMSUM.md`](../ru/BENCHMARK_QMSUM.md) |
| Counting / enumeration | [`../ru/BENCHMARK_COUNTING.md`](../ru/BENCHMARK_COUNTING.md) |
| Analysis mode | [`../ru/BENCHMARK_ANALYSIS_MODE.md`](../ru/BENCHMARK_ANALYSIS_MODE.md) |

---

## Cost accounting

Token usage is recorded per run, so a quality result can be divided by what it
cost. `scripts/cost_per_correct.py` turns a run into calls-per-correct-answer
and a dollar figure.

This exists because two strategies with the same accuracy can differ by more
than an order of magnitude in price, and an accuracy table alone makes them
look equivalent.

---

## Guards against fooling ourselves

Two are worth naming, because both were added after a real incident rather
than in principle:

**Degenerate-answer detection.** In one run an open model emitted prompt
scaffolding instead of answers, and the judge counted a large share of it as
correct — producing an impossible score on the `none` arm. The harness now
detects scaffolding-shaped output and marks the whole run `INVALID` rather
than reporting an inflated number. This guards against *silent inflation*,
the mirror image of the fallback-driven *silent degradation* that was already
guarded.

**Reproducible inputs.** Evaluation datasets are fetched to a persistent cache
(`~/.cache/tessent-eval`) rather than read from a temporary path, so a run can
be repeated later and get the same inputs.

---

## How to read these numbers

**Sample sizes are 20–200 questions.** These are real measurements, not
statistics. A difference of two or three hundredths on thirty questions is
noise, and is not treated as a result here.

**Judged, not exact-match.** Where an LLM judge is used, it is named. Judges
have their own failure modes; the degenerate-answer guard above exists because
one of them bit.
