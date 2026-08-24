# Tessbrain

The memory layer underneath a company. Meetings and documents become a
typed graph where every fact carries its source, its date and how sure we
are of it — and questions get answered from that graph, with citations.

This repository is the open core: the retrieval engine, the typed store,
the access boundary, and the benchmark harness we publish our numbers
with. It is the part you can audit, fork and run yourself.

## Why this exists

Three things break a company's memory, and they break independently. A
person leaves and the context leaves with them — not the files, the
reasoning. A decision gets made twice because nobody can find the first
one. And an assistant that answers confidently without a source cannot be
used where the answer matters.

## What is in here

| Area | What it does |
|---|---|
| `backend/core/search/` | three retrieval channels — lexical (BM25), vector, graph — fused by reciprocal rank fusion |
| `backend/core/store/` | the typed graph, the vector index, and tenant isolation |
| `backend/core/access/` | the access boundary: a clearance sits on the individual fact, and restricted facts are removed *before* the model sees them |
| `backend/core/lineage/` | provenance — where each fact came from |
| `backend/core/temporal/` | Allen interval algebra, for questions about when |
| `scripts/brainbench_run.py` | our stack on someone else's benchmark |
| `integrations/gbrain-evals/` | an adapter that runs this code inside GBrain's own runner |

## Numbers, including the ones we lose

We ran [BrainBench](https://github.com/garrytan/gbrain-evals) — someone
else's corpus, someone else's queries, someone else's formulas — in
*their* runner, five page-order shuffles, standard deviation 0.0:

| | P@5 | R@5 | correct |
|---|---|---|---|
| our BM25 alone (control) | 0.161 | 0.597 | — |
| **this repo, default config** | **0.316** | **0.900** | 229 / 261 |
| this repo, `CONFIDENT_GRAPH=on` | 0.729 | 0.991 | 257 / 261 |
| gbrain, as they published it | 0.491 | 0.979 | 248 / 261 |

On defaults we are **behind** gbrain and ahead of every non-graph
baseline it publishes. The flag that puts us ahead ships **off**, because
all 145 questions in that suite have the same shape and we will not
headline a number we are not ready to serve by default.

That number was wrong once. We first published 0.729 from rules hardcoded
in our own benchmark script, corrected it down to 0.240 when we found
them, and only then earned it back by moving the ideas into the product
behind kill-switches. The whole story, with ablations:
[`docs/ru/BENCHMARK_BRAINBENCH.md`](docs/ru/BENCHMARK_BRAINBENCH.md).

## Running the tests

No test framework to install — every file runs on its own:

```sh
python tests/unit/test_brainbench_port.py
python tests/unit/test_interval_algebra.py
python tests/unit/test_relational_answer_type.py
```

## Reproducing the benchmark

```sh
git clone --depth 1 https://github.com/garrytan/gbrain-evals /tmp/ge
python scripts/brainbench_run.py --corpus /tmp/ge/eval/data/world-v1
```

## Documentation

English documentation is in [`docs/en/`](docs/en/); the Russian
originals are in [`docs/ru/`](docs/ru/). Start with
[`docs/en/overview.md`](docs/en/overview.md).

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).

## Who builds this

Tessbrain is built by SYNLABS. We use it ourselves, and we publish the
benchmark numbers we lose along with the ones we win.

If you are deploying this and want something that isn't here — a
connector to a system we don't support, an extractor for your domain,
help fitting it to how your company actually works, or a hand with a
serious multi-tenant install — write to info@synlabs.pro. That kind of work is
how the open version stays funded, so the ask is genuine rather than
polite.

Bug reports and pull requests need no contract: open an issue.
