# -*- coding: utf-8 -*-
"""Событийные (webhook-подобные) триггеры процесс-досок.

Доска с trigger-узлом `trigger_type=event` запускается, когда в компании
происходит событие мозга (встреча завершилась, задача просрочена, KPI
изменился, документ обновлён и т.п.). Мост: один обработчик на каждый тип
события в EventBus; при событии находим все process-доски этого пользователя,
у которых event-триггер настроен на этот тип, и прогоняем их через
process_engine с payload события на входе.

Регистрация обработчиков идемпотентна и делается один раз при старте
(`register_board_event_handlers`) — переживает рестарт так же, как
восстановление event-подписок автоматизаций. Гейт — тот же
BOARD_PROCESS_TRIGGERS, что и у schedule-триггеров. Никогда не raises.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_registered = False


def _event_trigger_data(graph: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """data event-триггера доски (trigger_type=event), иначе None."""
    for n in (graph or {}).get("nodes") or []:
        if str(n.get("type")) == "trigger":
            d = n.get("data") or {}
            if str(d.get("trigger_type")) == "event":
                return d
    return None


def event_trigger_type(graph: Dict[str, Any]) -> Optional[str]:
    """Тип события event-триггера доски, иначе None."""
    d = _event_trigger_data(graph)
    if d is None:
        return None
    et = str(d.get("event_type") or "").strip()
    return et or None


def event_trigger_enabled(graph: Dict[str, Any]) -> bool:
    """Включена ли автоматизация у event-триггера. Явное enabled=false → выключена
    (доска не стреляет — режим сборки/теста). Нет поля → включена (обратная
    совместимость: существующие доски работают как раньше)."""
    d = _event_trigger_data(graph)
    if not d:
        return False
    return d.get("enabled") is not False


def event_trigger_folders(graph: Dict[str, Any]) -> Optional[List[str]]:
    """Фильтр по папкам встреч у event-триггера доски (folder_ids). Пусто/нет →
    None = «любая папка» (без ограничения)."""
    d = _event_trigger_data(graph)
    if not d:
        return None
    raw = d.get("folder_ids")
    if not isinstance(raw, list):
        return None
    ids = [str(x).strip() for x in raw if str(x).strip()]
    return ids or None


def _folder_matches(filter_ids: Optional[List[str]], payload: Dict[str, Any]) -> bool:
    """Проходит ли событие по фильтру папок. Нет фильтра → да (любая папка).
    Фильтр задан → у события должен быть folder_id из списка (иначе — не наше)."""
    if not filter_ids:
        return True
    fid = ""
    if isinstance(payload, dict):
        fid = str(payload.get("folder_id") or payload.get("folderId") or "").strip()
    return bool(fid) and fid in set(filter_ids)


def _payload_to_text(payload: Dict[str, Any]) -> str:
    """Человекочитаемый текст из payload события — он течёт в граф как вход
    триггера. Предпочитаем осмысленные поля, иначе — компактный JSON."""
    if not isinstance(payload, dict):
        return str(payload or "")
    for key in ("summary", "title", "text", "name", "message"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        return json.dumps(payload, ensure_ascii=False)[:2000]
    except Exception:
        return str(payload)[:2000]


def _graph_fingerprint(graph: Dict[str, Any]) -> str:
    """Отпечаток СОДЕРЖАНИЯ доски: типы+данные узлов и рёбра, БЕЗ имени и
    позиций. Копии одной доски (шаблон открывали и запускали несколько раз —
    каждая Run создавала новую) дают одинаковый отпечаток. Пусто при сбое —
    тогда дедуп не применяется (безопасное направление)."""
    try:
        import hashlib
        nodes = sorted(
            json.dumps({"t": n.get("type"), "d": n.get("data") or {}},
                       ensure_ascii=False, sort_keys=True)
            for n in (graph or {}).get("nodes") or [])
        edges = sorted(f"{e.get('source')}->{e.get('target')}"
                       for e in (graph or {}).get("edges") or [])
        return hashlib.sha1(("|".join(nodes) + "#" + "|".join(edges))
                            .encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        return ""


def _fired_path(user_id: str) -> str:
    import os
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = os.path.join("data", "board_event_fired")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}.json")


def _already_fired(user_id: str, board_id: str, meeting_id: str) -> bool:
    """Доска уже отработала эту встречу? «Встреча завершилась» на деле
    эмитится при КАЖДОЙ (пере)индексации — без этого реестра доски стреляли
    много раз по одной и той же старой встрече."""
    import os
    try:
        p = _fired_path(user_id)
        if not os.path.exists(p):
            return False
        with open(p, encoding="utf-8") as f:
            return f"{board_id}:{meeting_id}" in (json.load(f) or {})
    except Exception:
        return False


def _mark_fired(user_id: str, board_id: str, meeting_id: str) -> None:
    try:
        import time
        p = _fired_path(user_id)
        data = {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        data[f"{board_id}:{meeting_id}"] = int(time.time())
        # кап: держим последние 500 записей
        if len(data) > 500:
            for k in sorted(data, key=data.get)[:len(data) - 500]:
                data.pop(k, None)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        logger.debug("board fired registry write failed", exc_info=True)


async def _run_boards_for_event(event: Any) -> None:
    """Обработчик EventBus: запустить процесс-доски, подписанные на это событие.
    Best-effort — ошибки логируются, не роняют эмиссию события."""
    from backend.core.board.triggers import triggers_enabled
    if not triggers_enabled():
        return
    event_type = str(getattr(event, "event_type", "") or "")
    user_id = str(getattr(event, "user_id", "") or "")
    payload = getattr(event, "payload", {}) or {}
    if not event_type or not user_id:
        return
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        rows: List[Dict[str, Any]] = await sb._request(
            "GET", "/rest/v1/board_workflows",
            params={"kind": "eq.process", "user_id": f"eq.{user_id}",
                    "select": "id,user_id,graph", "limit": "200"}) or []
    except Exception as e:
        logger.warning("board event triggers: list failed: %s", e)
        return

    from backend.core.board.process_engine import run_process_board
    text = _payload_to_text(payload)
    meeting_id = str((payload or {}).get("meeting_id") or "") or None
    # force-переиндексация старой встречи — не событие «встреча завершилась»,
    # а техпрогон. Доски по нему не стреляем (иначе — залп старых карт/отчётов).
    if isinstance(payload, dict) and payload.get("is_reindex"):
        logger.info("board event trigger: %s — переиндексация (force), доски "
                    "не запускаем (встреча %s)", event_type, meeting_id)
        return
    ran = 0
    seen_fp: set = set()
    for b in rows:
        graph = b.get("graph") or {}
        if event_trigger_type(graph) != event_type:
            continue
        # Дедуп ДОСОК-КОПИЙ: одинаковое содержание → одинаковый результат.
        # Инцидент: у пользователя накопилось 6 копий «Инфографики» + 5 копий
        # «Mind map» (каждый Run загруженного шаблона создавал новую доску) —
        # новая встреча получала 11 почти одинаковых сообщений. Первая копия
        # отрабатывает, остальные помечаются в реестре и пропускаются.
        _fp = _graph_fingerprint(graph)
        _bid0 = str(b.get("id") or "")
        if _fp and _fp in seen_fp:
            logger.info("board event trigger: доска %s — копия уже "
                        "запущенной в этом событии, пропуск (дубль)", _bid0)
            if meeting_id and _bid0:
                _mark_fired(user_id, _bid0, meeting_id)
            continue
        if _fp:
            seen_fp.add(_fp)
        # Автоматизация выключена на триггере (режим сборки/теста) → не стреляем.
        if not event_trigger_enabled(graph):
            continue
        # Фильтр по папкам встреч (если задан на триггере): гоняем только для
        # выбранных папок. Нет фильтра → любая папка (обратная совместимость).
        if not _folder_matches(event_trigger_folders(graph), payload):
            continue
        # Один раз на встречу: переиндексация старой встречи снова эмитит
        # meeting_ended — доску по той же встрече повторно не гоняем.
        _bid = str(b.get("id") or "")
        if meeting_id and _bid and _already_fired(user_id, _bid, meeting_id):
            logger.info("board event trigger: доска %s уже отработала встречу "
                        "%s — пропуск (переиндексация)", _bid, meeting_id)
            continue
        try:
            await run_process_board(graph, user_id=user_id, trigger_payload=text,
                                    trigger_meeting_id=meeting_id)
            if meeting_id and _bid:
                _mark_fired(user_id, _bid, meeting_id)
            ran += 1
        except Exception as e:
            logger.warning("board event trigger: run failed board=%s: %s",
                           b.get("id"), e)
    if ran:
        logger.info("board event triggers: событие %s → запущено досок: %d",
                    event_type, ran)


def register_board_event_handlers() -> int:
    """Подписать один обработчик на каждый тип события EventBus. Идемпотентно.
    Возвращает число зарегистрированных типов (0 при повторном вызове)."""
    global _registered
    if _registered:
        return 0
    try:
        from backend.core.automations.event_bus import EventType, get_event_bus
        bus = get_event_bus()
        # Только «доменные» события компании (не служебные automation_failed/custom).
        board_events = [
            EventType.MEETING_ENDED, EventType.TASK_OVERDUE,
            EventType.TASK_COMPLETED, EventType.NEW_MEETING_SCHEDULED,
            EventType.KPI_CHANGED, EventType.DOCUMENT_UPDATED,
            EventType.USER_MENTIONED, EventType.NEW_EMAIL, EventType.NEW_LEAD,
        ]
        for et in board_events:
            bus.subscribe(et, _run_boards_for_event)
        _registered = True
        logger.info("board event triggers: обработчики зарегистрированы (%d типов)",
                    len(board_events))
        return len(board_events)
    except Exception as e:
        logger.warning("board event triggers: регистрация не удалась: %s", e)
        return 0
