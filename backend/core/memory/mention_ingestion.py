# -*- coding: utf-8 -*-
"""
Mention ingestion glue — подключение cross_source_mentions к РЕАЛЬНОЙ индексации
(MEMORY_DESIGN_PRINCIPLES §6 №5).

Тонкий слой между пайплайном индексации (vector_indexer._index_entities и т.п.) и
примитивом CrossSourceMentions. Зачем отдельно: vector_indexer тяжёлый (Qdrant и
пр.), а это — чистая, тестируемая в изоляции логика «сущности встречи → упоминания».

Принципы:
- entity-ключ строится как "{user_id}::{name}" (per-user namespace, как в тестах).
- source_id = meeting_id (стабильный, тот же что в graph/vector payload).
- Идемпотентность обеспечивает сам стор (повтор той же встречи не задваивает).
- Best-effort: любая ошибка → {"error": ...}, вызывающего НЕ роняем.
- store можно ИНЪЕКТИРОВАТЬ (для тестов / повторного использования); по умолчанию
  открывается per-user JSON-стор по tenant-пути.
"""
from __future__ import annotations

from typing import Optional


def entity_key(user_id: str, name: str) -> str:
    """Ключ сущности с per-user namespace. Нормализуем пробелы, регистр — нет
    (имена сущностей регистрозависимы: 'Polis' ≠ 'polis insurance')."""
    return f"{user_id}::{' '.join((name or '').split())}"


def _open_user_store(user_id: str):
    """Открыть per-user CrossSourceMentions по tenant-пути. Бросает наверх при
    проблеме с путём (caller ловит)."""
    from backend.core.memory.cross_source_mentions import CrossSourceMentions
    from backend.core.store.tenant_paths import cross_source_mentions_path_for_user
    path = cross_source_mentions_path_for_user(user_id)
    return CrossSourceMentions(persist_path=path)


def ingest_entity_mentions(
    entities: list, *, meeting_id: str, user_id: str,
    source_type: str = "meeting", at: Optional[str] = None, store=None,
) -> dict:
    """Записать упоминания сущностей встречи в cross-source стор.

    entities: список dict с ключом 'name' (форма из vector_indexer._index_entities).
    Возвращает {"ingested": bool, "added": int, "existed": int} или
    {"ingested": False, "error": ...}. Никогда не бросает (best-effort).
    """
    if not (user_id and meeting_id):
        return {"ingested": False, "error": "user_id и meeting_id обязательны"}
    names = []
    for e in (entities or []):
        if isinstance(e, dict):
            name = e.get("name") or e.get("entity") or ""
        else:
            name = str(e)
        if name and str(name).strip():
            names.append(entity_key(user_id, str(name)))
    if not names:
        return {"ingested": False, "error": "нет сущностей с именем"}
    try:
        st = store if store is not None else _open_user_store(user_id)
        res = st.record_many(names, source_type=source_type, source_id=meeting_id, at=at)
        return {"ingested": True, "added": res["added"], "existed": res["existed"]}
    except Exception as e:  # best-effort — индексацию не роняем
        return {"ingested": False, "error": f"{type(e).__name__}: {e}"}
