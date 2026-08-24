# -*- coding: utf-8 -*-
"""Снапшот встречи — структурный полуфабрикат для наблюдателя (Ф1).

Один LLM-проход на встречу (встречи после записи не меняются → кэш
навсегда) раскладывает транскрипт в компактный JSON со СТРУКТУРНЫМИ
сигналами, которых нет в графе как первоклассных сущностей:

  blockers       — что застряло / кого-чего ждут («Ждём CSV от Ивана 2 недели»)
  tensions       — расхождения мнений / неразрешённые вопросы
  explicit_asks  — кто кого явно попросил о чём
  + summary/topics/decisions/people/products/clients

Дисциплина промпта (взято из изученного proactive-модуля, это его лучшая
часть): только то, что реально звучит в транскрипте; конкретика вместо
общих слов; пусто — значит пусто, не выдумывать.

Хранение: data/meeting_snapshots/<uid>.json — {meeting_id: {snapshot, ts}},
кап 500 записей. Включение: флаг enable_meeting_snapshots (default OFF).
Never-raise: сбой снапшота не трогает синк встречи.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_SNAPSHOTS = 500
_LIST_FIELDS = ("topics", "blockers", "decisions", "tensions",
                "explicit_asks", "people_mentioned", "products_mentioned",
                "clients_mentioned")

SNAPSHOT_SYSTEM_PROMPT = """\
Ты читаешь транскрипт одной встречи и раскладываешь его в компактный JSON
со структурными сигналами. Это не отчёт и не саммари для человека — это
рабочий полуфабрикат для дальнейшего анализа.

Жёсткие правила:
- Опирайся ТОЛЬКО на то, что реально звучит в транскрипте. Никаких общих
  фраз и додумывания.
- Все списки — короткие и конкретные. Тема «автоматизация выгрузки
  отчётов» лучше, чем «эффективность». Блокер «Ждём CSV от Ивана 2
  недели» лучше, чем «есть проблемы с коммуникацией».
- Если чего-то в транскрипте нет — оставляй пустой список. Не выдумывай
  «возможные темы».
- Имена людей, названия продуктов и клиентов — как звучат в транскрипте
  (не нормализуй, не переводи).

Верни строго JSON следующей структуры, без markdown и комментариев:
{
  "summary": "2-4 предложения своими словами, о чём была встреча",
  "topics": ["3-8 коротких формулировок, что обсуждали"],
  "blockers": ["что явно мешает / что застряло / кого-чего ждут"],
  "decisions": ["что решили; только реальные решения"],
  "tensions": ["где было расхождение, спор, неразрешённый вопрос"],
  "explicit_asks": ["кто кого попросил о чём — если было явно"],
  "people_mentioned": ["имена/роли, про кого говорили"],
  "products_mentioned": ["названия продуктов / сервисов"],
  "clients_mentioned": ["названия клиентов / сегментов"]
}
"""


def snapshots_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_meeting_snapshots)
    except Exception:
        return False


# ── хранилище ────────────────────────────────────────────────────────────

def _path(user_id: str) -> str:
    import os
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = os.path.join("data", "meeting_snapshots")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}.json")


def _load_all(user_id: str) -> Dict[str, Any]:
    import os
    try:
        p = _path(user_id)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        logger.debug("snapshots load failed", exc_info=True)
        return {}


def _store(user_id: str, meeting_id: str, snap: Dict[str, Any],
           meta: Optional[Dict[str, Any]] = None) -> None:
    try:
        data = _load_all(user_id)
        data[str(meeting_id)] = {"snapshot": snap, "ts": int(time.time()),
                                 **({"title": str((meta or {}).get("title") or "")[:200],
                                     "created_at": str((meta or {}).get("created_at") or "")[:32]})}
        if len(data) > _MAX_SNAPSHOTS:
            for k in sorted(data, key=lambda k: data[k].get("ts", 0))[
                    :len(data) - _MAX_SNAPSHOTS]:
                data.pop(k, None)
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(_path(user_id), data)
    except Exception:
        logger.debug("snapshot store failed", exc_info=True)


def get_snapshot(user_id: str, meeting_id: str) -> Optional[Dict[str, Any]]:
    """Снапшот встречи из кэша (None если не строился)."""
    rec = _load_all(user_id).get(str(meeting_id))
    return (rec or {}).get("snapshot") if isinstance(rec, dict) else None


def list_snapshots(user_id: str, limit: int = 50) -> Dict[str, Dict[str, Any]]:
    """Последние снапшоты пользователя: {meeting_id: snapshot} (свежие первыми
    по ts записи)."""
    data = _load_all(user_id)
    items = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("ts", 0),
                   reverse=True)[:max(1, limit)]
    return {mid: rec.get("snapshot") or {} for mid, rec in items
            if isinstance(rec, dict)}


def list_snapshot_records(user_id: str, limit: int = 50) -> Dict[str, Dict[str, Any]]:
    """То же, но с метаданными встречи: {meeting_id: {snapshot, title,
    created_at, ts}} — для промптов наблюдателя (заголовок+дата в рендере)."""
    data = _load_all(user_id)
    items = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("ts", 0),
                   reverse=True)[:max(1, limit)]
    return {mid: rec for mid, rec in items if isinstance(rec, dict)}


# ── извлечение ───────────────────────────────────────────────────────────

def _normalize(data: Any) -> Optional[Dict[str, Any]]:
    """Привести ответ LLM к жёсткой форме (summary-строка + списки строк)."""
    if not isinstance(data, dict):
        return None
    out: Dict[str, Any] = {
        "summary": str(data.get("summary") or "").strip()[:1200]}
    for key in _LIST_FIELDS:
        raw = data.get(key)
        vals: List[str] = []
        if isinstance(raw, list):
            vals = [str(x).strip()[:300] for x in raw if str(x).strip()][:12]
        out[key] = vals
    return out


async def ensure_snapshot(user_id: str, meeting: Dict[str, Any],
                          ) -> Optional[Dict[str, Any]]:
    """Построить (или взять из кэша) снапшот встречи. Never-raise.

    meeting: {id, title?, created_at?, transcription_text?/transcript?}.
    Возвращает снапшот или None (нет транскрипта / LLM не ответил)."""
    mid = str(meeting.get("id") or meeting.get("meeting_id") or "").strip()
    if not (user_id and mid):
        return None
    cached = get_snapshot(user_id, mid)
    if cached is not None:
        return cached
    transcript = str(meeting.get("transcription_text")
                     or meeting.get("transcript") or "").strip()
    if not transcript:
        return None
    title = str(meeting.get("title") or "(без названия)")
    created = str(meeting.get("created_at") or "?")
    user_prompt = (f"Встреча: {title}\nДата: {created}\n\n"
                   f"Транскрипт (полный):\n{transcript}\n\n"
                   "Разложи эту встречу в JSON согласно системному промпту.")
    try:
        from backend.core.llm.router import ModelTier, get_llm_router
        raw = await get_llm_router().generate_json(
            prompt=user_prompt,
            system_prompt=SNAPSHOT_SYSTEM_PROMPT,
            model_tier=ModelTier.STANDARD,
            temperature=0.2)
    except Exception as e:
        logger.warning("meeting snapshot LLM failed (%s): %s", mid[:8], e)
        return None
    snap = _normalize(raw)
    if snap is None:
        logger.warning("meeting snapshot: non-dict LLM response (%s)", mid[:8])
        return None
    _store(user_id, mid, snap, meta={"title": title, "created_at": created})
    logger.info("👁 снапшот встречи %s: тем %d, блокеров %d, напряжений %d",
                mid[:8], len(snap["topics"]), len(snap["blockers"]),
                len(snap["tensions"]))
    return snap
