# -*- coding: utf-8 -*-
"""Аудит чтений чувствительных знаний.

`audit_log` в проекте писал только ДЕЙСТВИЯ — шаринг, запуск исполнителя,
ретеншен, смену LLM-профиля. Чтения не журналировались вообще, а для
enterprise-продажи «кто и что смотрел» — обычно прямое требование службы
безопасности, а не украшение: утечка чаще выглядит как чтение, а не как
изменение.

Две вещи, из-за которых это нельзя сделать «в лоб»:

  1. **Объём.** Писать каждое чтение — значит утопить журнал в шуме от
     обычной работы и сделать его бесполезным ровно тогда, когда он
     понадобится. Поэтому журналируем только чувствительное: профили
     людей и узлы уровня CONFIDENTIAL и выше.

  2. **Повторы.** Открыл карточку, обновил страницу, вернулся назад — три
     записи об одном и том же. Здесь стоит окно подавления: повторное
     чтение того же ресурса тем же человеком в пределах окна не пишется.
     Окно короткое (по умолчанию 5 минут) — оно гасит дребезг интерфейса,
     но не скрывает возвращение к данным через час.

Никогда не поднимает исключений и не блокирует выдачу данных: журнал —
наблюдение за системой, а не её часть.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Уровень доступа, начиная с которого чтение считается чувствительным.
# 3 = CONFIDENTIAL по шкале backend/core/access/levels.py.
_DEFAULT_MIN_LEVEL = 3

# Окно подавления повторов, секунды.
_DEFAULT_WINDOW = 300

# Последние записи: (user, resource) → когда писали. Живёт в процессе;
# после рестарта окно просто начинается заново — потерять пару записей
# дешевле, чем тащить ради этого внешнее хранилище.
_recent: Dict[str, float] = {}
_MAX_RECENT = 10000


def enabled() -> bool:
    """По умолчанию ВКЛ: журнал чтений — то, чего не хватало.

    Выключается READ_AUDIT_ENABLED=0, если объём окажется проблемой."""
    raw = os.environ.get("READ_AUDIT_ENABLED", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def min_level() -> int:
    try:
        return int(os.environ.get("READ_AUDIT_MIN_LEVEL",
                                  str(_DEFAULT_MIN_LEVEL)))
    except ValueError:
        return _DEFAULT_MIN_LEVEL


def _window() -> int:
    try:
        return max(0, int(os.environ.get("READ_AUDIT_WINDOW_SECONDS",
                                         str(_DEFAULT_WINDOW))))
    except ValueError:
        return _DEFAULT_WINDOW


def _should_record(user_id: str, resource: str) -> bool:
    """False, если это повтор в пределах окна подавления."""
    win = _window()
    if win <= 0:
        return True
    key = f"{user_id}|{resource}"
    now = time.monotonic()
    last = _recent.get(key)
    if last is not None and (now - last) < win:
        return False
    # Простая защита от роста словаря: при переполнении чистим половину
    # самых старых записей. Точность окна тут не критична.
    if len(_recent) >= _MAX_RECENT:
        for k in sorted(_recent, key=_recent.get)[:_MAX_RECENT // 2]:
            _recent.pop(k, None)
    _recent[key] = now
    return True


def reset_window() -> None:
    """Сбросить окно подавления (используется тестами)."""
    _recent.clear()


def is_sensitive(node: Dict[str, Any]) -> bool:
    """Считается ли чтение этого узла чувствительным."""
    try:
        lvl = node.get("access_level")
        if lvl is None:
            return False
        return int(lvl) >= min_level()
    except (TypeError, ValueError):
        return False


async def record_read(user_id: str, *, resource_type: str,
                      resource_id: str = "", count: int = 1,
                      access_level: Optional[int] = None,
                      extra: Optional[Dict[str, Any]] = None) -> bool:
    """Записать чтение. Возвращает True, если запись реально произошла.

    resource_type: 'persona' | 'person_twin' | 'graph_nodes' | …
    count: сколько объектов отдали (для списков)."""
    if not enabled() or not user_id:
        return False
    resource = f"{resource_type}:{resource_id}" if resource_id else resource_type
    if not _should_record(str(user_id), resource):
        return False
    try:
        from backend.core.observability import audit_log
        md = {"count": int(count or 1)}
        if access_level is not None:
            md["access_level"] = int(access_level)
        if extra:
            md.update(extra)
        await audit_log.emit(action=f"read.{resource_type}",
                             user_id=str(user_id), resource=resource,
                             metadata=md)
        return True
    except Exception:
        logger.debug("read_audit: запись не удалась", exc_info=True)
        return False


async def record_sensitive_nodes(user_id: str, nodes, *,
                                 resource_type: str = "graph_nodes") -> int:
    """Отметить чтение списка узлов — только чувствительных.

    Пишем одну агрегированную запись на выдачу, а не по записи на узел:
    иначе один запрос списка порождал бы десятки строк журнала."""
    if not enabled() or not user_id:
        return 0
    try:
        sensitive = [n for n in (nodes or [])
                     if isinstance(n, dict) and is_sensitive(n)]
        if not sensitive:
            return 0
        top = max(int(n.get("access_level") or 0) for n in sensitive)
        ids = [str(n.get("id") or n.get("node_id") or "")
               for n in sensitive[:20] if n.get("id") or n.get("node_id")]
        await record_read(user_id, resource_type=resource_type,
                          count=len(sensitive), access_level=top,
                          extra={"sample_ids": ids} if ids else None)
        return len(sensitive)
    except Exception:
        logger.debug("read_audit: разметка узлов не удалась", exc_info=True)
        return 0
