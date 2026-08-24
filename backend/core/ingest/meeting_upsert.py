"""Org-aware idempotency для processed_meetings ledger.

Расширяет существующий `_mark_meeting_processed` (knowledge_sync.py) тремя
операциями:

    find_completed_episode  — есть ли уже успешная обработка этого эпизода?
    claim_episode           — атомарно занять lease на обработку.
    mark_completion         — заполнить финальный статус + новые колонки.

Дедуп-стратегия (приоритет сверху вниз):
    1. (org_id, source, external_id)   — основной ключ для corp-канала.
       Покрыт partial UNIQUE индексом из миграции 250.
    2. content_hash                    — fallback: разные external_id, тот же
       транскрипт (повторный экспорт после правки тайтла, ре-загрузка mp3).
    3. (user_id, meeting_id)           — legacy, для Mini Tess / solo-юзеров
       и manual upload без org_id. Поведение совместимо с миграцией 003.

Lease (processing_lease_until): мягкий лок. Если другой worker не успел
завершить обработку за TTL — мы можем перехватить. Это защищает от висящих
'running' после crash, не блокируя честных конкурентов.

Все функции never-raise при сетевых сбоях: возвращают `{acquired: False,
reason: '...'}`. Caller сам решает — fallback или skip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# TTL лизы. 30 минут хватает на самые тяжёлые orchestrator-проходы; после этого
# stale-запись доступна для перехвата конкурентом.
LEASE_TTL = timedelta(minutes=30)

# Статусы. Совместимы со старыми значениями ('completed', 'no_transcript')
# чтобы код в knowledge_sync.py видел те же значения. Ошибки пишем фиксированным
# 'failed' (текст — в last_error), НЕ свободной строкой 'error: ...'.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_NO_TRANSCRIPT = "no_transcript"
STATUS_FAILED = "failed"
TERMINAL_OK = frozenset({STATUS_COMPLETED, STATUS_NO_TRANSCRIPT})

# В Supabase на processed_meetings исторически висит CHECK со старым словарём
# статусов ('pending','processing','completed','failed') — миграция
# 267_processed_meetings_status_check.sql расширяет его. Если она ещё не
# применена, POST/PATCH с 'running'/'no_transcript' падает с 23514, строка не
# пишется, и встреча переобрабатывается КАЖДЫЙ цикл поллера (runaway-расход
# LLM). Fallback: при 23514 ретраим один раз с маппингом в старый словарь.
_LEGACY_STATUS_MAP = {
    STATUS_RUNNING: "processing",
    # терминальный для skip-логики knowledge_sync (TERMINAL_OK содержит
    # 'completed'); настоящий статус сохраняем в last_error
    STATUS_NO_TRANSCRIPT: STATUS_COMPLETED,
}


def _legacy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Смаппить payload под старый CHECK-словарь статусов (fallback на 23514)."""
    out = dict(payload)
    status = out.get("status")
    if status in _LEGACY_STATUS_MAP:
        out["status"] = _LEGACY_STATUS_MAP[status]
        if status == STATUS_NO_TRANSCRIPT and not out.get("last_error"):
            out["last_error"] = STATUS_NO_TRANSCRIPT
    elif status not in ("pending", "processing", STATUS_COMPLETED, STATUS_FAILED):
        out["status"] = STATUS_FAILED
        if not out.get("last_error"):
            out["last_error"] = f"original_status: {status}"
    return out


def _is_check_violation(exc: Exception) -> bool:
    msg = str(exc)
    return "23514" in msg or "check constraint" in msg.lower()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_in(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


async def find_completed_episode(
    supabase,
    *,
    org_id: Optional[str],
    source: Optional[str],
    external_id: Optional[str],
    content_hash: Optional[str],
) -> Optional[dict[str, Any]]:
    """Найти существующую успешную обработку этого эпизода.

    Returns: ledger row (`{meeting_id, status, processed_at, ...}`) если есть
    completed/no_transcript запись по (org_id, source, external_id) или по
    content_hash. Иначе None.

    Never-raise: при ошибке сети/PostgREST возвращает None (caller просто
    пойдёт processing обычным путём — дубль в худшем случае).
    """
    # 1. Основной ключ — corp-канал.
    if org_id and source and external_id:
        try:
            rows = await supabase._request(
                "GET",
                "/rest/v1/processed_meetings",
                params={
                    "org_id": f"eq.{org_id}",
                    "source": f"eq.{source}",
                    "external_id": f"eq.{external_id}",
                    "status": f"in.({','.join(TERMINAL_OK)})",
                    "select": "id,meeting_id,user_id,status,processed_at,content_hash",
                    "limit": "1",
                },
            )
            if rows:
                return rows[0]
        except Exception as e:
            logger.debug("find by (org,source,ext) failed: %s", e)

    # 2. Fallback по content_hash. Полезно если external_id поменялся
    #    (re-export после правки тайтла, или manual upload того же mp3).
    if content_hash:
        try:
            rows = await supabase._request(
                "GET",
                "/rest/v1/processed_meetings",
                params={
                    "content_hash": f"eq.{content_hash}",
                    "status": f"in.({','.join(TERMINAL_OK)})",
                    "select": "id,meeting_id,user_id,status,processed_at,content_hash",
                    "limit": "1",
                },
            )
            if rows:
                return rows[0]
        except Exception as e:
            logger.debug("find by content_hash failed: %s", e)

    return None


async def claim_episode(
    supabase,
    *,
    user_id: str,
    meeting_id: str,
    org_id: Optional[str],
    source: Optional[str],
    external_id: Optional[str],
    content_hash: Optional[str],
    subscription_id: Optional[str],
) -> dict[str, Any]:
    """Атомарно занять lease на обработку.

    Возвращает:
        {acquired: True, ledger_id: '...'}  — мы взяли строку, можно processing
        {acquired: False, reason: '...', existing_status: '...'}

    Стратегия:
        - PATCH существующей строки (user_id, meeting_id) → status='running',
          lease=now+TTL. Это покрывает re-try после ошибки.
        - Если PATCH вернул пусто → POST новой строки. Если POST упал по
          UNIQUE — параллельный worker нас опередил, возвращаем acquired=False.
    """
    lease_until = _iso_in(LEASE_TTL)
    now = _iso_now()
    base_payload: dict[str, Any] = {
        "status": STATUS_RUNNING,
        "processing_lease_until": lease_until,
        "processed_at": now,
        "subscription_id": subscription_id,
        "org_id": org_id,
        "source": source,
        "external_id": external_id,
        "content_hash": content_hash,
    }

    # Попытка обновить существующую запись (re-try после ошибки или нового force).
    # Условие "lease истёк ИЛИ строка не running" гарантирует, что мы не отбираем
    # активный лизинг у живого worker'а.
    try:
        updated = await supabase._request(
            "PATCH",
            "/rest/v1/processed_meetings",
            params={
                "user_id": f"eq.{user_id}",
                "meeting_id": f"eq.{meeting_id}",
                # PostgREST: status NOT EQ 'running' OR lease истёк
                "or": f"(status.neq.{STATUS_RUNNING},processing_lease_until.lt.{now})",
            },
            json_data=base_payload,
            headers={"Prefer": "return=representation"},
        )
        if updated:
            return {"acquired": True, "ledger_id": updated[0].get("id")}
    except Exception as e:
        logger.debug("claim PATCH failed (will try POST): %s", e)

    # Нет такой строки — создаём.
    try:
        created = await supabase._request(
            "POST",
            "/rest/v1/processed_meetings",
            json_data={
                "user_id": user_id,
                "meeting_id": meeting_id,
                "attempts": 1,
                **base_payload,
            },
            headers={"Prefer": "return=representation"},
        )
        if created:
            return {"acquired": True, "ledger_id": created[0].get("id")}
    except Exception as e:
        # 23505 = unique_violation. Это значит другой worker уже создал строку
        # с тем же (org_id, source, external_id) — он первый, мы пропускаем.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower():
            logger.info("claim lost race for meeting %s: %s", meeting_id, msg[:120])
            return {
                "acquired": False,
                "reason": "race_lost_to_other_worker",
                "existing_status": STATUS_RUNNING,
            }
        logger.warning("claim POST failed: %s", e)
        return {"acquired": False, "reason": f"network_error: {msg[:120]}"}

    # PATCH вернул пусто И POST не дал результата → strange, отказываем.
    return {"acquired": False, "reason": "no_row_after_patch_and_post"}


async def mark_completion(
    supabase,
    *,
    user_id: str,
    meeting_id: str,
    org_id: Optional[str],
    source: Optional[str],
    external_id: Optional[str],
    content_hash: Optional[str],
    subscription_id: Optional[str],
    status: str,
    error: Optional[str] = None,
) -> None:
    """Записать финальный статус. Заменяет старый `_mark_meeting_processed`.

    Не бросает: idempotency-журнал — best-effort, не блокирует основной flow.
    """
    payload: dict[str, Any] = {
        "status": status,
        "processed_at": _iso_now(),
        "processing_lease_until": None,  # снять lease
        "subscription_id": subscription_id,
        "org_id": org_id,
        "source": source,
        "external_id": external_id,
        "content_hash": content_hash,
    }
    if error:
        payload["last_error"] = error[:500]

    # Сначала PATCH; если строки нет (claim не успел или был пропущен) — POST.
    patch_params = {
        "user_id": f"eq.{user_id}",
        "meeting_id": f"eq.{meeting_id}",
    }
    try:
        updated = await supabase._request(
            "PATCH",
            "/rest/v1/processed_meetings",
            params=patch_params,
            json_data=payload,
            headers={"Prefer": "return=representation"},
        )
        if updated:
            return
    except Exception as e:
        if _is_check_violation(e):
            # Строка есть, но новый статус отбит старым CHECK → legacy-ретрай,
            # иначе встреча зависнет в не-терминальном статусе навсегда.
            try:
                updated = await supabase._request(
                    "PATCH",
                    "/rest/v1/processed_meetings",
                    params=patch_params,
                    json_data=_legacy_payload(payload),
                    headers={"Prefer": "return=representation"},
                )
                if updated:
                    return
            except Exception as e2:
                logger.debug("mark_completion legacy PATCH failed: %s", e2)
        logger.debug("mark_completion PATCH failed (will try POST): %s", e)

    post_body = {
        "user_id": user_id,
        "meeting_id": meeting_id,
        "attempts": 1,
        **payload,
    }
    try:
        await supabase._request(
            "POST",
            "/rest/v1/processed_meetings",
            json_data=post_body,
            headers={"Prefer": "return=representation"},
        )
        return
    except Exception as e:
        if not _is_check_violation(e):
            logger.warning("mark_completion POST failed for meeting %s: %s", meeting_id, e)
            return
        logger.warning(
            "mark_completion: status CHECK отбил '%s' для %s — ретрай в legacy-"
            "словаре (примени миграцию 267_processed_meetings_status_check.sql)",
            status, meeting_id)

    # Ретрай с legacy-статусом: без записи в ledger встреча вечно считается
    # «новой» и переобрабатывается каждый цикл поллера — это дороже, чем
    # потеря точного статуса (он сохранён в last_error).
    try:
        await supabase._request(
            "POST",
            "/rest/v1/processed_meetings",
            json_data=_legacy_payload(post_body),
            headers={"Prefer": "return=representation"},
        )
    except Exception as e:
        logger.error(
            "mark_completion legacy-retry failed for meeting %s: %s — встреча "
            "будет переобработана следующим циклом", meeting_id, e)
