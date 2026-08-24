# -*- coding: utf-8 -*-
"""Единый матчер фильтров источников для всех каналов поиска.

Контракт фильтров чата: {"meeting_id": [id, ...], "document_id": [id, ...]}
— пользователь выбрал встречи и/или документы, источник должен принадлежать
ЛЮБОЙ из выбранных встреч ИЛИ ЛЮБОМУ из выбранных документов.

До этого фикса чат передавал СПИСОК туда, где каналы сравнивали со
СКАЛЯРОМ (metadata["meeting_id"] != [..] — всегда истина) → при выбранных
встречах BM25 возвращал пусто, а vector/graph фильтр вообще не получали.

Семантика:
- скалярное значение фильтра → строгое равенство, все такие условия — AND
  (прежнее поведение для остальных вызовов сохранено);
- списочные значения → membership; НЕСКОЛЬКО списочных ключей образуют
  OR-группу между собой (встречи ИЛИ документы), и AND со скалярами.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def metadata_matches_filters(metadata: Optional[Dict[str, Any]],
                             filters: Optional[Dict[str, Any]]) -> bool:
    """True, если metadata проходит filters (семантика в докстринге модуля)."""
    if not filters:
        return True
    md = metadata or {}
    list_keys = []
    for key, value in filters.items():
        if isinstance(value, (list, tuple, set)):
            list_keys.append((key, value))
        else:
            if md.get(key) != value:
                return False
    if not list_keys:
        return True
    # OR-группа списочных ключей: достаточно попадания по одному
    return any(md.get(k) in v for k, v in list_keys)


def graph_node_passes_filters(node: Optional[Dict[str, Any]],
                              filters: Optional[Dict[str, Any]]) -> bool:
    """Мягкий фильтр для ГРАФОВОГО канала.

    Узлы графа не все привязаны к встрече/документу: Person/Project/KPI —
    кросс-встречные сущности, у них meeting_id нет, и механикой не
    предусмотрено скоупить их к выбору. Правило: узел с СОБСТВЕННЫМ
    значением ключа, не входящим в выбор, — отбрасываем; узел БЕЗ такого
    поля — оставляем (кросс-сущность полезна как контекст)."""
    if not filters:
        return True
    nd = node or {}
    for key, value in filters.items():
        if not isinstance(value, (list, tuple, set)):
            continue  # скаляры к графу не применяем — не его контракт
        own = nd.get(key)
        if own is not None and own != "" and own not in value:
            return False
    return True
