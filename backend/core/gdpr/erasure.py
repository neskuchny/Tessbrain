"""GDPR Art. 17 — каскадное удаление user-owned данных.

Раньше DELETE /gdpr/me стирал только профиль (UserProfileService) — граф,
персона, reactive, coffee, bridge и пр. оставались (TODO W10). Этот модуль
закрывает каскад по всей Postgres-схеме.

Инвентаризация (см. docs/ru/GDPR_ERASURE.md): список таблиц с owner-колонкой
сверен с миграциями. Сравнение делаем как `column::text = :uid`, чтобы один
код работал и для UUID-, и для TEXT-колонок без cast-ошибок.

Каждое удаление — НЕЗАВИСИМОЕ и best-effort (своя try/except): сбой одной
таблицы (например, её ещё нет в этом деплое) не блокирует остальные. Возвращаем
по-табличные счётчики + список ошибок.

ИСКЛЮЧЕНИЯ (намеренно НЕ удаляем — см. SKIPPED_TABLES):
- audit_events: Art. 17(3)(b) — retention для legal compliance; таблица
  append-only с триггерами против DELETE. Документированное исключение.
- llm_profiles: tenant-shared конфиг (tenant_id, не персональные данные).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# (table, owner_column). Порядок: дети раньше родителей там, где у обоих есть
# своя owner-колонка. FK-only дети (sima_blocks, share_grants,
# automation_execution_log, processed_meetings без своей колонки) удаляются
# каскадом ON DELETE CASCADE от корневых таблиц.
ERASURE_SPECS: list[tuple[str, str]] = [
    # --- reactive ---
    ("reactive_feedback", "user_id"),
    ("reactive_signals", "user_id"),
    ("cascade_events", "user_id"),
    ("cognitive_load_samples", "user_id"),
    ("reactive_calibration", "user_id"),
    # --- coffee ---
    ("coffee_artifacts", "user_id"),
    # --- persona (дети раньше personas) ---
    ("persona_observations", "user_id"),
    ("persona_field_history", "user_id"),
    ("persona_external_sources", "user_id"),
    ("personas", "user_id"),
    # --- BWE (outcome раньше decision) ---
    ("bwe_outcome_events", "user_id"),
    ("bwe_decision_events", "user_id"),
    ("bwe_wisdom_patterns", "user_id"),
    # --- knowledge sync (processed_meetings каскадом, но у неё есть user_id) ---
    ("processed_meetings", "user_id"),
    ("knowledge_sync_subscriptions", "user_id"),
    # --- SIMA (корень; 10+ детей по ON DELETE CASCADE) ---
    ("sima_projects", "user_id"),
    # --- automations (execution_log каскадом) ---
    ("scheduled_automations", "user_id"),
    ("scheduled_tasks", "user_id"),
    # --- direct user data ---
    ("validation_results", "user_id"),
    ("bookmarks", "user_id"),
    ("messenger_onboarding_tokens", "user_id"),
    ("messenger_links", "user_id"),
    ("user_profiles_v2", "user_id"),
    ("documents", "user_id"),
    ("tz_templates", "created_by"),
    # --- sharing (share_grants/share_audit_views каскадом от share_bundles) ---
    ("share_bundles", "owner_user_id"),
    # --- aggregation (личностный линк = contributor/target → стираем) ---
    ("aggregation_contributions", "contributor_user_id"),
    ("aggregation_emissions", "target_user_id"),
]

# Таблицы с особой логикой удаления (не простой column = uid).
_SPECIAL = {"bridge_messages"}

# Намеренно НЕ трогаем (с причиной).
SKIPPED_TABLES: dict[str, str] = {
    "audit_events": "Art. 17(3)(b) legal-hold; append-only trigger-protected",
    "llm_profiles": "tenant-shared config, not personal data",
}


async def _delete_simple(
    session: Any, table: str, column: str, user_id: str, *, dry_run: bool,
) -> int:
    """DELETE (или COUNT при dry_run) по одной таблице. column::text = uid
    работает и для UUID-, и для TEXT-колонок."""
    from sqlalchemy import text
    if dry_run:
        r = await session.execute(
            text(f"SELECT COUNT(*) FROM public.{table} WHERE {column}::text = :uid"),
            {"uid": user_id},
        )
        row = r.first()
        return int(row[0]) if row and row[0] is not None else 0
    res = await session.execute(
        text(f"DELETE FROM public.{table} WHERE {column}::text = :uid"),
        {"uid": user_id},
    )
    return int(getattr(res, "rowcount", 0) or 0)


async def _delete_bridge(session: Any, user_id: str, *, dry_run: bool) -> int:
    """bridge_messages: пользователь — и отправитель, и получатель."""
    from sqlalchemy import text
    cond = "from_user_id::text = :uid OR to_user_id::text = :uid"
    if dry_run:
        r = await session.execute(
            text(f"SELECT COUNT(*) FROM public.bridge_messages WHERE {cond}"),
            {"uid": user_id},
        )
        row = r.first()
        return int(row[0]) if row and row[0] is not None else 0
    res = await session.execute(
        text(f"DELETE FROM public.bridge_messages WHERE {cond}"),
        {"uid": user_id},
    )
    return int(getattr(res, "rowcount", 0) or 0)


async def erase_user(
    user_id: str, *, dry_run: bool = False,
) -> dict[str, Any]:
    """Каскадно удалить (или, при dry_run, посчитать) все данные user_id.

    Каждая таблица обрабатывается в ОТДЕЛЬНОЙ транзакции/session — сбой одной
    не откатывает другие (best-effort erasure: лучше стереть 25 из 27 таблиц,
    чем 0 из-за одной отсутствующей).

    Returns:
        {
          "user_id", "dry_run",
          "deleted": {table: count, ...},   # rowcount по таблицам (>0)
          "errors": {table: "msg", ...},
          "skipped": {table: reason, ...},
          "total_rows": int,
        }
    """
    deleted: dict[str, int] = {}
    errors: dict[str, str] = {}
    if not user_id:
        return {
            "user_id": user_id, "dry_run": dry_run, "deleted": {},
            "errors": {"_": "user_id required"}, "skipped": SKIPPED_TABLES,
            "total_rows": 0,
        }

    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception as exc:
        return {
            "user_id": user_id, "dry_run": dry_run, "deleted": {},
            "errors": {"_postgres": str(exc)}, "skipped": SKIPPED_TABLES,
            "total_rows": 0,
        }

    async def _run(fn) -> Optional[int]:
        # Каждая таблица — своя session (изоляция сбоев).
        try:
            async with pg.session(apply_tenant=False) as session:
                return await fn(session)
        except Exception as exc:
            return exc  # type: ignore[return-value]

    # bridge_messages (special) + все simple specs.
    tasks: list[tuple[str, Any]] = [
        ("bridge_messages", lambda s: _delete_bridge(s, user_id, dry_run=dry_run)),
    ]
    for table, col in ERASURE_SPECS:
        tasks.append(
            (table, (lambda t, c: lambda s: _delete_simple(s, t, c, user_id, dry_run=dry_run))(table, col))
        )

    for table, fn in tasks:
        out = await _run(fn)
        if isinstance(out, Exception):
            errors[table] = str(out)
            logger.warning("gdpr.erase %s failed: %s", table, out)
        elif out:
            deleted[table] = out

    return {
        "user_id": user_id,
        "dry_run": dry_run,
        "deleted": deleted,
        "errors": errors,
        "skipped": SKIPPED_TABLES,
        "total_rows": sum(deleted.values()),
    }
