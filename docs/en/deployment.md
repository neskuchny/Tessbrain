# Deployment

A single-VPS deployment with Docker Compose and automatic HTTPS. For local
development see [Quickstart](quickstart.md).

Full Russian original with per-step troubleshooting:
[`../ru/DEPLOYMENT.md`](../ru/DEPLOYMENT.md).

---

## What you need

- A server with Docker and Docker Compose
- A domain pointing at it (two, if you want the UI on its own host)
- One model provider key, or a reachable local model endpoint

## Layout

`deploy/single-vps/docker-compose.yml` brings up:

| Service | Role |
|---|---|
| `postgres` | relational store |
| `redis` | cache, counters, rate limits |
| `qdrant` | vectors |
| `neo4j` | graph |
| `migrate` | applies schema, then exits |
| `init-data` | first-run seed |
| `api` | the backend |
| `worker` | background tasks |
| `frontend` | web UI |
| `caddy` | reverse proxy, automatic TLS |

## Steps

```bash
# 1. configuration
cp env.example .env
# fill in secrets — see Configuration; JWT_SECRET is mandatory in production

# 2. data stores first
docker compose -f deploy/single-vps/docker-compose.yml up -d \
    postgres redis qdrant neo4j

# 3. schema
python -m backend.db.migrate

# 4. readiness gate — must exit 0
python scripts/preflight_check.py

# 5. everything else
docker compose -f deploy/single-vps/docker-compose.yml up -d --build
```

Then import `deploy/grafana/tessent_overview.json` into Grafana if you want the
prepared dashboard.

## Smoke test

```bash
curl https://your-host/api/v1/ping
curl https://your-host/api/v1/health
```

`/health` reports each dependency separately, so a partial outage is visible
rather than being hidden behind a single "ok".

## Before real traffic

Do not skip [Production hardening](production-hardening.md) — several
protections are permissive by default.

## Enabling features in stages

Feature flags exist so a deployment can be brought up in stages rather than
all at once. `docker-compose.yml` carries comments marking which ones to turn
on in which order. Turning everything on at first boot makes a failure hard to
localise.

## Upgrades

```bash
git pull
python -m backend.db.migrate          # migrations are checksummed and idempotent
docker compose -f deploy/single-vps/docker-compose.yml up -d --build
```

See [Migrations](migrations.md).
