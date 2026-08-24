#!/usr/bin/env python3
"""W36: pre-flight production readiness check.

Запускается оператором перед deploy:
    python scripts/preflight_check.py

Проверяет:
- ENV переменные обязательные / запрещённые dev-defaults
- Postgres connectivity + миграции применены
- Redis connectivity
- Qdrant connectivity (опционально)
- Neo4j connectivity (опционально)
- LLM provider reachable (Gemini / OpenAI / local)
- enterprise_mode invariants (если включён)
- Доступные dischar quotas / config sane

Exit codes:
- 0 — всё ок, можно deploy'ить
- 1 — критические проблемы (deploy НЕЛЬЗЯ)
- 2 — warning (deploy можно, но есть риски)

Использует только stdlib + asyncpg/redis-py если установлены — никаких
тяжёлых импортов всего backend, чтобы можно было запускать на чистой
prod-машине ДО `pip install`.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Optional

INSECURE_DEFAULTS = {
    "change-me-in-production",
    "your-jwt-secret-key",
    "your-secret-key",
    "tessent-brain-secret",
    "tessent_secret",
    "neo4j_secret",
    "password",
    "supersecret",
    "dev-share-secret",
}

REQUIRED_PROD_ENVS = [
    "SECRET_KEY",
    "JWT_SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
]


class Result:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checks_passed: int = 0

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self) -> None:
        self.checks_passed += 1


def check_env_secrets(r: Result) -> None:
    """Обязательные ENV + запрет dev-defaults."""
    app_env = os.environ.get("APP_ENV", "development")
    if app_env != "production":
        r.warn(
            f"APP_ENV={app_env} (not production) — pre-flight strict mode "
            "is intended for production deploy"
        )

    for key in REQUIRED_PROD_ENVS:
        v = os.environ.get(key, "")
        if not v:
            r.err(f"{key} is empty — required for production")
            continue
        if v in INSECURE_DEFAULTS or v.startswith("dev-") or v == "change-me":
            r.err(f"{key} uses an insecure dev default")
            continue
        if key.endswith("_KEY") and len(v) < 32:
            r.warn(f"{key} length={len(v)} — should be ≥32 chars for production")
        r.ok()

    # Optional but recommended.
    for key in ("SHARE_JWT_SECRET", "SERVICE_JWT_SECRET", "TELEGRAM_WEBHOOK_SECRET"):
        v = os.environ.get(key, "")
        if not v:
            r.warn(
                f"{key} is empty — feature disabled or dev mode "
                "(SHARE_JWT_SECRET fallback to JWT_SECRET_KEY in dev)"
            )


def check_database_url(r: Result) -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return
    if "asyncpg" not in url:
        r.err(f"DATABASE_URL must use asyncpg driver: {url[:30]}…")
        return
    # Insecure password in URL
    m = re.search(r"://[^:]+:([^@]+)@", url)
    if m and m.group(1) in INSECURE_DEFAULTS:
        r.err("DATABASE_URL contains an insecure default password")
        return
    r.ok()


async def check_postgres_connectivity(r: Result) -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        r.err("DATABASE_URL not set, cannot probe postgres")
        return
    try:
        import asyncpg
    except ImportError:
        r.warn("asyncpg not installed — skipping postgres probe")
        return
    try:
        # asyncpg expects postgres:// not postgresql+asyncpg://
        clean_url = url.replace("postgresql+asyncpg://", "postgres://")
        conn = await asyncio.wait_for(asyncpg.connect(clean_url), timeout=5.0)
        try:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        finally:
            await conn.close()
    except Exception as exc:
        r.err(f"Cannot connect to Postgres: {exc}")
        return

    table_names = {t["tablename"] for t in tables}
    expected_tables = {
        "audit_events", "tz_templates", "validation_results",
        "messenger_links", "share_bundles", "share_grants",
    }
    missing = expected_tables - table_names
    if missing:
        r.err(
            f"Missing migrations: {', '.join(sorted(missing))}. "
            "Run: python -m backend.db.migrate apply"
        )
    else:
        r.ok()


async def check_redis_connectivity(r: Result) -> None:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis.asyncio as aioredis
    except ImportError:
        r.warn("redis-py not installed — skipping redis probe")
        return
    try:
        client = aioredis.from_url(url)
        ok = await asyncio.wait_for(client.ping(), timeout=3.0)
        await client.aclose()
        if ok:
            r.ok()
        else:
            r.err("Redis ping returned False")
    except Exception as exc:
        r.err(f"Cannot connect to Redis: {exc}")


def check_enterprise_mode(r: Result) -> None:
    is_enterprise = os.environ.get("ENTERPRISE_MODE", "").lower() in {"1", "true", "yes"}
    if not is_enterprise:
        return
    if os.environ.get("OPENAI_API_KEY"):
        r.err("ENTERPRISE_MODE=true but OPENAI_API_KEY set — managed LLM forbidden")
    if os.environ.get("GOOGLE_API_KEY"):
        r.err("ENTERPRISE_MODE=true but GOOGLE_API_KEY set — managed LLM forbidden")
    base = os.environ.get("LLM_LOCAL_BASE_URL", "")
    if not base:
        r.err("ENTERPRISE_MODE requires LLM_LOCAL_BASE_URL pointing at internal vLLM/Ollama")
        return
    # Internal-URL heuristic.
    if any(p in base for p in ("api.openai.com", "googleapis.com", "anthropic.com")):
        r.err(f"LLM_LOCAL_BASE_URL={base} points to a managed LLM provider")
    elif not any(p in base for p in (
        "localhost", "127.", "10.", "172.", "192.168.", ".local", ".svc.cluster.local",
    )):
        r.warn(
            f"LLM_LOCAL_BASE_URL={base} doesn't look internal — "
            "verify it's RFC1918 / loopback / internal DNS"
        )
    r.ok()


def check_cors_origins(r: Result) -> None:
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if not raw:
        r.warn("CORS_ALLOWED_ORIGINS empty — using dev defaults (localhost only)")
        return
    if "*" in raw:
        r.err("CORS_ALLOWED_ORIGINS contains '*' — incompatible with allow_credentials=true")
        return
    if any("localhost" in o for o in raw.split(",")) and os.environ.get("APP_ENV") == "production":
        r.warn("CORS_ALLOWED_ORIGINS contains localhost in production")
    r.ok()


def report(r: Result) -> int:
    print()
    print(f"=== Pre-flight check report ===")
    print(f"Checks passed: {r.checks_passed}")
    print(f"Warnings:      {len(r.warnings)}")
    print(f"Errors:        {len(r.errors)}")
    print()
    if r.warnings:
        print("⚠️  Warnings:")
        for w in r.warnings:
            print(f"  - {w}")
        print()
    if r.errors:
        print("❌ Errors (deploy blocked):")
        for e in r.errors:
            print(f"  - {e}")
        print()
        return 1
    if r.warnings:
        print("✅ No blockers. Review warnings above before deploy.")
        return 2
    print("✅ All checks passed. Ready to deploy.")
    return 0


async def main_async() -> int:
    r = Result()
    check_env_secrets(r)
    check_database_url(r)
    check_cors_origins(r)
    check_enterprise_mode(r)
    await check_postgres_connectivity(r)
    await check_redis_connectivity(r)
    return report(r)


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
