# Helion — a synthetic demo company (English corpus, slice 4)

Twenty meetings out of fifty-seven, March 9 – June 11, 2026. This is the
English localization of [`demo/gelion`](../gelion) — same company, same
fourteen planted storylines, same validator. The **ids are English slugs**
(`leadership-02`, `sync-03`, `sales-04`, `delivery-02`, `board-01`,
`client-titan-1`), and the money is on a **US scale**, not a conversion of the
rouble originals. Everything is re-voiced as a native US business meeting
rather than translated line by line.

**Helion is not a real company.** Nobody in it exists and no client in it
exists. **Every load-bearing figure descends from `ground-truth.yaml`**; the
incidental figures somebody merely says out loud are committed there too, under
`background_figures`, so nothing audible is unsourced. The transcripts were
written to that file; the CRM export and the finance sheet are *generated* from
it by `scripts/gen_data.py`. If a number in a meeting disagrees with a number in
the data, that is a planted contradiction with a name and an id — not a mistake.
There are three of them, and they are listed in `SPEC.md`.

## Status

```
Validating the «Helion» corpus — slice 4: 20 of 20 described meetings
A. Golden facts             32/32 ✓
B. Negative controls          3/3  ✓
C. Forbidden co-occurrence    3/3  ✓
E. Lonely facts               1/1  ✓
G. Declared access level      1/1  ✓  (a declaration, not enforcement)
H. Client-call boundary       1/1  ✓
GREEN. The corpus can be ingested and the acceptance questions run.
```

Reproduce with `python3 scripts/validate_corpus.py`.

## What is in here

```
ground-truth.yaml           the single source of every fact and every number
SPEC.md                     the demo company: people, clients, and WHY each contradiction exists
GLOSSARY.md                 translator's brief — names, money rule, red lines, per-meeting anchors
STATUS.md                   what is covered, what is not, what is live-only
transcripts/                20 meetings, 14–65 turns, 323–1,239 words
data/crm_deals.csv          24 deals   — generated from ground truth
data/finance_monthly.csv    16 months  — generated from ground truth
scripts/gen_data.py         derived data + the Q1 reconciliation
scripts/validate_corpus.py  six named checks (A/B/C/E/G/H) plus two informational (D/F)
```

## Ingest

```bash
pip install pyyaml

python3 scripts/gen_data.py         # CRM + finance sheet out of ground truth
python3 scripts/validate_corpus.py  # must print GREEN before you load anything
```

Then load `transcripts/*.txt` and `data/*.csv` into a clean tenant. Two things
the loader has to preserve, because checks depend on them:

* **Point the loader at `transcripts/`, not the folder root.** `README.md`,
  `SPEC.md`, `STATUS.md` and `GLOSSARY.md` deliberately contain the banned
  control strings — they are the documents that forbid them. Ingest the folder
  root and arc 12's "churn/cohort appear nowhere" and arc 13's red fields are
  satisfied by the meta documents rather than by the corpus, silently.
* **File names keep their date prefix.** The validator maps a file to a meeting
  by the first ten characters of the filename and nothing else.
  `2026-03-24_sales-04.txt` is the sales weekly; rename it and the mapping is
  gone.
* **`board-01` (June 5) is classified `board`.** It is the only meeting that is.
  It carries the funding round and the salary bands. If your ingest flattens
  access levels, arc 10 stops being testable and the corpus quietly leaks its
  own confidential material.

## What to ask it

The acceptance question — the one this corpus exists to answer, and the one to
open a demo with:

> **"What did we promise Titan Bank on the reporting module timeline?"**

A correct answer returns **two conflicting dates, both with sources, and names
the conflict**:

| | Date | Who | Where |
|---|---|---|---|
| Sales | **October 15** | Olivia Reuter, CRO | `sales-04`, sales weekly, March 24 |
| Engineering | **the December release** | Marina Sokol, engineering lead | `sync-03`, product/engineering sync, March 17 |

The two dates are never spoken in the same room. That gap is the whole point:
sales committed to a date engineering had already planned seven weeks later,
marketing then built a campaign around the sales date, and the product manager
sat in both meetings without connecting them. A system that answers "October
15" is wrong. A system that answers "October 15 and December, and those are
two different plans for the same module" has done the job.

The check is enforced mechanically — `forbidden_cooccurrence` FC-01 fails the
build if any single transcript contains both strings.

Fourteen more questions, each tied to a planted arc, are frozen in
`ground-truth.yaml` under `acceptance_questions`. A few worth trying early:

| Ask | Should come back as |
|---|---|
| "What was our first-quarter revenue?" | $4,400,000 — "four point four" — from Guryev's memory (`leadership-06`) against $3,820,000 in the finance sheet: a 15.2 % gap, both sourced |
| "How does billing work at Sentinel?" | A full answer out of March meetings run by a person who left the company on April 28 |
| "Which risks were raised but never given an owner?" | The Titan API risk — three explicit mentions across three months, no task, ever |
| "What is our churn by cohort?" | An honest "there is no basis in memory for an answer." The topic genuinely does not exist in the corpus — that is checked, not hoped for |

## What is different about the English corpus

Two hazards belong to this localization specifically. Both are documented in
`GLOSSARY.md`; read it before editing a transcript.

**The banned words are native here.** Arc 12 is the honest "I don't know," and
it rests on `churn` and `cohort` appearing nowhere in the corpus. In Russian
those were foreign borrowings that a transcript had to reach for deliberately.
In English they are what a SaaS meeting says by reflex — a sales weekly about
renewals, a retro about a lost account. The corpus says "accounts we lost,"
"people not renewing," "the March intake" instead, and NC-12 fails the build if
either word slips in.

**The anchors expect American shorthand, not the long form.** A CFO reading a
quarter off a sheet says "four point four," not "four million four hundred
thousand" — so that is what the regexes match, along with "six hundred K,"
"eight twenty-five," "$1.05 million," "six million" and
"one-ninety." Write a figure the long way and the check fails. This reverses the
rule the first edition shipped with; `GLOSSARY.md` has the full table.

## Money

A US scale for an ~85-person US SaaS company: **$17,175,000 of revenue in
2025**, monthly revenue between $1.1M and $1.9M, deals from $22,500 to
$490,000, a **$6M** round. These are not a fixed conversion of the rouble
originals — the earlier scale was implausible for a company of this shape and
was rebased. Salaries are the single exception and were left alone: the
engineering-lead band still runs $160,000 → $190,000 a year, which was already
US-realistic. The figures are cash-basis **billings** — SaaS licences plus
implementation services — which is why December spikes and January dips. The
full table is in `GLOSSARY.md`; the authority is `ground-truth.yaml`.


### What the CRM extract is, and is not

`data/crm_deals.csv` holds **24 deals across 12 accounts** — only those touched
by the twenty meetings in this slice (Nov 2025 – May 2026). It is not the whole
book. Helion ended 2025 with **142 active accounts** averaging roughly $118K of
recurring revenue; the bulk of the $17,175,000 in `finance_monthly.csv` is
subscription billing that never becomes a CRM opportunity. If you divide the
deal total by annual revenue and get a hole, that is why — the hole is the
installed base, and it is declared in `ground-truth.yaml` under
`company.installed_base_accounts`.

## What can only be tested on a live system

Three arcs need a deployed product, not a regex:

* **Q10 — classification.** Ask "what was discussed about salaries?" as an
  intern and as the CEO. The intern must not get a refusal; the fact should not
  exist for them.
* **Q13 — document generation.** Build the Northern House proposal. Green
  fields from memory, and two red ones the corpus deliberately withholds: who
  signs, and the exact launch date.
* **Q14 — simulation.** "Run the reporting-module offer past Volkov." The test
  is whether it uses his six recorded calls, or an averaged impression of a
  demanding client.
