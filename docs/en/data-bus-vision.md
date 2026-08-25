# The data bus: what works today and where it leads

The bus is the **company's reception desk for machines**: an external
system, somebody else's AI agent or another company gets a key and exactly
its own slice of memory — not a file but live answers it can work on. The
factual current state is in the
[capability map, §13](product-capabilities.md); this document is about
**why** the bus exists and **which processes it opens up**. Everything
marked "direction" is designed mechanics, not a shipping feature: we keep
those two things strictly apart.

## What works today

- **Key and slice.** An external party gets a key and exactly its slice of
  memory; the slice, fact-type blocking and redaction are applied **before
  the model** — the model physically does not see what is forbidden.
- **The key is a managed asset.** Shown once, has a lifetime, a quota and a
  journal, revoked with one click. A contractor leaves — access is closed.
- **The journal as business intelligence.** You can see what an investor
  asks before a round, and what a contractor keeps asking on day three.
- **Preview through the consumer's eyes.** A test query on their behalf
  before the key is issued: how many facts matched, how many passed the
  filter, how many places were redacted.
- **Reverse flow with an unforgeable origin mark.** The external party can
  send facts in; the server stamps the source — the base always shows
  which fact is yours and which came from outside.
- **Agent-to-agent dialogue.** An external agent asks in words and gets a
  coherent answer under the same policy and the same audit.
- **Notifications without data.** A signed "something new" push carries
  only the event type and counters — the consumer comes for the data
  itself, through its own slice.
- **A link between two organizations as a relationship.** A handshake, each
  side with its own slice, a one-sided break — today inside one
  deployment.
- **Executor agents.** An agent registers on its channel, takes tasks, and
  delivers results under three-outcome machine acceptance.

## Why

What passes between companies today is not meaning but its compressed
residue: a presentation instead of reality, a brief instead of context, a
CV instead of a person. A contractor spends a month getting up to speed; a
consultant spends weeks collecting what already sits in the message
threads. The bus returns what compression loses: the external party gets
**context, not a retelling** — within the boundaries you set. And once
context is machine-readable, machines can not only read it but **work**
on it.

## Direction 1. External agents run by the brain

Today automations live inside the system and executors live on channels of
one deployment. The direction is the same contracts for agents living
**anywhere**: the brain assigns a task to an external agent, hands it a
work permit — exactly the slice of context the task needs — accepts the
result through machine acceptance, and writes the outcome back into
company memory.

Out of this grow **agent chains outside the company**: a pipeline of
external executors — a contractor's bot, a specialist service, another
company's corporate agent — with the brain as the orchestrator: it knows
the task's context, cuts each participant its own slice, and checks the
results against each other and against memory. Automation stops ending at
the company's boundary — while the access boundary stays sharp: every link
has its own key, its own journal and its own acceptance.

## Direction 2. The brain as a source for other systems

The symmetric move: not an agent doing work for the brain, but somebody
else's system coming to the brain for context. An HR platform, a CRM, an
analytics tool connects and pulls not an export but live answers — "what
did this person actually do across projects this quarter", "which
agreements with this client are in force" — within its role and under the
journal.

Employee data is the most sensitive thing here, so the direction inherits
the rules that already operate instead of inventing new ones: a
restriction level on every fact, the slice applied before the model, an
access journal visible to the profile's owner, GDPR deletion — and the
principle that **a person's profile belongs to the person**: you can take
your own profile with you (capability map, §3).

## Direction 3. A network of brains and HR processes at the intersection

A link between two organizations already works inside one deployment; the
direction is federation across deployments. Once company brains are
connected by relationships, the **intersection** of their data about
people enables processes that could not exist before — because the data
lived in separate companies, in the shape of CVs:

- **A confirmed experience profile instead of a CV.** A person's profile
  is built from real work, not self-description. On leaving, the employee
  takes their profile along and decides what to open to the next
  employer. Hiring against confirmed experience.
- **An interview with the profile — with the candidate's consent.**
  Questions about how the person actually solves problems — before the
  first call, over the part of the profile they chose to open.
- **Continuity between companies.** When a person moves, loose ends,
  context and standing agreements transfer as a slice, not in someone's
  head.
- **Expertise search across the network.** "Who has already solved this
  kind of problem" — answered as an aggregate, nothing extra revealed;
  contact is established with both sides' consent.
- **Aggregate HR analytics.** Load, dynamics and practices computed at the
  intersection of companies; only aggregates leave, never individual
  facts.

Every item inherits the system's DNA: the boundary before the model, the
owner's consent, the journal, one-click revocation. Without that DNA such
a network cannot be built — which is exactly why it cannot be built on top
of ordinary search or file exports.

## Honest status

| Capability | Status |
|---|---|
| Key, slice before the model, journal, preview, revocation | works |
| Agent-to-agent dialogue (question → coherent answer) | works |
| Executor agents with machine acceptance | works, inside one deployment |
| Two-organization link (handshake, slices, break) | works, inside one deployment |
| Secrecy ceiling as a separate sieve on the main path | does not yet fire — not sold as a boundary |
| Tamper protection for the bus's own journal | not yet |
| Agent negotiation about *how* to do the work (dialogue, not task → result) | none |
| Discovery between deployments, federation | direction |
| External agent chains orchestrated by the brain | direction |
| HR processes at the intersection of companies | direction; requires federation and a consent model |

This is road logic, not a promise of dates: the directions describe where
the **already-working contracts** lead if extended past the boundary of a
single deployment.
