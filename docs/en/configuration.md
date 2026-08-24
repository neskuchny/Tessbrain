# Configuration

Everything is configured through environment variables, read into a typed
settings object (`backend/config.py`). Start from `env.example`.

Only the variables you are likely to need are listed here; the settings module
is the complete reference.

---

## Required in production

| Variable | Notes |
|---|---|
| `JWT_SECRET` | `openssl rand -hex 32`. **Production will not start without a real value** — a placeholder from `env.example` is rejected. |
| `APP_ENV` | `production` enables boot-time validators |
| `CORS_ALLOWED_ORIGINS` | Comma-separated. `*` with credentials is refused. |

Accepted aliases for the JWT secret, checked in order: `JWT_SECRET`,
`SUPABASE_JWT_SECRET`, `SECRET_KEY`, `JWT_SECRET_KEY`, `LEGACY_JWT_SECRET`
(the last one only for rotation).

## Model providers

| Variable | Notes |
|---|---|
| `GOOGLE_API_KEY` | Gemini |
| `OPENAI_API_KEY` | OpenAI |
| `LLM_LOCAL_BASE_URL` | Any OpenAI-compatible endpoint — vLLM, Ollama, TGI, LM Studio |
| `LLM_LOCAL_MODEL` | Model name at that endpoint |
| `ENTERPRISE_MODE` | `true` forbids cloud providers entirely and requires a local endpoint |

At least one provider must be configured, or extraction and chat cannot run.

## Data stores

| Variable | Default |
|---|---|
| `DATABASE_URL` | built from `POSTGRES_*` if unset |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / `6333` |
| `QDRANT_API_KEY` | empty = no auth (local dev) |
| `QDRANT_HTTPS` | `false` — set `true` only for Qdrant Cloud |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | optional; NetworkX fallback if absent |

> `QDRANT_HTTPS` deserves attention: the client turns HTTPS on by default once
> an API key is set, which breaks a self-hosted plain-HTTP instance with
> `SSL: WRONG_VERSION_NUMBER`.

## Limits and spend

| Variable | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Needs Redis to actually enforce |
| `RATE_LIMIT_USER_PER_MINUTE` | `600` | Authenticated bucket |
| `RATE_LIMIT_ANON_PER_MINUTE` | `120` | Per-IP bucket |
| `RATE_LIMIT_TENANT_PER_MINUTE` | `5000` | Tenant bucket |
| `LLM_USER_DAILY_TOKEN_LIMIT` | `2000000` | `0` = unlimited |
| `LLM_TENANT_DAILY_TOKEN_LIMIT` | `5000000` | `0` = unlimited |
| `LLM_TENANT_DAILY_COST_USD` | `50.0` | Soft daily ceiling |
| `LLM_PER_JOB_COST_USD` | `5.0` | Guards runaway background jobs |
| `TESSENT_QUOTA_STRICT` | off | Block instead of allow when usage cannot be metered |
| `LLM_QUOTA_ATOMIC` | on | Atomic reservation against parallel overshoot |

## Security

| Variable | Default | Purpose |
|---|---|---|
| `STRICT_USER_AUTH` | off | Reject token-less requests carrying `?user_id=` |
| `TRUSTED_PROXY_HOPS` | inferred | How many trailing `X-Forwarded-For` entries are yours |
| `LOGIN_MAX_FAILURES` | `10` | Failed logins per account before a hold; `0` disables |
| `LOGIN_FAILURE_WINDOW_SEC` | `900` | Window for the above |
| `SERVICE_JWT_SECRET` | — | For service-to-service tokens |

## Feature flags

Longer-lived toggles live in `config/feature_flags.json` and
`backend/core/config/feature_flags.py`, each with a description in the source.
They exist so a deployment can be brought up in stages.
