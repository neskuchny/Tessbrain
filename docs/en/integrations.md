# Integrations

Where material comes in from, and where results go back out.

`backend/core/integrations/` — modular: each integration subclasses
`BaseIntegration` and self-registers, so adding one does not touch a central
list.

---

## Division of labour

The system deliberately does not do everything. Capture and recognition live
upstream; **understanding and memory live here**.

- **Meetings** arrive already recorded and transcribed.
- **Calls** stay in the calling system. Transcripts and audio are *not*
  copied over — events arrive, and a client card is pulled at display time.
  Conclusions that upstream system's model produced are marked as *model
  narrative* and do not become company facts. That last part is deliberate: one
  AI system feeding another its guesses is how confident nonsense compounds.

## Inbound

| Source | Notes |
|---|---|
| Meeting transcripts | primary material |
| Telegram, Slack | conversations and a chat entry point |
| Documents and links | absorbed on the same footing as meetings |
| CRM | amoCRM, Bitrix24, HubSpot, Pipedrive |
| 1C | via OData |
| Google Sheets, BI, CSV/Excel | structured data |
| Partners | facts pushed in over the [data bus](data-bus.md) |

### How refresh works

Data sources refresh on a schedule set by their owner. The fetch is always
full — sources like Google Sheets do not offer a reliable delta — but the
result is **compared against what is stored**:

- unchanged → nothing is rewritten, no model spend, no empty version in history
- changed → new data becomes current, the previous version moves into history
  (last 5 kept, with rollback), metrics recompute, subscribers are notified

## Outbound

| Target | Notes |
|---|---|
| Slack, email, Notion, Jira and others | via board blocks |
| CRM writes | create a deal or lead, append a comment |
| Partners | scoped feeds over the data bus |
| Assistants | Claude Code, Cursor and others over [MCP](mcp.md) |

> **CRM writing is closed by default.** The write path is isolated from the
> read path and stays off until switched on deliberately. Reads are cheap to
> get wrong; writes into someone's sales pipeline are not.

## Which flow changes what

Three flows are easy to confuse, and they touch different things:

| Flow | Changes |
|---|---|
| Meetings → memory | company memory, via extraction and nightly consolidation |
| Sources → tables | company data tables, via scheduled refresh |
| Memory → outside | other systems, only through explicit process actions |

No flow writes into another's territory: a data source cannot alter a fact
extracted from a meeting, and meeting extraction does not touch data tables.
