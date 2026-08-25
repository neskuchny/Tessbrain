# Helion — translator's brief (English localization of the «Гелион» corpus)

> **Reference document for maintainers — NEVER ingest this file; ingest
> `transcripts/` only.** This brief, like `SPEC.md`, `README.md` and
> `STATUS.md`, quotes the banned control strings verbatim in order to forbid
> them. Point your loader at `transcripts/` (and `data/`), never at the folder
> root, or the negative controls arc 12 and arc 13 rest on will be ingested as
> corpus content and every one of them will read as satisfied.

Everything you write is machine-checked. `scripts/validate_corpus.py` runs
every regex in `ground-truth.yaml` against your transcripts before ingest.
Red means the transcript gets rewritten, not the report.

Three things the validator does, so you know what it can see:

* **Anchors** (`golden_facts`) are searched **only inside the named speaker's
  lines of the named meeting**. Put the phrase in the wrong person's mouth and
  it fails, even though the words are on the page.
* **Bans** (`negative_controls`, `client_boundary`, `forbidden_in_text`) are
  searched in the **whole file**, header block included.
* Matching is **case-insensitive**. `Churn`, `CHURN` and `churn` are the same
  violation.

---

## 🚫 RED LINES — read this before anything else

### 1. Words that must appear NOWHERE in the corpus

| Forbidden | Control | Why | Say instead |
|---|---|---|---|
| **churn** | NC-12 | Arc 12 is the honest "I don't know". The topic was never discussed. | "customers leaving", "accounts we lost", "renewals", "people not renewing" |
| **cohort** | NC-12 | same | "the March intake", "the group that signed in Q1", "that batch of accounts" |
| **Demidov** *(and the Cyrillic **Демидов**)* | NC-13A | Arc 13: the Northern House signatory is never named → red field in the proposal. | nothing — the client "is still deciding internally who signs" |
| **delivery date** (exact phrase) | NC-13B | Arc 13: the second red field. | "launch", "go-live", "when it goes live", "the timeline", "launch date" |

**`churn` and `cohort` are the hardest trap in this whole job.** An English
SaaS transcript produces both words by reflex — a sales weekly about renewals,
a retro about a lost account, any sentence starting "if we look at the
customers who signed in March…". The Russian corpus never had to fight this
because the words were foreign there. You do. Grep your file before you hand
it in:

```
grep -inE 'churn|cohort|Demidov|Демидов|delivery date' transcripts/*.txt   # must return nothing
```

### 2. The once-only phrase

> ### **"account executive" / "account executives"**
> This appears **exactly once in the entire corpus** — Andrew Helin's line in
> **leadership-07** (`2026-04-14`). Nowhere else. Not in another meeting, not in a
> participant list, not as anyone's job title.

That is the whole point of arc 5: a decision is made and then never spoken of
again. If the phrase surfaces a second time anywhere, LF-05 goes red and the
arc stops existing.

Everywhere else in the corpus — including **Nick Frolov's own role**, which is
"Sales rep", never "Account Executive" — use: *sales rep*, *salesperson*,
*someone in sales*, *a new seller*, *headcount in sales*.

Related, legitimate, and **not** a violation: leadership-06 has a different hiring
conversation about delivery capacity ("Do we hire, then?"). That is a
different topic and the pattern is deliberately narrow so it does not catch it.
Just never let the words *account executive* into it.

### 3. The pair that must never share a transcript

**"October 15" × "December"** (FC-01). Two dates for the same Titan reporting
module. They are never said in the same room — that gap *is* arc 1.

| Files that say **October 15** | Files that say **December** |
|---|---|
| sales-04, leadership-06, client-titan-2, client-titan-4 | leadership-02, sync-03, delivery-02, sync-04, sync-05 |

Anywhere else, and in leadership-06 specifically, use **"mid-October"** or **"the
fourth quarter"** — neither matches either side, so both stay safe. Watch the
header block too: a meeting title containing "December" would break it.

---

## Names — use exactly these

### People

| Russian | English | Role in the corpus |
|---|---|---|
| Гелион | **Helion** | the company (`Helion Inc.`) |
| «Пульс» | **Pulse** | the product |
| Андрей Гелин | **Andrew Helin** | CEO, founder |
| Ольга Реутова | **Olivia Reuter** | Chief Revenue Officer |
| Дмитрий Царёв | **Dmitri Tsarev** | CTO |
| Марина Соколова | **Marina Sokol** | Engineering lead |
| Павел Гурьев | **Paul Guryev** | CFO |
| Игорь Ватулин | **Igor Vatulin** | Head of Delivery |
| Аня Лебедева | **Annie Lebedev** | Analyst (junior) |
| Костя Мельник | **Kostya Melnik** | Senior implementation consultant |
| Никита Фролов | **Nick Frolov** | **Sales rep** — never "account executive" |
| Тимур Ахметов | **Timur Akhmetov** | Product manager |
| Света Ким | **Sveta Kim** | Head of Marketing |
| Вера Дорн | **Vera Dorn** | HR Director |
| Валерий | **Valerie Ross** | Partner, Meridian Growth (investor) — `board-01` only |
| Сергей Волков | **Sergey Volkov** | **SVP, Data & Analytics, Titan Bank** (client calls only) — the module's business owner, *not* a procurement gatekeeper |
| Виктор Ковалёв | **Victor Koval** | head of digital transformation, Northern House |
| Демидов | — | **must not exist in either script.** NC-13A now matches `Demidov|Демидов` |

Speaker labels in transcripts are matched by exact string. Write the full name
every time: `[04:12] Marina Sokol:` — not `Marina:`, not `M. Sokol:`.

### Clients

| Russian | English |
|---|---|
| «Титан-Банк» | **Titan Bank** |
| «Северный Дом» | **Northern House** |
| «Фудлайн» | **Foodline** |
| «Гарант-Страхование» | **Sentinel Insurance** (short form: *Sentinel*) |
| Ритейл-Метрик | **Retail Metrics** (competitor, arc 9) |
| Аналитики Плюс | **Analytics Plus** (competitor, arc 14) |
| «Ремтех» · «Аквилон-Про» · «МедЛайн» · «Оптима-Групп» · «Стройбаза-77» · «Витадент» · «Логопарк» · «Артель-Инвест» | Redline Services · Summit Pro · CareGrid Health · Optima Group · Cornerstone Build · Brightsmile Dental · Freightline Logistics · Keystone Capital (CRM export only) |

### Domain terms

| Russian | English | Note |
|---|---|---|
| витрины (данных) | **data marts** | anchor for GF-07A/B/C — use this exact term, not "data models" or "views" |
| биллинг | **billing** | anchor for GF-08 |
| АБС | **core banking system** | the Titan integration; the risk itself is always called **the API** |
| мультитариф | **multi-rate** | Sentinel's billing feature, arcs 8 and 3 (US insurance says rates, never tariffs) |
| промышленная эксплуатация | **production** / **go-live** | never "delivery date" |
| закупочный комитет | **the procurement committee** / **the committee** | Volkov's constraint — and a **separate body he does not sit on**. He is the business owner taking his case *to* the committee, which is why he can be pushed on dates and still be blocked |
| планёрка руководства | **leadership standup** | meeting title |
| ретро | **retro** | the retro-01 **title must contain the word "retro"** |

---

## Money — a US scale, spoken the way a US operator speaks it

Helion is an ~85-person US SaaS company. The figures are **not** a conversion
of the rouble originals: they were rebased to that scale (2025 revenue
$17,175,000, monthly revenue $1.1M–$1.9M, deals $22.5K–$490K). **Salaries are
the one exception** — the engineering-lead band stays at $160,000 → $190,000 a
year, which was already US-realistic.

> ### ⚠ The anchors now expect SHORTHAND, not the long form
> This is the reverse of the old rule and it is the single most important line
> in this file. The regexes match **"four point four"**, **"six hundred K"**,
> **"eight twenty-five"**, **"$1.05 million"**, **"six million"**, **"one-ninety"**. Writing *"four million four hundred thousand"*
> or *"eight hundred and twenty-five thousand"* now **fails the check**. A CFO
> reading a quarter off a sheet says "four point four" — write that.

### Figures that get spoken out loud

| Figure | How it must be said | Where | Anchored? |
|---|---|---|---|
| Q1-2026 from memory, $4,400,000 | **"four point four"** (or `4.4`) | `leadership-06`, Guryev | **yes — GF-02** |
| Q1-2026 actual, $3,820,000 | never spoken — it lives in the sheet | finance sheet | no |
| 2025 revenue, $17,175,000 | "seventeen-one-seven-five" — never said aloud | company profile | no |
| Pulse 2.0 v1, $600,000 | **"six hundred K"** / `$600K` / "six hundred thousand" | `leadership-02`, Akhmetov | **yes — GF-03A** |
| Pulse 2.0 v2, $825,000 | **"eight twenty-five"** / `825K` | `sync-04`, Akhmetov | **yes — GF-03B** |
| Pulse 2.0 v3, $1,050,000 | **`$1.05 million`** (the digits `1.05` must appear) | `sync-05`, Akhmetov | **yes — GF-03C** |
| the round, $6,000,000 | **"six million"** / `$17.5` | `board-01`, Valerie Ross | **yes — GF-10A** |
| lead band floor, $160,000/yr | "one-sixty" — **unchanged, not rescaled** | `board-01` | no |
| lead band ceiling, $190,000/yr | **"one-ninety"** / `190K` — **unchanged, not rescaled** | `board-01`, Guryev | **yes — GF-10B** |
| Titan renewal, D-001 | $490,000 — "four ninety" | CRM | no |
| Titan reporting module, D-002 | $320,000 — "three twenty" | `sales-04`, `leadership-06`, `client-titan-4` | no |
| Titan core-banking integration, D-003 | $210,000 — "two ten" | CRM | no |
| Northern House full rollout, D-009 | $410,000 — "four ten" internally, **"four hundred and ten thousand"** on the client call | `sales-04`, `sales-06`, `client-northern-1` | no |

### Figures with no row of their own — see `background_figures`

A handful of numbers are spoken out loud that no sheet and no CRM row produces.
Every one of them is committed in `ground-truth.yaml` under `background_figures`
so that nothing audible is left unsourced — including the one that is
deliberately not true. `background_figures` is the authority; if you put a new
figure in somebody's mouth, add the entry there and a row here, and do not go by
the length of this table:

| Figure | Value | Status |
|---|---|---|
| Q1-2025 revenue | $3,415,000 | spoken as **"about three point four"** in `leadership-06`; Jan–Feb 2025 predate the finance sheet, so it is committed, not derived |
| Helin's FY2026 forecast | $19,000,000 | **CEO optimism on the record, with no sheet behind it — intentional.** The gap against the series is a planted fact, not drift |
| open pipeline at 2026-04-07 | $2,292,500 | arithmetic over the CRM: every deal not closed by that date. `gen_data.py` reconciles it and fails if it drifts |
| Lebedev's February count | 412 | her own hand count of dashboards (arc 7). A count, not money — **not** multiplied |
| Guryev's "seven fifty" | $750,000 | a **rhetorical counterfactual** in `leadership-02`, not a budget version — what he says an unrecorded estimate turns into in two months, and he says on the line that he is inventing it. Foreshadows arc 3 and lands low: the real recount is "eight twenty-five" |
| Titan at risk in March | $530,000 | the sum Reuter and Frolov reach in `sales-04` — D-002 ($320,000) + D-003 ($210,000), spoken as **"five thirty"**. Arithmetic over the CRM, and an *at-risk* total, never a booking |
| the trade-show booth | $28,000 | Kim's aside in `sales-04`, **"twenty-eight K"** — a marketing cost with no CRM row and no line of its own in the finance sheet. Kept off $40,000 deliberately so it cannot be mis-joined to D-019, CareGrid Health's support contract |
| Retail Metrics price gap | 20 % | Reuter's read of the competitor's list price in `sales-04`, **"about twenty percent under us"** — the benchmark the 15 % discount was measured against, which is what makes her "five points" arithmetic (20 − 15). A sales-side estimate, not a sheet fact; a percentage, not money |
| engineering comp gap | 20 % | Guryev's read in `board-01`, **"about twenty percent behind the market"** — the benchmark that justifies taking the lead ceiling from one-sixty to one-ninety. Off the comp benchmarks and the offers the company has lost people to; no sheet, no HR system. Ties to the raise *loosely*: 160 → 190 is +18.75 %, so it closes most of the gap and not all of it — say so on the line (**"doesn't fully close it"**) and the rounding reads as rounding. Same number as the row above, different subject and different speaker — never join the two |
| standard discount ceiling | 10 % | Reuter's standing answer to Northern House procurement in `retro-01`, **"up to ten percent"** — the list ceiling anyone gets for asking, quoted before a negotiation exists. Policy, not a grant: no deal carries a 10 % discount, nothing is agreed with Northern House, and D-009 stays at the full $410,000. Keep it visibly apart from the 15 % Foodline discount it sits next to (D-004, the exception signed *above* this ceiling — that contrast is the point of the scene) and from the 20 % rows above; say on the line that ten is the standard and fifteen was not |

### The full monthly finance series (spoken in `leadership-02`, `leadership-06`)

| Month | Revenue | Costs | | Month | Revenue | Costs |
|---|---|---|---|---|---|---|
| 2025-03 | $1,240,000 | $1,055,000 | | 2025-11 | $1,725,000 | $1,390,000 |
| 2025-04 | $1,305,000 | $1,100,000 | | 2025-12 | $1,900,000 | $1,510,000 |
| 2025-05 | $1,370,000 | $1,130,000 | | 2026-01 | $1,105,000 | $1,245,000 |
| 2025-06 | $1,495,000 | $1,215,000 | | 2026-02 | $1,290,000 | $1,255,000 |
| 2025-07 | $1,360,000 | $1,190,000 | | 2026-03 | $1,425,000 | $1,280,000 |
| 2025-08 | $1,430,000 | $1,205,000 | | 2026-04 | $1,365,000 | $1,310,000 |
| 2025-09 | $1,575,000 | $1,295,000 | | 2026-05 | $1,480,000 | $1,340,000 |
| 2025-10 | $1,600,000 | $1,320,000 | | 2026-06 | $1,560,000 | $1,370,000 |

Q1-2026 = 1,105 + 1,290 + 1,425 = **$3,820,000**. Guryev says **"four point
four"** ($4,400,000). The 15.2 % gap is arc 2, computed by script — never state
the percentage in the text.

Say these aloud as an operator would: "a million two ninety", "a million one oh
five in, almost a million two fifty out". Never "one million two hundred and
ninety thousand".

### The remaining CRM amounts

$255,000 · $115,000 · $380,000 · $95,000 · $70,000 · $155,000 · $45,000 ·
$135,000 · $220,000 · $37,500 · $60,000 · $180,000 · $55,000 · $75,000 ·
$40,000 · $22,500 · $105,000 · $265,000 · $50,000 · $90,000 — see `deals:` in
`ground-truth.yaml`.

---

## File and format rules

**File names keep the date prefix.** The validator maps a file to a meeting by
the first 10 characters of its name and nothing else.

```
transcripts/2026-03-10_leadership-02.txt
transcripts/2026-04-14_leadership-07.txt
```

**Header block** — four lines, then `---`, then a blank line:

```
Meeting: Leadership standup
Date: 2026-03-10
Duration: 8 min
Participants: Andrew Helin, Paul Guryev, Olivia Reuter, Dmitri Tsarev, Timur Akhmetov, Igor Vatulin, Sveta Kim, Vera Dorn, Marina Sokol, Annie Lebedev
---
```

**`Duration` must agree with the timecodes.** It is the last `[MM:SS]` in the
file, rounded up to the minute, plus one. Nothing else is allowed — an earlier
edition carried durations roughly 7× the actual timecodes, which is the first
thing a careful reader checks.

Client calls name the outside party after a semicolon:

```
Participants: Nick Frolov, Olivia Reuter (Helion); Sergey Volkov — SVP, Data & Analytics, Titan Bank
```

**Turns** are `[MM:SS] Full Name: text`, **one turn per line, never wrapped** —
the parser reads to the end of the line and no further. No colon inside a
speaker label. Timestamps are digits only.

---

## Required anchors, by meeting

Each row: who has to say it, the regex that must match **their** lines, and a
canonical English line that satisfies it. Reword freely around the canonical
line — just keep the matching words intact.

### leadership-02 — 2026-03-10 — Leadership standup
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Timur Akhmetov | GF-03A | `six hundred K\|\$?600K\|six hundred thousand` | "It comes out to six hundred K for the whole version." |
| Annie Lebedev | GF-07A | `data mart` | "The regional data marts rebuild overnight, so that number is a day behind." |
| Marina Sokol | GF-06A | `keeping up` | "We're keeping up. It's tight, but we're keeping up." |

*Also here:* Guryev closes February at **"a million two ninety"** ($1,290,000)
against costs of **"a million two fifty-five"**; January was **"a million one oh
five in, almost a million two fifty out"**. He may mention the December bonus —
fine, `leadership-02` has no October 15.

⚠ Guryev's **"seven fifty"** right after the approval is an *invented* number —
his point about what an estimate becomes once nobody writes down where it came
from. Keep it marked as invented in the line itself ("I'm making that number
up"), because it sits between the $600K approved here and the real $825K
recount in `sync-04` and must never read as a fourth budget version. It is
committed as `background_figures.guryev_drift_hypothetical`; if you cut the
figure from the dialogue, drop that entry with it.

### sync-03 — 2026-03-17 — Product and engineering sync
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Marina Sokol | GF-01B | `December` | "It goes into the December release. Not before." |
| Dmitri Tsarev | GF-04A | `\bAPI\b` | "The API access on their side — that's the risk, and nobody owns it." |
| Annie Lebedev | GF-07B | `data mart` | — |

**BANNED in this file:** `October 15 / fifteenth of October`. No owner may be
assigned to Tsarev's risk — that is the arc.

### sales-04 — 2026-03-24 — Sales weekly
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Olivia Reuter | GF-01A | `October 15\|fifteenth of October\|October the fifteenth` | "I talked to him yesterday and I told him — October 15." |
| Sveta Kim | GF-01C | `campaign` | "Then I'm building the autumn campaign around mid-October." |
| Nick Frolov | GF-09A | `15 ?%\|fifteen per ?cent\|15 per ?cent` | "We ended up at fifteen percent." |
| Olivia Reuter | GF-09B | `Retail Metrics` | "Retail Metrics was in the room with a lower number. That's why." |

**BANNED in this file:** `December`. Akhmetov is present and says nothing
about dates — do not let him connect October to December.

### delivery-02 — 2026-03-26 — Delivery review by account
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Kostya Melnik | GF-08 | `billing` | "Their billing runs in three layers, and the exceptions live in the middle one." |
| Annie Lebedev | GF-07C | `data mart` | — |

Melnik's throwaway "I'm not here forever" is the only hint that he leaves.

### leadership-06 — 2026-04-07 — Leadership standup
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Paul Guryev | GF-02 | `four point four\|4\.4` | "Q1 came in around four point four, off the top of my head." |

*Also here, all four backed by `background_figures` or the CRM:* last year's Q1
is **"in the region of three point four"** ($3,415,000); Helin's "which means
**nineteen million** for the year is real" is his forecast and nothing else;
Reuter reads **"two point two nine million in open deals"** off the CRM
($2,292,500, reconciled by `gen_data.py`) and Helin halves it to **"about one
point one five"**; the Titan reporting module is **"the three twenty"**.

⚠ Reuter repeats the Titan date here — she must say **"mid-October"**, never
"October 15" (FC-01). Tsarev tries to raise the risk and is talked over.

### leadership-07 — 2026-04-14 — Leadership standup
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Andrew Helin | GF-05 | `hir(e\|ing) two account executives` | "Fine — we're hiring two account executives. End of May." |

> **This is the only file in the corpus allowed to contain the words "account
> executive".** LF-05. No owner is assigned to the search; Vera is on vacation.

### sync-04 — 2026-04-21 — Product and engineering sync
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Timur Akhmetov | GF-03B | `eight twenty-five\|\b825K?\b` | "The recount comes to eight twenty-five. $825K. I ran it twice." |
| Dmitri Tsarev | GF-04B | `the same thing I raised in March` | "This is the same thing I raised in March." |
| Marina Sokol | GF-06B | `(finish\|wrap).{0,25}over the weekend` | "I'll finish it over the weekend." |

### sales-06 — 2026-04-28 — Sales weekly
No anchors. Titan API access still not granted; the Foodline franchise deal
slips to "at risk"; Northern House signatory still undecided.

### delivery-03 — 2026-04-30 — Delivery review by account
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Igor Vatulin | GF-08B | `it.{0,1}s not there` | "The ticket says in progress. I opened it. It's not there." |

Use the contraction — *"it is not there"* does not match.

### retro-01 — 2026-05-19 — "Retro: the Foodline franchise deal"
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Olivia Reuter | GF-09C | `margin we gave away` | "That's margin we gave away for nothing." |

The title must keep the word **retro**.

Reuter's closing aside carries the one figure in this file that no CRM row
produces: the **"up to ten percent"** she quotes Northern House procurement,
committed as `background_figures.standard_discount_ceiling_pct`. It is the
standard list ceiling, not a discount anybody was given — write it so it cannot
be read as a second Foodline number: say it is what anyone gets for asking, that
nothing is agreed with Northern House, and that the fifteen on Foodline was the
exception. If you cut the figure from the dialogue, drop that entry with it.

### sync-05 — 2026-06-02 — Product and engineering sync
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Timur Akhmetov | GF-03C | `1\.05\|million-oh-five` | "Another two twenty-five on top. $1.05 million all in — and the reason is different this time." (the digits **1.05** must be on the page) |
| Dmitri Tsarev | GF-04C | `I raised in March and .{0,25}in April` | "It's the same thing I raised in March and what I said again in April." |

Tsarev's two lines are one running formula — **"the same thing I raised in
March"** in April, extended in June with **"and what I said again in April"**.
Keep that wording in both files; it is what makes the three mentions one thread.

### client-northern-1 — 2026-06-03 — Client call, Northern House
No anchors. Three hard bans in this one file:
* the launch date is **never fixed** — and never call it a **delivery date**;
* the signatory is **never named** — **Demidov** appears nowhere;
* price confirmed verbally: **four hundred and ten thousand** — the long form
  here, because it is a price being confirmed to a client; internally the same
  deal is "four ten".

Rich material to keep: 15 projects, 7 regions, 2-3 weeks of manual
reconciliation, the client asks for the proposal by email.

### board-01 — 2026-06-05 — Closed board session
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Valerie Ross | GF-10A | `six million\|\$?17\.5` | "The round is six million, and the terms are basically agreed." |
| Paul Guryev | GF-10B | `\b190K?\b\|one-ninety` | "We take the ceiling at lead from one-sixty to one-ninety." |

The only `access: board` meeting in the corpus. Tsarev names Sokol as the
reason for the raise. Hiring account executives is **not** mentioned.

⚠ Guryev's **"about twenty percent behind the market"** is the justification for
the raise, and it is committed as `background_figures.engineering_comp_market_gap_pct`
— keep it marked *on the line* as his own read ("that's my read off the comp
benchmarks, not a science"), because no sheet and no HR system produces it. It
matches the band only by rounding: one-sixty → one-ninety is +18.75 %, so he
adds **"doesn't fully close it"** and the two figures corroborate loosely
instead of pretending to be exact. Do not derive either one from the other, and
do not connect it to Reuter's identical-looking 20 % in `sales-04` — that one is
a competitor's list price.

### leadership-09 — 2026-06-09 — Leadership standup
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Marina Sokol | GF-06C | `not keeping up with everything` | "Honestly — I'm not keeping up with everything." |
| Vera Dorn | GF-06D | `gone quiet lately` | "Marina's gone quiet lately. She used to be the one talking." |

Vera says it once, in passing, and nothing happens after it. Hiring is not
mentioned at all — not even now that Vera, its natural owner, is back.

### client-titan-1 — 2026-03-09
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14A | `look(ing)? elsewhere` | "If it doesn't land this year, we'll start looking elsewhere." |

### client-titan-2 — 2026-03-23
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14B | `falls out of the budget` | "Without a date before the May committee it falls out of the budget entirely." |

This is where **October 15** is actually promised — so **no "December"** in
this file. Volkov also wants written confirmation and a demo on their own data.

### client-titan-3 — 2026-04-02
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14C | `request in January` | "Three approvals on our side. I filed the request in January myself." |

### client-titan-4 — 2026-04-24
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14D | `break (this\|it) (up )?into (stages\|phases\|milestones)` | "Then let's break this into phases, with a payment on each one." |
| Sergey Volkov | GF-14E | `penalty clause` | "And the SLA goes into the contract with a penalty clause." |

Contains **October 15** (milestone three) → **no "December"** here either. He
offers symmetry himself: if access slips on his side, the deadline slips too.

### client-titan-5 — 2026-05-12
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14F | `Analytics Plus` | "Analytics Plus is telling me three months, not seven." |

### client-titan-6 — 2026-06-11
| Speaker | Id | Regex | Canonical line |
|---|---|---|---|
| Sergey Volkov | GF-14G | `third approval` | "The third approval is still sitting there. Committee's in two weeks." |

---

## The client-call boundary (CB-01)

All seven client calls — `client-titan-1..6` and `client-northern-1` — must
stay clean of five internal facts. This is the same access boundary the
product sells, tested on the text:

| Must not appear in any client call | Comes from |
|---|---|
| `account executive(s)` | LF-05 — the hiring decision |
| `gone quiet lately` | GF-06D — the remark about Sokol |
| `six million` / `17.5` | GF-10A — the round |
| `190K` / `one-ninety` | GF-10B — the salary band |
| `1.05` / `million-oh-five` | GF-03C — the final budget |

Deal amounts ($490,000 / $320,000 / $210,000 / $410,000) are fine in client
calls — those are the client's own numbers.

---

## Voice — this must read as a native English meeting

It is a localization, not a translation. Nobody should be able to tell it came
from Russian.

* **Contractions everywhere.** "We're", "it's", "doesn't", "I'll". Full forms
  only when someone is being emphatic.
* **People interrupt.** Cut a turn mid-sentence, let the next speaker take it
  over, let someone say "sorry, go ahead".
* **Filler is required.** "look", "honestly", "I mean", "hang on", "right".
  15-20 % of every transcript should be off-topic: the office move, the
  conference, the CRM losing attachments again, someone's audio.
* **People talk past each other.** In sales-04 Akhmetov is in the room while
  October is agreed and simply does not connect it to December. Do not have
  anyone "helpfully" notice things — the whole corpus is built on nobody
  noticing.
* **No translationese.** Drop "as for", "in the given case", "it is necessary
  to note". Nobody says "colleagues" to open a standup; they say "okay, are we
  all here?".
* Per-person voice: Helin fast and jumpy; Reuter confident, speaks in
  statements; Tsarev short and quiet and never pushes; Sokol detailed in March
  and visibly shorter by June; Guryev precise, talks in numbers; Vatulin
  steady; Lebedev direct and unhedged about data; Melnik explains at length
  with examples; Volkov direct, no filler, not hostile — and speaking as the
  person who owns the module's outcome, not as a buyer haggling on price.

---

## Before you hand a file in

```bash
grep -inE 'churn|cohort|Demidov|Демидов|delivery date' transcripts/*.txt  # must be empty
grep -inE 'account executive' transcripts/*.txt                          # leadership-07 only, once
python3 scripts/gen_data.py                                              # both reconciliations OK
python3 scripts/validate_corpus.py                                       # must end GREEN
```

Run the greps against `transcripts/` — **never** against the folder root. This
file, `SPEC.md`, `README.md` and `STATUS.md` all name the banned strings on
purpose, and a root-level grep (or a root-level ingest) will find them.
