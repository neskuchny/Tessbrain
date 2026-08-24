# Tessbrain — what the company brain does

> A business map of the product: what each part does, why a company needs it,
> with concrete examples. **Compiled from the code, not from plans** — anything
> found only in a roadmap is moved to the "Honest limits" section.

A company talks about itself every day — in meetings, in chats, in documents.
Normally that stream disappears: it stays in people's heads, in message
threads, and in files nobody will open again. **Tessbrain turns a company's
conversations into its memory, and that memory into work:** it answers
questions with links to the original source, computes figures in code, drafts
documents, runs processes, and notices on its own where the company has drifted
out of sync.

This document describes what **runs in code**. Capabilities behind flags, or
existing only in plans, are collected in a separate section at the end and
flagged inline — because a product that promises more than it does destroys
trust at the first demo.

## 1. How the whole thing works

One loop, four stages. Everything else in this document is detail inside it.

| Stage | What happens |
|---|---|
| Intake | Meetings, calls, Telegram and Slack, documents, links, CSV/Excel exports, CRM (amoCRM, Bitrix24, HubSpot, Pipedrive), 1C via OData, Google Sheets, BI. Nothing has to be entered by hand — the system consumes what is happening anyway. |
| Extraction | About thirty specialised extractors each pull their own layer out of a meeting: decisions, tasks, participants, KPIs, risks, ideas, opinions, causal chains, contradictions, outcomes. Every fact carries a source, a date and a confidence. How many model calls this costs depends on the model class: on a strong model the whole base pass is a single call, on a cheaper one it is split. |
| Memory | The company graph: typed entities and typed relationships. Facts are versioned, not overwritten. Overnight the system tidies itself: merges duplicates, weakens unconfirmed material, collects insights. |
| Work | Chat, documents, process boards, reports, simulations, controlled feeds to partners, and a connection into Claude Code and Cursor as a context source. |

- **~30** knowledge extractors per meeting
- **31** ready-made process templates out of the box
- **23** tools for external AI assistants over MCP

> **Why this matters.** **The model is a consumable; the asset is what the
> company has accumulated.** Company memory, playbooks, data ontology, skills
> and configured processes are not tied to any single AI vendor. The model
> underneath can be swapped — for another provider, for your own servers, for a
> local model inside a closed perimeter (section 14) — and everything
> accumulated stays entirely yours. This is the answer to the main fear in
> adoption: *"we will invest, and in a year the vendor raises the price or shuts
> down."* The performer changes, not the asset. The opposite arrangement — where
> company knowledge lives inside somebody else's assistant — means that when the
> vendor leaves, the accumulated knowledge leaves too.

## 1.1. What feeds the system and where it sends results

The company brain does not work in a vacuum: it sits inside an ecosystem where
each service contributes its own kind of conversation. This is not
"integrations by the list" but a division of labour — capture and recognition
live elsewhere, understanding and memory live here.

**Meetings — from MeetFlow** — Our own meeting-processing system: it records
and transcribes, the brain takes the finished material and breaks it into
meaning. A trusted internal source, not a third-party service bolted on.

**Calls — from a call analytics service** — The domain of client communication
is deliberately separated from internal memory: transcripts and audio are
**not copied to us**. Events arrive — deal at risk, complaint, coaching
recommendation — and the live client card is pulled from their system at
display time. Conclusions their model produced are marked as *model narrative*
and never become company facts: protection against one AI system feeding
another its guesses.

**Messenger — Mini Tess** — A Telegram bot as a separate entry point: it
delivers post-meeting artifacts, answers from company memory, and holds a
conversation with an employee (section 11).

**Data — from where it already lives** — CRM, 1C via OData, Google Sheets, BI,
file exports, links. Plus external parties who push facts in over the bus
(section 13).

**Back outward** — Writes to CRM (create a deal or lead, append a comment) on a
separate path, isolated from reading and closed by default; delivery to Slack,
email, Notion, Jira and other connected systems through board blocks; feeds to
partners over the bus; "something new appeared" notifications for subscribers.

## 1.2. How synchronisation works: what is pulled and what changes

Three different flows that are easy to confuse — they concern different things.

**First: meetings → memory.** Knowledge sync reads meetings from the source and
runs them through extraction. It runs in the background with visible progress,
and chat knows about it — during a run it honestly warns "sync in progress,
data is incomplete" rather than answering from half the base as if that were
all of it.

**Second: sources → company tables.** A dataset connected by link or to a CRM
refreshes on the schedule its owner set. The fetch is always full — sources
like Google Sheets do not offer a delta at all. But the system compares what it
fetched against what is stored: **unchanged means nothing is rewritten**, no
model spend on rebuilding the ontology, no empty version in history. Changed
means the new data becomes current, the previous version moves into history
(5 kept, with rollback), metrics recompute and subscribers are notified.

**Third: memory → outward.** Changes the system makes in other systems: CRM
writes, document delivery, tasks in trackers. Here the principle is the
opposite of intake — money is locked by default: the CRM write path is off
until deliberately enabled.

**Which flow changes what — the short answer.** Company memory changes only
through extraction and overnight tidying. Data tables change through source
refresh. External systems change only through explicit process actions. No flow
writes into another's territory: a data source cannot alter a fact from a
meeting, and meeting extraction does not touch data tables.

**Different models for different work** — Each workload gets its own model
class, chosen by the rule "sensitivity to quality x frequency". Heavy and rare
work that sets the quality of the whole knowledge base (meeting extraction,
overnight consolidation, spec generation, final synthesis of a deep search)
runs on a strong model. Frequent and interactive work (chat, ordinary search,
intermediate steps) runs on a smaller one, where speed and price matter. The
company chooses the provider and the key (section 14); the system chooses the
tier per task.

## 2. Graph memory — the foundation of everything

Not notes and not document search, but a model of the company: people,
projects, clients, decisions, tasks, metrics and the relationships between
them. It grows out of conversation on its own.

> **Why this matters.** A person leaves and the context leaves with them. "I
> think we decided this, but nobody remembers when or why." And third: an
> ordinary AI cannot be trusted, because it answers confidently and without a
> source — and an answer like that cannot be taken to a board meeting.

**A fact's history, not an overwrite** — A project budget, a person's role and
department, a task's deadline and assignee, a project's status are kept as a
chain of dated versions. A new value *does not erase* the old one — it marks it
superseded, with the date and the source meeting. You see not only "what is it
now" but "what was it and when did it change". Amounts are normalised to
numbers, so "2M RUB" and "2,500,000 rub" from different meetings are comparable
— growth shows as a figure, not as an impression.

**Query "how it was on a date"** — The state of the company on 15 March, on
1 May and today — three slices side by side with an automatically computed
difference: who appeared, what changed, what disappeared. Retrospective without
archaeology through message threads. A version is dated by the **meeting date**,
not the upload date, so an archive loaded in one day does not collapse into a
single point.

**Typed forgetting** — Contextual links ("mentioned near each other") weaken
over time, while structural facts — decided, assigned, changed role — never
decay. Memory that forgets nothing bankrupts its owner; untyped forgetting
would eat the org chart.

## 2.1. Cards: the system makes sense of what it stored, in advance

Between raw facts and an answer sits a layer that thinks ahead of the question.

> **Why this matters.** Assembling a picture of a client or a project at the
> moment of asking is slow and expensive. Prepared cards make the frequent case
> instant, and leave the model to do what only it can.

## 2.2. The system understands what it does not know

**Honest "unknown" instead of a plausible answer** — When memory has no basis
for an answer, the system says so instead of producing something that sounds
right. This is the property that makes an answer presentable.

## 3. Digital profiles of people

A profile of a person built from how they actually participated: role,
projects, decisions, opinions, manner of speech — from meetings, not from a
questionnaire.

> **Why this matters.** What a person actually does and what their job title
> says diverge in every company. The profile is built from behaviour.

## 4. HR 2.0: everything about a person without questionnaires

Competencies, load, growth and risk of burnout — inferred from participation
in real work rather than from a self-assessment form.

## 4.1. The system watches how people work together

Who works with whom, where communication breaks, which pairs produce results
and which stall.

## 5. Simulations: clients, partners, a board meeting

Run an offer, a negotiation or a contested decision *before* money and time are
spent.

> **Why this matters.** Marketing tests its messages with budget, and decisions
> are discussed by the same circle of people with the same blind spots. A
> simulation gives cheap preliminary screening — and honestly says when it is
> too early to believe it.

**Simulating real clients** — The counterpart is not an invented avatar but a
*real client from the company graph*: a dossier assembled from node properties,
relationships and the cards of linked people (section 2.1). You can talk to it
and run an offer past it.

**Segments by real attributes** — Clients are grouped by actual attributes
(industry, status, category). Clients without attributes honestly land in
"no attributes" rather than being distributed into invented groups.

**A board meeting of roles and profiles** — A multi-round discussion of a
question: abstract roles (CEO, CFO, CTO) and profiles of real employees at one
table, with a record of disagreements and a check of the conclusion against
company data.

**A reaction panel plus a sceptic** — Personas answer in their own language,
with an adversary set over them. In a panel of field personas the adversary
attacks the messages.

## 6. Numbers: ontology and metrics

The governing principle: **code computes, not the language model.** The model
turns a question into a structured plan, the plan executes deterministically
and is visible in the API response. If a question does not fit a plan, a
fallback engages — the model writes the computation as code, which runs in a
sandbox; the answer then arrives together with the code, so the logic can be
checked.

> **Why this matters.** "The report says one number, the CRM another, and
> somebody quoted a third at the meeting." And "you cannot trust an AI
> assistant with figures" — because it does arithmetic in its head and is
> confidently wrong.

**A number from a meeting and a number from a system are one object** —
"Revenue 4.2M", said at a standup, and a column from 1C become one metric with
a shared history. The summary reads: *"meetings said X, the data says Y — a 17%
discrepancy"*.

**A plan instead of the model's arithmetic** — Question → structured plan
{operation, column, grouping, filters} → deterministic execution. When the
model is unavailable a built-in parser handles common questions ("sum of X by
month", "top N by Y") — figures are computed even without an LLM.

**A passport for every number** — Coverage (how many rows were counted), how
many were skipped, and why.

## 7. Chats: a conversational way into memory

Three different chats for three different jobs — not one window with a mode
switch.

| Chat | What it does | When you need it |
|---|---|---|
| **Over company memory** | Searches the whole memory three ways at once (meaning, exact terms, graph relationships) and synthesises an answer with a list of sources. Three channels are a necessity, not belt-and-braces: semantic search has a proven mathematical ceiling (ICLR'26) — a single vector cannot return every combination of documents, and the best models fail on enumerative queries. So enumerative questions — "show everyone who…", "how many…", "who worked on both A and B" — are recognised and weight the graph channel, where enumeration and intersection are exact by construction | "What did we decide about client X?", "Show everyone who worked with both A and B" |
| **Over meetings and documents** | No base-wide search at all: you choose the materials yourself, and the answer comes strictly from them | Reviewing a contract, preparing for a call about one client |
| **Product guide** | Explains how to use the product from curated help. **Deliberately blind to company memory** — no meetings, no graph | "How do I set up a board?" |

## 8. Boards: automations and visual reports

**A board is a diagram that works.** You lay blocks on a canvas and connect
them with arrows: "when this happens → ask the company brain that → make a
document → send it there". The diagram is not a picture for a presentation; it
*is* the automation: save it and it runs on a schedule or on an event.

There are two kinds, and they mix on one canvas:

| Kind | Assembled from | What you get |
|---|---|---|
| **Process** | trigger, data sources (meetings, CRM, web), documents, conditions, delivery | Automation: do it and send it without a human |
| **Creative** | references, prompts, image and text generation | A presentation, ad creatives, a storyboard, a landing page in blocks |

Mixed boards are the ordinary case, not a trick: "after a meeting collect the
results → draw a picture from them → send it to chat" is one board whose first
half is process and second half creative.

> **Why this matters.** "Everything depends on somebody not forgetting."
> "Reports get written but nobody reads them." "Automation takes months through
> a contractor" — here it is assembled by the person who needs it, and it runs
> on the company's own data.

## 8.1. Documents: a separate tool, not an attachment to boards

Document generation lives in its own section and works on its own. Boards
simply know how to call it — another entrance to the same module, not its
owner.

> **Why this matters.** A commercial proposal gets assembled by copy-paste from
> a previous one, carrying somebody else's details and old prices. Here the
> document is assembled from company memory, and anything missing is honestly
> flagged rather than invented.

**A document from a meeting** — A proposal, a contract, a client brief or a
free-form document: the system takes the meeting material and the relationship
history, assembles the text and returns a finished Word or PDF file on
letterhead.

**Your own templates with substitution** — Upload your own DOCX with markers
and the system fills them from memory and company details. The key part is that
**every field is marked**: green for what was found in the data, yellow for what
was inferred, red for what is missing. Not "your document is ready" but "your
document is ready, check these three fields".

**Playbooks from what accumulated** — The system finds repeated patterns in
memory and assembles them into rules.

## 9. Company experience as an asset

The system remembers not only *what* was decided but *how it turned out* — and
next time says so in advance.

> **Why this matters.** This is the only layer that answers an investor's
> question, "what accumulates here that cannot be bought?" Company experience
> usually lives in three people's heads and leaves with them.

> ⚠️ **Honestly about the maturity of this section.** The layer fills
> automatically as each meeting is processed (decisions from all of them,
> outcomes from retrospectives) and is mixed into chat and spec generation.
> What is missing: its own screen in the interface (patterns are visible only
> through the API) and any quality measurement — the accuracy of situation
> classification and outcome linking has not been measured.

**A decision as a type of situation** — "A competitor cut prices by 15%" becomes
"competitor price pressure", classified by business tensions: growth versus
margin, speed versus quality, in-house versus contractor. The tension reference
is closed — anything that does not fit is honestly marked "other" rather than
forced into the nearest match.

**Linking to a real outcome** — Retrospectives are mined for how things ended,
and confidence rises only from **confirmed outcomes**, decaying with time. Ten
discussions whose fate is unknown produce *low* confidence, not high —
otherwise the number would mean "we talked about this a lot" while reading as
"we verified this".

## 10. SIMA: turning an idea into a spec an AI will build correctly

**What it is.** A separate loop for the small products, services and
automations a company needs for itself. The point is not to draw a diagram but
to produce a **very detailed and correct technical specification grounded in
company data** — one you can hand to an external AI to implement and then verify
that it built exactly that.

The goal the loop works towards: an employee who **cannot program** conceives a
mini-service they or their colleagues need, brings it in SIMA to a coherent
specification, hands it to an AI executor, and the system watches the result.

> **Why this matters.** Asking an AI to "build me a service" in one sentence
> gets you the wrong thing. Meaning is lost between "we thought of it" and "it
> was built": one person writes the spec, another reads it, a third checks it.
> Here the spec is assembled piece by piece, each piece grounded in real company
> data, and acceptance is not an opinion but the result of checks.

**Acceptance with three outcomes** — Passed, failed, and **not proven**. The
third exists because "no evidence" is not the same as "done".

## 11. Mini Tess — an employee's personal assistant

**What it is.** A separate product in the ecosystem: a Telegram bot with its own
memory of a person. It brings an employee what concerns them personally after
meetings, remembers how to talk to them, reminds them about tasks, and lets them
ask **the company brain without opening the web interface** — just by writing in
a messenger.

> **Why this matters.** Corporate systems die wherever you have to log into
> them. A bot lives in the messenger a person already looks at a hundred times a
> day. And, more importantly, it speaks to each person differently, because it
> knows how that person thinks.

**An answer from company memory right in the messenger** — "What did we decide
about client N?" and the answer arrives from the whole company memory with
source links, using the same search as the web interface.

**Personal follow-up — the "coffee scenario"** — The name is literal: while a
person walks from the meeting room to their desk, the system has already
prepared drafts for them — matched to their role in *that meeting*. A strategic
summary and questions for the founder, task cards for the project lead, a
technical specification ready to hand off for the developer, a draft letter for
sales. The role is determined by how the person participated, not by a line in
the staffing table. Not a generic "meeting minutes" mailout but work already
started for you: you sit down not to an empty screen but to a draft that needs
correcting.

## 12. For the executive: the system watches the company itself

Not a dashboard you have to open, but an observer that notices and speaks
first.

> **Why this matters.** An executive learns about a problem last — usually once
> it has already cost money. This layer exists to speak before they find out on
> their own.

**Desynchronisation is the main hidden loss of efficiency, and it exists almost
everywhere.** Departments do not tell each other important things, do not pass
data along, and understand the same words differently. The company pushes on one
part of the system and it breaks in another: sales accelerates the pipeline and
support drowns; marketing promises a date production has never heard of. Nobody
is individually at fault — connectivity is broken.

**A synchronisation index** — One number showing how far the company is moving
in one direction, and above all *where* it diverges. The check runs at four
levels: inside a department, between departments, company goals against
department goals, and executives against each other. The index keeps history, so
you see a trend rather than a snapshot.

**Honest "unknown" instead of a pretty figure** — A level with no data does not
enter the index. Emptiness does not become perfect synchronisation — which is
the first thing metrics like this usually get wrong.

## 13. Data bus: groundwork for external agents and cross-company exchange

**What it is.** Not simply "send a partner an export". This is the **company's
reception desk for machines**: an external system, somebody else's AI agent or
another company gets a key and **exactly its own slice** of memory — not a file
but live answers. And, more importantly, the ability to **work** on those
answers: understand the context and do a task without taking people's time.

> **Why this matters.** What passes between companies today is not meaning but
> its compressed residue: a presentation instead of reality, a brief instead of
> context, a CV instead of a person. A contractor spends a month getting up to
> speed, a consultant spends weeks collecting what is already in the message
> threads, a new partner hears an edited version. The bus returns what
> compression loses: the external party gets **context, not a retelling** —
> within the boundaries you set.

**The boundary runs before the model** — The slice, the blocking of fact types
and the redaction are applied to the material *before* an answer is composed.
The model physically does not see what is forbidden — more reliable than "we
asked the assistant not to talk about salaries".

**Redaction sieves** — Forbidden fact types → allowed fields inside a fact →
replacing amounts with "[AMOUNT HIDDEN]", emails and phone numbers. The number
of redactions is returned in the response. The real protection here is the
folder slice and type blocking: a secrecy ceiling as a separate sieve on the
main path does not yet fire, so it cannot be sold as a boundary.

## 14. Enterprise: access and infrastructure

The things that usually stop an adoption at the security department.

**A classification level on every fact** — Not on a folder but on an individual
graph node: a scale from "visible to all" to "CEO only". It works with the
caveats in "Honest limits": classification filtering applies on the file graph
backend, and the classification itself has to be set manually — everything
defaults to "internal". The scenario "the M&A discussion will not surface for
an intern" requires both.

**The filter works inside the AI's answer** — Not only in the interface: on
supported configurations, forbidden material is cut before synthesis, so the
model cannot let slip what it never saw.

**Isolation at the database level** — Row-level security policies: even a bug in
application code will not let one client see another's data. An argument for a
security officer, not a marketing claim.

**An audit trail that cannot be cleaned up** — The main audit log is protected
from modification and deletion by a database trigger — compliance by
construction rather than by promise. Plus a separate log of sensitive *reads*
(profiles and confidential nodes): a leak more often looks like a read than a
change. The data bus's own log does not yet have this protection.

**An organisation tree and holdings** — One person in several organisations with
switching, a holding above corporations, custom roles on top of built-in ones, a
"Chinese wall" between companies in a group.

**A closed perimeter** — The model can run on your own servers. "Internal" has a
single definition for the whole product, checked both at boot and at call time.

## 15. Evidence instead of promises

The section where "we are better" is replaced by numbers — including the ones
that are not in our favour. Otherwise it would not be worth having.

**Runs on public sets** — LongMemEval (the oracle variant and the variant with
distractor sessions), LoCoMo, HotpotQA, RAGTruth, TruthfulQA, QMSum. Accepted
public sets rather than our own convenient examples; data is downloaded from
public sources, and the judge and model are recorded in every result.

**Where we win — measured.** The difference comes from how we *assemble and
present* memory, not from the mere fact of having it:

| What was compared | Baseline | Ours | Sample |
|---|---|---|---|
| HotpotQA — multi-hop questions | strict answer from fragments 0.57 | analysis mode 0.77 | 30 |
| LoCoMo — multi-session memory | 0.43 | product path with date resolution 0.57 | 30 |
| Hard LongMemEval questions | "dump the whole history" 0.25 | analysis mode 0.71 | 24 |
| Time-based questions | ranking without time 0.17 | temporal module 0.40 | 30 |
| Questions spanning many conversations | retrieval 0.23 | sessions with hits in full 0.57 | 30 |

**Where we do not win — also measured.** Our hybrid retrieval (text + meaning +
rank fusion) draws level with plain BM25 on LongMemEval: 0.49 against 0.50 on a
sample of 200, and 0.675 against 0.675 on 40. Against "no memory at all" the gap
is enormous (0.065); against solid text search, there is none.

**How to read these numbers.** Sample sizes are 20 to 200 questions. This is
honest measurement, not statistics — a difference of two or three hundredths on
thirty questions cannot be taken seriously, and we do not take it so.

## 16. Honest limits

This is part of the product, not fine print. A product that promises more than
it delivers loses trust at the first demo.

**Off by default — enabled deliberately**

- **Company experience (section 9) has no screen of its own.** The layer fills
  as meetings are processed and mixes into chat and specs, but accumulated
  patterns can only be seen through the API. A retrospective is recognised by
  the meeting title ("retro", "review", "postmortem") — meetings with other
  titles yield no outcomes.
- **An outcome is taken from what was said at a retrospective, not from a
  metric.** The system links a decision to what people said about it. There is
  no external verification against a figure from a system — worth keeping in
  mind when reading "three of four cases closed successfully".
- **The quality of the experience layer has not been measured.** Unlike other
  parts of the product, it has neither a benchmark nor output-quality tests —
  only tests that the formulas compute correctly.
- **Classification is not assigned automatically.** Everything defaults to
  "internal"; a higher level has to be set by hand.
- **How much quality a closed perimeter costs has not been measured.** All our
  numbers come from cloud models. The harness for measuring on your own model is
  ready; the measurement itself has not been run.
- **There is no agent-to-agent negotiation.** An external executor takes a task
  and returns a result under machine-checked acceptance — request/response, not
  a dialogue about how to do the work.
- **Cross-deployment company discovery is not built.**
- **Dependencies are pinned with `>=`, not `==`,** and there is no Python
  lockfile, so the audited version is not guaranteed to be the installed one.
