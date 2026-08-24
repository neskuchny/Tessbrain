# Tessbrain adapter for BrainBench (gbrain-evals)

Two files that let [BrainBench](https://github.com/garrytan/gbrain-evals)
score the Tessbrain retrieval stack alongside its own adapters.

## Why a bridge instead of a TypeScript port

BrainBench adapters are TypeScript; Tessbrain's retrieval is Python.
Reimplementing the stack in TypeScript would benchmark a copy, and any
drift between the copy and the real system would be invisible — which
defeats the point of running on someone else's yardstick. So the adapter
is thin: it spawns `scripts/brainbench_run.py --serve` and exchanges
newline-delimited JSON with it over pipes. What answers each query is the
same code production runs (`bm25_searcher`, `rrf_fusion`,
`enumerative_detect`), with production's fusion weights.

No network, no third-party npm packages, no gold data across the
boundary — only `slug`, `type`, `title`, `compiled_truth`, `timeline` per
page and `{id, text}` per query.

## Install

```sh
git clone --depth 1 https://github.com/garrytan/gbrain-evals
cp tessent-brain.ts tessent-brain.test.ts gbrain-evals/eval/runner/adapters/
```

Then register it in `eval/runner/multi-adapter.ts`:

```typescript
import { TessentAdapter } from './adapters/tessent-brain.ts';

const allAdapters: Adapter[] = [
  ...existing,
  new TessentAdapter(),
];
```

The two files import `../types.ts`, which resolves once they sit in
`eval/runner/adapters/`. They are written to live in that tree, not this
one.

## Run

```sh
export TESSENT_REPO=/path/to/tessent_brain    # a checkout of this repo
export TESSENT_PYTHON=python3                 # optional, defaults to python3

cd gbrain-evals
bun test eval/runner/adapters/tessent-brain.test.ts
bun run eval:run:dev --adapter=tessent-brain  # N=1
BRAINBENCH_N=5 bun eval/runner/multi-adapter.ts --adapter=tessent-brain
```

Python 3.10+ with no third-party packages is enough — the retrieval
modules the bridge imports are stdlib-only.

Verified in their tree: the adapter's own tests (6/6) and the benchmark
runs above. **Not** verified: their full `bun run test` — it rebuilds a
PGLite schema (125 migrations) per test file and did not finish within
28 minutes in our container; the only failures seen before the cutoff
were in gbrain's own `hybrid.ts` (an evals↔gbrain master version drift),
none in this adapter. A PR to their repo should let their CI be the
judge of the full suite.

## Measured

Corpus `world-v1`, 240 pages, 145 relational queries, their runner,
N=5 page-order shuffles:

| Adapter | Runs | P@5 | R@5 | correct |
|---|---|---|---|---|
| tessent-brain (production defaults) | 5 | 31.6% ±0.0 | 90.0% | 229 / 261 |
| tessent-brain, `CONFIDENT_GRAPH=on` | 5 | 72.9% ±0.0 | 99.1% | 257 / 261 |
| gbrain (their published) | 1 | 49.1% | 97.9% | 248 / 261 |
| vector-grep-rrf-fusion | 1 | 17.8% | 65.1% | 129 / 261 |
| grep-only | 1 | 17.1% | 62.4% | 124 / 261 |
| vector | 1 | 10.8% | 40.7% | 78 / 261 |

Stddev is 0.0 in both modes, as BrainBench's adapter bar asks. Tie-break:
graph neighbours enter fusion sorted by slug ascending, so rank never
depends on hash-set iteration order.

The two rows are one product flag apart. `CONFIDENT_GRAPH` turns on the
confident structural answer: when the question is relational, the expected
answer type is known ("who" → person) and the graph produced nodes of that
type, the result is narrowed to that set instead of being topped up with
lexical noise. The flag ships **off** until the mode is validated on our
own real-meeting data (the product runs in Russian and English; our test
sets are Russian-first because that is the language we can verify
ourselves) — this corpus's 145 questions are homogeneous (all "Who …?"),
so the mode's edge will be smaller on diverse questions. The rules themselves live in production modules
(`enumerative_detect`, `answer_type`, `confident_graph`) with
Russian-language tests; this adapter contains no ranking logic of its own.

Known factors not fixed by tuning against this corpus: no vector channel
in our run (gbrain's number includes one), and edges here come from
markdown links, so they are untyped — our production extraction pass
(typed edges) is not exercised at all.

Full write-up, ablations and the record of a number we first corrected
downward and then earned back through product features:
[`../../docs/ru/BENCHMARK_BRAINBENCH.md`](../../docs/ru/BENCHMARK_BRAINBENCH.md).
