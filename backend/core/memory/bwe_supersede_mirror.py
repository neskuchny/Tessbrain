# -*- coding: utf-8 -*-
"""
BWE supersede mirror — ЗЕРКАЛИРОВАНИЕ изменений BWE-паттернов в SupersedeStore
(MEMORY_DESIGN_PRINCIPLES §6 №3, §7 supersede — недеструктивно).

Зачем: BWE уже пишет evolution_log внутри pattern_encoder (см. pattern_encoder.py
строки ~517-527 — порт concept_crystallizer.evolution_log). Это лог-строки
формата 'v{n} @ YYYY-MM-DD: head'. Зеркалирование в наш SupersedeStore даёт:
- per-user JSON стор по tenant-пути (как у других примитивов);
- унифицированный API: current/history/count_distinct_values/total_mentions;
- кросс-pattern запросы из одного источника;
- идемпотентность (повтор того же значения → mention, не новая версия).

КРИТИЧНО (по гардрейлу): ЗЕРКАЛО, не замена.
- pattern.evolution_log в BWE НЕ трогаем — он остаётся как был.
- Любая ошибка зеркалирования → swallowed, BWE-логика не падает.
- Glue инъектируется через DI (store=...); в проде по tenant-пути.
"""
from __future__ import annotations

from typing import Optional


def supersede_key(pattern_id: str, dimension: str = "differentiators") -> str:
    """Канонический ключ supersede для BWE-паттерна. Формат:
    'bwe::pattern::{pattern_id}::{dimension}'. dimension обычно
    'differentiators', но может быть и другим (например 'not_law')."""
    return f"bwe::pattern::{pattern_id}::{dimension}"


def _open_user_store(user_id: str):
    """Открыть per-user SupersedeStore по tenant-пути."""
    from backend.core.memory.supersede_store import SupersedeStore
    from backend.core.store.tenant_paths import supersede_store_path_for_user
    return SupersedeStore(persist_path=supersede_store_path_for_user(user_id))


def mirror_pattern_change(
    *,
    user_id: str,
    pattern_id: str,
    new_value,
    dimension: str = "differentiators",
    as_of: Optional[str] = None,
    source: Optional[str] = None,
    store=None,
) -> dict:
    """Зеркалить изменение differentiators (или иной dimension) паттерна BWE
    в SupersedeStore. Best-effort: исключения подавляются.

    Args:
        user_id: per-user изоляция (uuid)
        pattern_id: id BWE-паттерна
        new_value: новое значение dimension (list/str)
        dimension: какое измерение версионируем (по умолч. 'differentiators')
        as_of: ISO-дата изменения (по умолчанию now)
        source: meeting_id / контекст изменения
        store: инъекция для тестов; в проде — per-user JSON

    Returns:
        {"mirrored": True, "action": "added"|"mention"|"superseded"} либо
        {"mirrored": False, "error": "..."}.
    """
    if not (user_id and pattern_id and new_value):
        return {"mirrored": False, "error": "user_id, pattern_id, new_value обязательны"}
    # Нормализуем значение в строку (supersede хранит value: str)
    if isinstance(new_value, (list, tuple)):
        value_str = "; ".join(str(x) for x in new_value if x)
    else:
        value_str = str(new_value)
    if not value_str:
        return {"mirrored": False, "error": "пустое значение после нормализации"}
    key = supersede_key(pattern_id, dimension)
    try:
        st = store if store is not None else _open_user_store(user_id)
        r = st.record(key, value_str, source=source, as_of=as_of)
        return {"mirrored": True, "action": r["action"], "key": key}
    except Exception as e:
        return {"mirrored": False, "error": f"{type(e).__name__}: {e}"}
