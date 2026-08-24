# MCP server

Company memory can be attached as a **tool set** to any MCP client — Claude
Code, Claude Desktop, Cursor, Codex CLI. From inside a conversation with an
assistant you can then ask the company brain a question, generate a report,
absorb a link, run a skill, or triage tasks — against your own company's data.

The MCP server is a **thin wrapper over the same HTTP API** the web UI and CLI
use. It does not bypass authorisation and it does not duplicate logic; the real
work stays in the backend.

---

## Two transports

| File | Transport | Use when | Dependencies |
|---|---|---|---|
| `mcp_server.py` | **stdio** (JSON-RPC) | local Claude Code / Codex CLI / Cursor | stdlib + `httpx` |
| `mcp_http_server.py` | **Streamable HTTP** | remote access, tool directories | `mcp[cli]` + uvicorn/starlette |

Both expose the **same** `TOOLS` registry — the HTTP variant re-imports it from
the stdio one, so the two cannot drift apart.

For local work stdio is enough; the `mcp` package is not needed.

---

## The 23 tools

**Knowledge and memory** — `ask_brain`, `search_memory`, `ingest_url`,
`insights_feed`, `brain_status`

**Reports and artifacts** — `compose_artifact`, `generate_report`,
`list_report_types`, `list_reports`, `get_report`, `weekly_review`

**Skills** — `list_skills`, `run_skill`

**Tasks and goals** — `analyze_tasks`, `goals_progress`, `task_comment`,
`task_close`

**Coding handoff** — `coding_handoff`, `confirm_coding_handoff`,
`reject_coding_handoff`

**Spec / acceptance workflow** — `sima_kanon_status`, `sima_kanon_verify`,
`sima_kanon_handoff`

---

## Setup

### The normal path — issue a token in the UI

**Integrations → "Connect to Claude, Cursor and other assistants" → issue
token.** The token is shown once, together with a ready-made address and
connection command, and there is a revoke button next to it.

Tokens are **personal**: calls execute as whoever issued the token, so a single
server serves different people without mixing their data.

The server needs `TESSENT_API_TOKEN` set for tools to call back into the API;
without it `/api/v1/mcp` honestly returns 503 rather than pretending.

### A. Local stdio

```bash
python mcp_server.py
```

Client configuration (`mcpServers`): `command=python`,
`args=["mcp_server.py"]`, env: `TESSENT_API_URL`, `TESSENT_API_TOKEN`,
`TESSENT_USER_ID`.

### B. Hosted HTTP

```bash
claude mcp add --transport http tessent https://<host>/api/v1/mcp \
    -H "Authorization: Bearer <your token>"
```

Use a personal token from the UI. A static `TESSENT_MCP_TOKEN` from the server
environment also works for a single-operator or admin install, in which case
calls run as `TESSENT_USER_ID`.

### C. Remote connector with OAuth

A separate process, behind the `ENABLE_MCP_OAUTH` flag, that does not touch the
main API:

```bash
pip install starlette uvicorn httpx
export ENABLE_MCP_OAUTH=1
export MCP_PUBLIC_URL=https://your-host.example.com
export TESSENT_API_URL=http://127.0.0.1:8000
export TESSENT_API_TOKEN=<service-token>
python mcp_remote_server.py          # listens on :8770
```

Reverse-proxy `/mcp`, `/oauth/` and the two
`/.well-known/oauth-*` paths to `127.0.0.1:8770`, preserving the
`Authorization` header and disabling response buffering.

Verify from outside:

```bash
curl https://your-host.example.com/.well-known/oauth-protected-resource
```

---

## Token management from the console

The `tess_mcp_…` tokens are the same ones the UI button issues. You only need
the CLI for scripting or incident review:

```bash
python -m mcp_token_store mint <user_id>     # issue
python -m mcp_token_store list <user_id>     # show
python -m mcp_token_store revoke <token>     # revoke
```

---

## Security invariant

The MCP layer adds no privileges. Every tool call goes through the same
authentication, the same tenant resolution and the same access boundary as an
equivalent HTTP request — including the rule that material a person may not see
is removed before it reaches a model.

See [Security](security.md) and [Access control](access-control.md).
