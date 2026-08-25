# Status of the Helion corpus in this repository

> **Do not ingest this file.** `STATUS.md`, `README.md`, `SPEC.md` and
> `GLOSSARY.md` quote the banned control strings verbatim, because they are the
> documents that forbid them. Point ingestion at `transcripts/`, not the folder
> root, or arc 12 and arc 13 will be satisfied by the meta documents instead of
> by the corpus.

**The corpus is complete: 20 transcripts of 20.**

```
Validating the «Helion» corpus — slice 4: 20 of 20 described meetings
A. Golden facts             32/32 ✓
B. Negative controls          3/3  ✓
C. Forbidden co-occurrence    3/3  ✓
E. Lonely facts               1/1  ✓
G. Declared access level      1/1  ✓
H. Client-call boundary       1/1  ✓
GREEN. The corpus can be ingested and the acceptance questions run.
```

The block above is the target state, restored once the transcript pass lands.

`python3 scripts/gen_data.py` already reconciles clean against the rebased
figures: Q1-2026 from the monthly series is **$3,820,000**, matching
`derived.q1_2026_revenue`, against Guryev's spoken **$4,400,000** ("four point
four") — a 15.2 % gap, computed rather than typed. The open-pipeline figure at
2026-04-07, **$2,292,500**, is reconciled in the same pass against the CRM.

**Money scale.** Every dollar figure was multiplied by five after an
adversarial review found $3.4M across 85 people impossible for a US company:
2025 revenue is now $17,175,000. Salaries are the single exception and were
left unchanged — the engineering-lead band still runs $160,000 → $190,000 a
year. The figures are cash-basis billings, SaaS licences plus implementation
services, which is why December spikes and January dips.

**Ids.** All transliterated slugs are gone: `leadership-NN`, `sync-NN`,
`sales-NN`, `delivery-NN`, `board-01`, `client-titan-N`, `client-northern-1`;
people `helin`, `reuter`, `sokol`, `lebedev`, `valerie`; clients `sentinel`,
`northern`. Filenames keep their date prefix, which is what the validator maps
on.

## Contents

```
ground-truth.yaml           the single source of every fact and number
SPEC.md                     the demo company spec — and why each contradiction exists
GLOSSARY.md                 translator's brief: names, money rule, red lines, per-meeting anchors
transcripts/                20 meetings, 14–65 turns, 323–1,239 words each
data/crm_deals.csv          24 deals   — generated from ground truth
data/finance_monthly.csv    16 months  — generated from ground truth
scripts/gen_data.py         derived data + the Q1 reconciliation
scripts/validate_corpus.py  six named checks (A/B/C/E/G/H) + two informational (D/F)
```

## What is covered

All fourteen planted arcs have their material in the corpus. What differs is
how far a *regex* can take each one.

| Arc | State |
|---|---|
| 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12 | **Fully covered and machine-checked.** Every anchor is present, in the named meeting, in the named speaker's lines |
| 10 (classification) | **Material complete.** `board-01` is the only `access: board` meeting and carries both the round and the salary band. The validator confirms the *declaration*, not enforcement — real enforcement is a live test |
| 13 (red fields) | **Material complete.** The Northern House call gives scope, 15 projects / 7 regions and the $410,000 price; the two withheld fields are verified absent from the whole corpus. Generating the document is a live test |
| 14 (simulation) | **Material complete.** Six Volkov calls, three signature traits and three real objections, each with a dated source in `simulation_dossier`. The simulation itself is a live test |

Both localization-specific hazards are closed and enforced:

* **NC-12** — the two words arc 12 depends on appear nowhere in 20 transcripts.
  This is the hardest constraint in the English corpus, because both are what an
  English SaaS meeting reaches for by reflex; the Russian original never had to
  fight it.
* **FC-01** — "October 15" and "December" never share a transcript. October 15
  lives in `sales-04`, `leadership-06`, `client-titan-2`, `client-titan-4`; December
  lives in `leadership-02`, `sync-03`, `delivery-02`, `sync-04`, `sync-05`. Everywhere
  else the corpus says "mid-October" or "the fourth quarter," which match
  neither side.

The seven client calls are also clean of all five internal patterns (check H):
the hiring decision, the remark about Sokol, the size of the round, the salary
band, the final budget figure.

## What is not covered

**37 background meetings.** The specification describes 57; the 20 written here
are the 20 that carry golden facts. The rest were density with no planted
material. They can be written later as volume for load testing — no new
storylines are missing.

**The six supporting documents** listed in `SPEC.md` §4 of the original
specification — the Titan contract, two old proposals, the Foodline brief, the
implementation playbook, the escalation email — do not exist in this repository.
Only the two CSV sources do. Nothing in the fourteen arcs depends on them; arc
13 generates a proposal rather than reading one.

**Three tests cannot be run by this script**, by design:

* **Q10** — ask "what was discussed about salaries?" as an intern and as the
  CEO. Correct behavior is that the fact does not *exist* for the intern, not
  that access is refused. The script checks only that the meeting is declared
  `board`.
* **Q13** — build the Northern House proposal and confirm two fields come back
  red rather than plausibly filled in.
* **Q14** — run the reporting-module offer past Volkov and confirm the answer
  leans on his six recorded calls rather than an averaged "demanding client."

**Check F (graph answers) is informational.** "Who has worked with both Titan
and Foodline?" has three correct names on three different bases — direct
reporting, delegation plus a self-declaration, product ownership plus an
explicit link. Whether the system distinguishes *was in the room* from *worked
with the client* is verified by hand after ingest.

**Check D (volume and density) is not an acceptance criterion.** It prints turn
counts, word counts and speaker counts so a sterile transcript is visible, but
nothing fails on it.

**Durations are now derived, not declared.** Every transcript header carries the
last `[MM:SS]` in the file, rounded up plus one minute. The previous headers ran
roughly 7× the actual timecodes — a 47-minute standup whose last line lands at
06:54 — which is the first inconsistency a careful reader finds. Nothing checks
this automatically yet; it is a hand rule for anyone editing a transcript.

## Why the corpus exists

It closes two things at once, both more useful than they look.

**A quickstart with no customer data.** Demonstrating an enterprise system runs
straight into the fact that nobody will hand over their own data for a demo. A
synthetic company with planted storylines removes that: "ask what we promised
the bank on timing" — and the system pulls two conflicting dates out of two
different meetings, with sources. That scenario is why the corpus ships next to
the code.

**Acceptance questions.** The golden facts are the only set on which extraction
is tested end to end rather than in pieces. Fourteen questions, frozen before
generation, re-run in full after any edit to the corpus.
