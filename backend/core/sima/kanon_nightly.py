# -*- coding: utf-8 -*-
"""Ночной Kanon-планировщик (roadmap SIMA_KANON): «готово к подтверждению: N».

Смысл для непрограммиста: он спроектировал продукт в SIMA и ушёл; ночью
система сама пересчитывает, какие блоки уже можно отдавать в разработку
(контракт готов + зависимости не сломаны), и присылает пуш в Telegram —
«N блоков готовы, открой SIMA → Реализация и подтверди». Ничего НЕ запускает
и PENDING не плодит: только честный подсчёт + уведомление (запуск агента —
всегда явный confirm человека).

Флаг: KANON_NIGHTLY_PREPARE=on (по умолчанию ВЫКЛ) + общий enable_sima_kanon.
Доставка — через reactive learning-loop (submit_recommendation): гейт
нагрузки, дедуп 24ч (одинаковое состояние не спамит), обучение на реакции.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

_MAX_PROJECTS = 10  # свежих проектов на пользователя за ночь (кап стоимости)


def nightly_prepare_enabled() -> bool:
    return os.getenv("KANON_NIGHTLY_PREPARE", "").strip().lower() in ("on", "1", "true", "yes")


async def run_for_user(user_id: str) -> dict:
    """Пересчитать runnable-блоки по проектам пользователя и уведомить.
    Never-raise. Returns {"runnable": N, "notified": bool}."""
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if not bool(get_feature_flags().enable_sima_kanon):
            return {"runnable": 0, "notified": False, "reason": "kanon off"}
    except Exception:
        return {"runnable": 0, "notified": False, "reason": "flags unavailable"}

    try:
        from backend.core.sima import kanon_service as ks
        from backend.core.store.tenant_paths import sima_kanon_path_for_user
        from backend.db.sima_client import get_sima_db

        db = await get_sima_db()
        projects = await db.list_projects(user_id)
        store = ks.KanonStore(sima_kanon_path_for_user(user_id))
        total = 0
        lines: list[str] = []
        for p in (projects or [])[:_MAX_PROJECTS]:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            full = await db.get_project(pid, user_id)
            if not full:
                continue
            runnable = ks.pick_runnable_blocks(full, store.get_project(pid))
            if not runnable:
                continue
            total += len(runnable)
            names = ", ".join(r["name"] for r in runnable[:3])
            more = f" (+{len(runnable) - 3})" if len(runnable) > 3 else ""
            lines.append(f"- **{p.get('name', pid)}**: {len(runnable)} — {names}{more}")
        if total == 0:
            return {"runnable": 0, "notified": False}

        body = (
            "Эти блоки уже можно отдавать в разработку — контракт готов, "
            "зависимости не сломаны:\n\n" + "\n".join(lines) +
            "\n\nОткройте SIMA → «4 Реализация» → Kanon и подтвердите запуск "
            "(агент никогда не стартует сам)."
        )
        from backend.core.reactive.recommendations import submit_recommendation
        res = await submit_recommendation(
            user_id=user_id,
            title=f"SIMA: готово к подтверждению — {total} блок(ов)",
            body_markdown=body,
            signal_type="kanon_ready",
            priority=55,
            recommended_channels=["telegram"],
            source_kind="kanon_nightly",
        )
        return {"runnable": total, "notified": True, "delivery": res}
    except Exception as e:
        logger.debug("kanon_nightly failed for %s: %s", user_id[:8], e)
        return {"runnable": 0, "notified": False, "error": str(e)}
