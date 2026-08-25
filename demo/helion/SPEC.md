# Specification — the Helion demo company (English corpus)

**Purpose:** one preloaded synthetic company on which any demo, to any tier-A
contact, shows every key moment of the product in 15–20 minutes — with nobody
else's data and without an empty graph.

**Construction principle:** machine-readable ground truth first, then the
meetings generated against it, then the acceptance tests read from the same
file. Every question you ask the system on a call is a test that was run green
*before* the call. The demo cannot be broken by a question, because the list of
questions was committed in advance.

**Relationship to `demo/gelion`:** this is the English localization, not a
second company. Same fourteen arcs, same validator, same acceptance questions.
Two things are deliberately *not* shared. The **ids are English slugs** —
`leadership-NN`, `sync-NN`, `sales-NN`, `delivery-NN`, `board-01`,
`client-titan-N`, `client-northern-1` — because a US corpus carrying Russian
transliterations reads as a translation. And the **money is on a US scale**,
rebased rather than converted: the earlier per-100 conversion produced a
$3.4M-a-year company running 85 people, which does not survive a first reading.
Salaries were left alone — the engineering-lead band was already US-realistic.
The transcripts are re-voiced as native US business meetings — contractions,
interruptions, people talking past each other — not translated sentence by
sentence. Names, banned words and per-meeting anchors live in `GLOSSARY.md`.

**Do not ingest this file.** `SPEC.md`, `README.md`, `STATUS.md` and
`GLOSSARY.md` all quote the banned control strings verbatim, because they are
the documents that forbid them. Point ingestion at `transcripts/`, not the
folder root.

**Three ways one corpus gets used:**
1. **Live demo** — you drive, following §8.
2. **Sandbox** — a separate tenant on the same corpus, link-access for 7 days.
   The leave-behind after a call; a prospect returning to the sandbox is a
   measurable interest signal.
3. **Three-minute screencast** — a recorded pass over arcs 1, 2 and 13. For
   cold touches and follow-up.

---

## 1. Company profile

**Helion Inc.** builds *Pulse*, a B2B customer-analytics platform sold as SaaS
plus implementation. 85 people, 7 years old, $17,175,000 of revenue in 2025,
an office plus partial remote.

**What the figures are.** Every revenue number in this corpus is cash-basis
**billings** — SaaS licence invoices plus implementation services — not
recognised revenue and not ARR. That is why December spikes (annual renewals
and year-end implementation sign-offs land together) and January dips (almost
nothing invoices, while costs run flat), and it is why the January margin is
negative two years running without the company being in trouble.

**Why this profile:** every tier-A viewer recognizes either the vendor's side
of the room (systems integrators, analytics consultancies, implementation
shops) or the client's. Helion's clients are picked to mirror the segments you
sell into — a bank, a restaurant chain, an insurer, a property developer — so
the viewer sees their own industry inside the meetings and never has to ask
"would this work for us?" A company of this shape holding weekly standups is
also unremarkable, so the corpus does not look staged.

**Departments:** Engineering (35) · Delivery and Support (15) · Sales (8) ·
Product (6) · Marketing (5) · Finance (3) · People and Office (4) ·
Leadership (4) · other (5).

**Note on the 2025 figure.** The finance sheet runs from March 2025, so it
covers ten months of 2025 ($15,000,000). The $17,175,000 annual number includes
January and February 2025, which predate the series. Q1-2026 — the number arc 2
turns on — is fully inside the sheet and is computed by script, never typed.
Q1-2025, which Guryev quotes as "about three point four" for comparison, sits
outside the series and is therefore committed explicitly under
`background_figures`.

---

## 2. Speakers — the twelve Helion voices

Each one carries a **behavioral signature**: it drives how their lines are
written *and* is itself the material for people-analytics features. A character
here is a walking test case.

| # | Name | Role | Signature (drives generation and demo) |
|---|---|---|---|
| 1 | **Andrew Helin** | CEO, founder | Decides fast, jumps between topics, **never follows up on his own decisions** (feeds arc 5) |
| 2 | **Olivia Reuter** | Chief Revenue Officer | Confident, high-energy, speaks in statements; **gives clients dates without checking engineering** (engine of arc 1) |
| 3 | **Dmitri Tsarev** | CTO | Quiet and short; **raises the same risk three times and is never heard** (arc 4) |
| 4 | **Marina Sokol** | Engineering lead | March: long, detailed turns. June: short turns, "I'll finish it over the weekend," an explicit "I'm not keeping up with everything" (arc 6) |
| 5 | **Paul Guryev** | CFO | Precise — but in the standup he quotes the quarter **from memory**, and it disagrees with the sheet (arc 2) |
| 6 | **Igor Vatulin** | Head of Delivery | Steady, dependable, takes notes; inherits Melnik's accounts after he leaves |
| 7 | **Annie Lebedev** | Analyst (junior) | **The hidden expert:** junior on paper, answers 9 of 11 data-mart questions in practice (arc 7) |
| 8 | **Kostya Melnik** | Senior implementation consultant | **Leaves April 28.** The only carrier of context on Sentinel's billing (arc 8 — bus factor) |
| 9 | **Nick Frolov** | Sales rep | Runs Titan Bank and Northern House. Job title is *sales rep* — never "account executive," see LF-05 |
| 10 | **Timur Akhmetov** | Product manager | Mediator between Reuter and Tsarev — and the one person who sits on both sides of arc 1 without ever connecting them |
| 11 | **Sveta Kim** | Head of Marketing | Builds a campaign around a date engineering never confirmed (amplifies arc 1) |
| 12 | **Vera Dorn** | HR Director | The only person who says out loud what is happening to Sokol — once, in passing, with no action after it |

**Two external voices** appear in the corpus and are not Helion employees:

* **Sergey Volkov** — **SVP, Data & Analytics, Titan Bank.** Only in the six
  `client-titan-*` calls. Pushes on dates, negotiates by phase, wants the SLA
  in writing. He is the module's **business owner**, not a procurement
  gatekeeper: he takes his case *to* the procurement committee, which is why he
  can press hard on dates and still be blocked by his own organisation. He
  refers to that committee throughout as a separate body — which now reads
  correctly.
* **Valerie Ross** — **Partner, Meridian Growth** (investor). Only in
  `board-01`, the closed board session.

(A third, **Victor Koval**, head of digital transformation at Northern House,
appears in the single Northern House call.)

---

## 3. Clients — the outside ring

| Client | Industry (mirror for) | Role in the demo |
|---|---|---|
| **Titan Bank** | banking | Largest client. Simulation persona: **Sergey Volkov, SVP Data & Analytics** — a dossier built from 6 calls: objections, style, negotiating history |
| **Foodline** | café chain, 240 locations | Arc 9: a discount given under competitor pressure → retro → the lesson |
| **Sentinel Insurance** | insurance | Arc 8: the context that walked out the door |
| **Northern House** | property development | Arc 13: proposal generation with red fields; arc 9: the same situation comes round again |
| + 8 background companies | — | CRM export only, for realistic density: Redline Services, Summit Pro, CareGrid Health, Optima Group, Cornerstone Build, Brightsmile Dental, Freightline Logistics, Keystone Capital |

Two competitors are named, each by a different route. **Retail Metrics** is the
reason for the Foodline discount, named internally (arc 9). **Analytics Plus**
is named by Volkov himself on a client call (arc 14) — first-hand, rather than
as a retelling.

---

## 4. Timeline and corpus composition

**Period: March 1 – June 30, 2026.** Four months is enough for fact versioning,
participation dynamics and "how did it look as of this date"; longer costs more
without buying anything.

The full specification describes 57 meetings. **Twenty are written**, and they
are the twenty that carry golden facts — the other 37 were background density
with no planted material and can be added later as volume if a load scenario
needs it.

### The twenty written meetings

| Date | Id | Type | Title |
|---|---|---|---|
| Mar 9 | `client-titan-1` | client call | Titan Bank — reporting status |
| Mar 10 | `leadership-02` | leadership | Leadership standup |
| Mar 17 | `sync-03` | product/eng | Product and engineering sync |
| Mar 23 | `client-titan-2` | client call | Titan Bank — launch date |
| Mar 24 | `sales-04` | sales | Sales weekly |
| Mar 26 | `delivery-02` | delivery | Delivery review by account |
| Apr 2 | `client-titan-3` | client call | Titan Bank — API access |
| Apr 7 | `leadership-06` | leadership | Leadership standup |
| Apr 14 | `leadership-07` | leadership | Leadership standup |
| Apr 21 | `sync-04` | product/eng | Product and engineering sync |
| Apr 24 | `client-titan-4` | client call | Titan Bank — contract structure |
| Apr 28 | `sales-06` | sales | Sales weekly |
| Apr 30 | `delivery-03` | delivery | Delivery review by account |
| May 12 | `client-titan-5` | client call | Titan Bank — alignment before the committee |
| May 19 | `retro-01` | retro | Retro: the Foodline franchise deal |
| Jun 2 | `sync-05` | product/eng | Product and engineering sync |
| Jun 3 | `client-northern-1` | client call | Northern House — full rollout |
| Jun 5 | `board-01` | **board** | **Closed board session** |
| Jun 9 | `leadership-09` | leadership | Leadership standup |
| Jun 11 | `client-titan-6` | client call | Titan Bank — check-in before the committee |

Two structural constraints on this table:

* `board-01` is the **only** meeting with `access: board`. Arc 10 rests on it.
* The retro's **title must contain the word "retro"** — a downstream system
  keys off it.

### Non-meeting material

| Source | Volume | Note |
|---|---|---|
| `data/crm_deals.csv` | 24 deals | Amounts, stages, close dates — the "second number" for arc 2 |
| `data/finance_monthly.csv` | 16 months | 2025-03 … 2026-06, cash-basis billings. Q1-2026 = **$3,820,000** |

Both are generated by `scripts/gen_data.py` from `ground-truth.yaml`. Neither is
edited by hand.

---

## 5. The planted arcs — why each contradiction exists

The heart of the spec. Each arc is a storyline in the corpus **plus** a demo
question **plus** an acceptance criterion. This table is committed *before* the
corpus is generated: that is what makes it a pre-commitment rather than a
post-hoc description of whatever the writing produced.

| # | Arc | Where it lives | Demo question | What the system must show |
|---|---|---|---|---|
| 1 | **Titan misalignment.** Reuter promises the client October 15 (`sales-04`, `client-titan-2`); engineering has the same module in the December release (`sync-03`, `sync-04`). Never in the same room. Kim then builds a campaign around October | 4 meetings, 2 threads | "What did we promise Titan Bank on the reporting module timeline?" | Both dates with sources, plus the sales↔engineering misalignment signal |
| 2 | **The number disagrees.** Guryev says "around four point four" from memory in the standup; the finance sheet gives $3,820,000 (+15.2 % over actual) | `leadership-06` vs CSV | "What was our first-quarter revenue?" | "Meetings say $4.4M, data says $3.82M — a 15.2 % gap," both sourced |
| 3 | **Budget versions, Pulse 2.0.** $600,000 (Mar 10) → $825,000 (Apr 21) → $1,050,000 (Jun 2), each with a reason — and **v3's reason differs in kind** from v2's: v2 was tighter infrastructure numbers, v3 is the lesson learned from Sentinel. Guryev's "seven fifty" aside in `leadership-02` is a rhetorical counterfactual, **not** a fourth version — it is committed under `background_figures` so that it is sourced without being counted | `leadership-02`, `sync-04`, `sync-05` | "How did the Pulse 2.0 budget change?" plus "how did it look on May 1?" | The version chain with dates and source meetings, and an as-of slice |
| 4 | **The blind spot.** Tsarev raises the Titan API integration risk in `sync-03`, `sync-04`, `sync-05` — pointing back at his own earlier wording each time. No task is ever created | 3 meetings + an attempt talked over in `leadership-06` | "Which risks were raised but never given an owner?" | The risk, three mentions, the absence of any task, and the escalation that was discussed and never happened |
| 5 | **The decision that stuck.** Helin decides to hire two account executives (`leadership-07`, deadline end of May). Guryev agrees to fund it but not to find anyone; HR is on vacation; **the topic never comes up again** — not even on June 9 with Vera back | `leadership-07` only | "What did we decide and never do?" | The decision, the deadline, the missing owner, and zero trace of execution |
| 6 | **Load in engineering.** Sokol's turns shrink March→June: baseline "we're keeping up" (`leadership-02`) → tone drops (`leadership-07`) → "I'll finish it over the weekend" (`sync-04`) → "I'm not keeping up with everything" plus Dorn's passing "she's gone quiet lately," with no action after it (`leadership-09`) | ~20 meetings of dynamics | "What is happening with load in engineering?" | A load signal with participation dynamics. **Present it as a signal, never as a diagnosis** |
| 7 | **The hidden expert.** Annie Lebedev, a junior, answers the data-mart questions the leads are asked — in `leadership-02`, `sync-03`, `delivery-02` | distributed | "Who here actually knows our data marts?" | Lebedev, with the evidence episodes listed |
| 8 | **Context that left.** Melnik leaves April 28. In March he explained Sentinel's billing in detail (`delivery-02`). On April 30 Vatulin opens the ticket for the promised write-up: "It's not there" (`delivery-03`) | 2 meetings + the departure | "How does billing work at Sentinel?" *(asked "in June")* | The full answer out of Melnik's March meeting, with sources. **The core pitch, shown positively** — and `delivery-03` shows honestly what is gone for good |
| 9 | **Company experience.** March: a 15 % discount to Foodline under pressure from Retail Metrics. May retro: the discount was **not** the blocker — their budget cycle was, the deal was lost anyway and the margin was spent for nothing. June: Northern House asks for the same discount | `sales-04`, `retro-01`, `client-northern-1` | "Northern House is asking for a 15 % discount — what do you say?" | The precedent: same situation, negative outcome, here is the retro and the lesson |
| 10 | **Classification.** The closed board session: a seventeen-and-a-half-million round, and the engineering-lead band raised from $160,000 to $190,000 a year (salaries were the one figure not rescaled) — with Sokol named out loud as the reason | `board-01`, access `board` | The same question, "what was discussed about salaries?", from the intern role and the CEO role | Intern: the fact does not exist — not "denied," *invisible*. CEO: the full answer. **The moment for the security buyer** |
| 11 | **Enumeration.** Three people worked with both Titan and Foodline — Frolov, Vatulin, Akhmetov — each on a *different* basis (direct reporting; delegation plus a self-declaration; product ownership plus an explicit link) | across the corpus | "Who has worked with both Titan and Foodline?" | Exactly three names — and a test that the system does not confuse *was in the room* with *worked with the client* |
| 12 | **The honest "I don't know"** (negative control). Churn by cohort is discussed nowhere and exists in no data — **verified at generation time, not assumed** | deliberate absence | "What is our churn by cohort?" | "There is no basis in memory for an answer" — instead of a plausible invention |
| 13 | **The document with red fields.** The Northern House proposal. The corpus deliberately withholds two things: who signs on the client side, and the exact launch date | material present, 2 fields absent | "Put together the proposal for Northern House" | A finished document: green fields from memory (modules, 15 projects / 7 regions, $410,000), **two red "no data" fields** — honesty as a feature, inside the artifact |
| 14 | **Simulation.** The Volkov dossier from 6 calls — style, objections, negotiating history | the client calls | "Run the reporting-module offer past Volkov" | An answer in his manner, with objections drawn from the real record, each with a dated source |

**Acceptance rule:** the demo is ready when all fourteen questions pass on a
clean ingest, three runs in a row. Any edit to the corpus triggers a full
re-run. Question wording is frozen together with the corpus — those exact
questions get asked on the call, with improvisation on top of a guaranteed
skeleton.

### The symmetry nobody planned

Both sides of the API blocker are stuck inside their own process and neither
can see the whole of the other. Internally, Tsarev knows only "they promised
access in January and never gave it" (`sync-03`). On the client call, Volkov
explains that he filed the request personally in January and it is sitting
"somewhere between the second and third approval" (`client-titan-3`,
`client-titan-6`). **No internal Helion document connects those two facts** —
the company does not know the client is stuck too, rather than stalling.

That makes a good diagnostic question for a demo: will the system link "our API
risk" (arc 4) to "his explanation of the blocker" (arc 14) into one picture, or
report them as two unrelated facts?

---

## 5-bis. Arithmetic corrections carried over from the first draft

Three numbers in the first edition of the specification did not add up and were
fixed in `ground-truth.yaml` (the figures below are already in USD):

| Was | Became | Why |
|---|---|---|
| Q1 = $412,000 against $3.4M/year | Q1-2026 = **$3,820,000**, Guryev's claim **$4,400,000** | $412,000 contradicted the annual figure outright |
| Pulse 2.0 budget $20K → $28K → $35K | **$600K → $825K → $1.05M** | $20,000 for a flagship release is not credible for 85 people |
| "revenue about $3.4M" | **$17,175,000**, tied to the monthly series | the figure is now a result, not a declaration |

The 15.2 % gap in arc 2 is computed by `gen_data.py` from the series. It is
never written into a transcript, and never stated as a percentage in the text.

### The second rebase — the whole money scale, ×5

An adversarial review found the corrected scale still impossible: $3.4M of
annual revenue across 85 people is $40K of revenue per head, which no US SaaS
company survives. **Every dollar figure in the corpus was multiplied by five**,
end to end — the finance series, the CRM, the derived figures, the budget
versions, the round. That puts 2025 at $17,175,000, about $200K per head, which
is unremarkable for the shape of company described in §1.

**Salaries were the one exception and were deliberately left alone.** The
engineering-lead band running $160,000 → $190,000 a year was already
US-realistic; multiplying it would have produced a $950,000 engineering lead.
The band is the only figure in the corpus that does not descend from the ×5
rebase, and GF-10B anchors it as `one-ninety` to keep it visibly separate.

Four figures that were merely *spoken* had no source at all before the rebase —
Q1-2025, the CEO's full-year forecast, the open-pipeline total, and Lebedev's
dashboard count — and a fifth, Guryev's rhetorical "seven fifty" in
`leadership-02`, was caught by the same sweep. A later money-tie pass over
`sales-04` added three more: the $530,000 Titan pair Reuter and Frolov add up
(D-002 + D-003), the $28,000 trade-show booth in Kim's aside, and the 20 %
Retail Metrics price gap that the Foodline discount was measured against. The
same pass over `board-01` added one more: the 20 % gap Guryev quotes between
Helion's top engineering band and the market, which is the justification for
moving the lead ceiling from $160,000 to $190,000. It ties to that band by
rounding and not exactly — the move is +18.75 % — so Guryev says on the line
that it does not fully close the gap; the looseness is a spoken estimate, not a
fourth planted contradiction. The same pass over `retro-01` caught the last
one: the 10 % standard discount ceiling Reuter quotes Northern House
procurement. That one is a policy limit rather than a figure anyone was given —
no deal carries it, nothing is agreed with Northern House — and it is spoken two
lines away from the 15 % Foodline discount it implicitly contrasts with, so the
dialogue names the difference out loud instead of leaving an extractor to join
them. Every one of them is committed under
`background_figures` in `ground-truth.yaml`, each with its status: sheet-adjacent, deliberately
unbacked, arithmetic over the CRM, a hand count, a counterfactual that is
explicitly **not** a budget version, an operational cost, a standing policy
number, or a benchmark somebody is reading off the market — a competitor's price
in `sales-04`, pay in `board-01`.

---

## 6. Generation pipeline

**Step 0. `ground-truth.yaml`** — every fact: people, roles, clients, deals with
amounts, dates, decisions, budget versions, who attended what. **Every
load-bearing figure descends from this file**, and the incidental figures that
are only ever said out loud are committed alongside them under
`background_figures`, each labelled with its status. The CRM CSV and the finance
sheet are generated by script from the same file, so arc 2 closes arithmetically
rather than by luck.

**Step 1. Character bibles** — per person: tone, turn length, verbal tics,
dynamics over time (Sokol's "energy" parameter drops by month:
1.0 → 0.8 → 0.6 → 0.4 across March–June).

**Step 2. Scene beats** — per meeting: which golden facts are spoken, by whom,
verbatim or paraphrased. Arc 1 requires that "October 15" and "December" never
meet in one meeting — checked by script, not by eye.

**Step 3. Transcript generation** — one meeting at a time: speakers, timecodes,
the equivalent of 30–60 minutes. **15–20 % everyday noise is mandatory:**
tangents, corrections, interruptions, "can we come back to that." A sterile
corpus gives itself away in a minute and takes the credibility of the whole
demo with it. For the English corpus this also means *American* noise —
contractions, sentence fragments, two people answering at once.

**Step 4. Automatic validation** — `scripts/validate_corpus.py`: (a) every
golden fact is present in its assigned meeting, in the assigned speaker's
lines; (b) negative controls are absent from the entire corpus; (c) forbidden
co-occurrences do not appear; (d) lonely facts appear exactly once; (e) client
calls contain no internal facts.

**Step 5. Ingest into a clean tenant → run the 14 acceptance questions.** All
green means the demo exists. Anything red gets fixed in the corpus or in the
product — and that is separately valuable: the demo company becomes a
regression test on every release.

### The client-call boundary (check H)

None of the seven client calls — six with Titan plus Northern House — may
contain an internal company fact: the hiring decision, the observation about
Sokol's state, the size of the round, the new salary band, the final v3 budget.
The patterns are the already-validated internal anchors, reused deliberately:
if one of them turns up in a client call, that is the *same* fact somewhere it
is not allowed to be.

This is a direct test of the same access boundary the product sells as an
enterprise feature. The boundary has to hold in the data the client simulation
is assembled from, not only in the interface.

**The narrow-pattern rule, learned the hard way in the Russian corpus and
inherited here:** LF-05 and CB-01 both match `account executives?` and nothing
broader. A grammatically complete pattern for "hire" was tried and rejected —
it also caught `leadership-06`, where Helin asks Vatulin "Do we hire, then?" about
delivery capacity and gets "Not yet." That is a legitimate, unrelated hiring
conversation. Breadth of coverage and precision of target are different axes,
and for a check of the form *this specific secret must not leak*, precision
wins. The corpus says "sales rep," "salesperson" or "someone in sales"
everywhere except the one line in `leadership-07`.

---

## 7. Operation

* **Reset script:** clean tenant from a snapshot in under a minute. Run before
  every call — the previous prospect may have left mess in the sandbox.
* **Sandbox tenant:** link lives 7 days, read-only plus chat. Goes in the
  follow-up email. Log the logins: "came back to the sandbox" is a hot signal.
* **Screencast:** 3 minutes, arcs 8 → 1 → 13. Re-record when the UI changes.

---

## 8. The 20-minute live demo

| Time | What you show | Arc | Bridge line |
|---|---|---|---|
| 0–2 | Context: "a synthetic company, 57 meetings, 4 months, here is what is in it" | — | "I don't need your data — look at someone else's" |
| 2–5 | Sourced answer about the person who left | 8 | "He quit in April. Let's ask in June." |
| 5–9 | The misalignment plus the number that disagrees | 1, 2 | "Sales and engineering are living in two realities — the system sees it first" |
| 9–12 | Budget versions plus the as-of-May-1 slice | 3 | "Not *what is the budget* — *how did it move, and why*" |
| 12–14 | The proposal with green and red fields | 13 | "The system tells you what it does *not* know, right inside the document" |
| 14–16 | Classification: the same question as intern and as CEO | 10 | "The boundary is cut before the model — the model never saw the restricted material" |
| 16–18 | The honest "I don't know" plus the measurements page | 12 | "Here are our numbers, including the ones where we lose" |
| 18–20 | Pilot structure, next step | — | from the 1-pager |

**Emphasis by vertical:** banking — 10 → 13 → 8 (security first); retail/QSR —
1 → 9 → 5 (operations and follow-through); integrators — the ingest pipeline,
MCP and open-core boundaries instead of arcs 6–7; an HR audience — 6 → 7 → 3.

**Held in reserve:** arcs 5, 7, 11 and 14 are not in the base twenty minutes.
They are the answers to "can it also…" and the material for a second meeting.

---


### What the CRM extract is, and is not

`data/crm_deals.csv` holds **24 deals across 12 accounts** — only those touched
by the twenty meetings in this slice (Nov 2025 – May 2026). It is not the whole
book. Helion ended 2025 with **142 active accounts** averaging roughly $118K of
recurring revenue; the bulk of the $17,175,000 in `finance_monthly.csv` is
subscription billing that never becomes a CRM opportunity. If you divide the
deal total by annual revenue and get a hole, that is why — the hole is the
installed base, and it is declared in `ground-truth.yaml` under
`company.installed_base_accounts`.

## 9. Effort and risks

| Stage | Estimate |
|---|---|
| `ground-truth.yaml` + character bibles | 1–2 days |
| Beat and transcript generation + validation | 2–3 days |
| Ingest, 14-question run, fixes | 1–2 days |
| Reset + sandbox + screencast | 1 day |
| **Total** | **~1.5–2 person-weeks** |

**Risks and how they are closed:**

1. *Sterile speech* → step 3, 15–20 % noise, plus a human read of three random
   transcripts.
2. *Inconsistent numbers* → ground-truth-first: every load-bearing figure
   descends from `ground-truth.yaml`, and the incidental figures a speaker
   merely says out loud are committed in `background_figures` rather than left
   to be invented at read time.
6. *An implausible money scale* → the reason for the ×5 rebase above. A demo
   audience that sells into this segment does the revenue-per-head arithmetic in
   its head, and a corpus that fails it loses the room before arc 1.
3. *Expensive ingest* → end-to-end on 5 meetings first, scale after a green run.
4. *An arc the product cannot find* → not a failed demo, a bug found before the
   call. That is exactly what the run is for.
5. *Localization drift* (English corpus only) → the two words arc 12 depends on
   are native English SaaS reflexes, and the anchors now require American
   shorthand ("four point four", "eight twenty-five", "one-ninety") rather than
   the long form. Both are enforced by the validator, and both are written up in
   `GLOSSARY.md` before anyone edits a transcript.
