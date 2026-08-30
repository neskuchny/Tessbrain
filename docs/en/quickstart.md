# Quickstart

Get a local instance answering requests. For a real deployment see
[Deployment](deployment.md) — this page is deliberately the short path.

**Requirements:** Python 3.11+, Docker, Node.js 20+ (only if you want the web
UI).

---

## 1. Start the infrastructure

```bash
docker compose up -d
```

This brings up PostgreSQL, Redis and Qdrant.

> **Note on Neo4j.** The graph store is optional for a first run — the system
> falls back to an in-process NetworkX graph when Neo4j is not reachable. The
> production compose file (`deploy/single-vps/docker-compose.yml`) includes a
> Neo4j service; the development one does not.

## 2. Install dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

> **If `pip install` fails on `ag2`** — the agent framework is a heavy optional
> dependency and its wheels lag behind new Python releases. The app starts
> without it: memory, search and chat work, and the agent features log a
> warning and stay off. Check with
> `python -c "import backend.core.think as t; print(t.AG2_AVAILABLE)"`.
> To enable them later: `pip install "ag2[openai,gemini]>=0.9,<1.0"` —
> the upper bound matters: ag2 1.0+ no longer ships the `autogen` module.
> Python 3.11 and 3.12 are the tested versions.

Optional, only if you want the browser-driving Web Operator:

```bash
playwright install chromium
```

## 3. Configure

```bash
cp env.example .env
```

Then open `.env` and set, at minimum:

- **One model provider key** — `GOOGLE_API_KEY` or `OPENAI_API_KEY`. Without
  one, extraction and chat cannot run.
- **`JWT_SECRET_KEY`** — generate with
  `python -c "import secrets; print(secrets.token_hex(32))"`. Note the `_KEY`
  suffix: the setting is `jwt_secret_key`, and unknown keys are ignored
  silently, so `JWT_SECRET` on its own does nothing.

Everything else has a working default for local use. Full list:
[Configuration](configuration.md).

> In production the process **refuses to start** without a real JWT secret.
> That is deliberate: without it, signature verification silently degrades and
> identity stops being checked. See [Security](security.md).

## 4. Apply the schema

```bash
python -m backend.db.migrate
```

Useful flags: `--list` to see what would run, `--dry-run` to rehearse,
`--target <version>` to stop at a specific migration.

## 5. Run the API

```bash
python -m backend.main
```

Or explicitly through the ASGI server used in production:

```bash
python -m granian --interface asgi --host 0.0.0.0 --port 8000 backend.api.app:app
```

## 6. Check it

```bash
curl http://localhost:8000/api/v1/ping
```

Interactive API docs: <http://localhost:8000/docs>

Readiness of every dependency: <http://localhost:8000/api/v1/health>

## 7. Web UI (optional)

```bash
cd frontend
npm install
npm run dev
```

---

## First real request

Fastest: load the pre-extracted demo corpus — no LLM calls, nothing billed:

```bash
python scripts/seed_demo.py --corpus demo/helion   # or demo/gelion (Russian)
```

Or ingest a meeting yourself and then ask about it:

```bash
# 1. feed a transcript in
curl -X POST http://localhost:8000/api/v1/analyze/ingest/meeting \
  -H 'Content-Type: application/json' \
  -d '{"title":"Weekly sync","transcript":"...","date":"2026-03-11"}'

# 2. ask the memory a question
curl -X POST http://localhost:8000/api/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"message":"What did we decide about the landing page?"}'
```

Full surface: [API reference](api-reference.md).

---

## Connect an assistant

To use company memory from Claude Code, Cursor or any MCP-capable client:

```bash
python mcp_server.py
```

This exposes 23 tools over stdio. Setup, hosted HTTP transport and token
handling: [MCP server](mcp.md).

---

## Before you put it in front of real traffic

The local path above skips things that matter in production. At minimum, read
[Production hardening](production-hardening.md) — rate limiting, strict
authentication, trusted proxy configuration and quota enforcement are all off
or permissive by default so that local development is not painful.
