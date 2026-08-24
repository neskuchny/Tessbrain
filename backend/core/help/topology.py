# -*- coding: utf-8 -*-
"""
Топология экосистемы Tessbrain — что с чем связано и что советовать.

Небольшое РУЧНОЕ описание (не авто-интроспекция интеграций — это было бы
overkill). Гид всегда подмешивает краткую строку в системный промпт, чтобы
советовать нужный инструмент («вы про звонки — посмотрите CallInsight в
«Клиентах»»). Новая интеграция = правка этого списка.
"""
from __future__ import annotations

# kind/use/where — для строки-подсказки; status live|future.
TOPOLOGY: list[dict[str, str]] = [
    {"name": "MeetFlow", "kind": "встречи", "status": "live",
     "use": "авто-ингест встреч по подписке на проекты/папки",
     "where": "Синхронизация → Подписки"},
    {"name": "CallInsight", "kind": "звонки", "status": "live",
     "use": "голосовые отчёты и клиенты в зоне риска",
     "where": "Знания → Клиенты"},
    {"name": "DailyCalls / стендапы", "kind": "ежедневные созвоны", "status": "future",
     "use": "разбор стендапов (пока — Daily Pulse)",
     "where": ""},
    {"name": "mini Tess", "kind": "внешний доступ", "status": "live",
     "use": "работа с мозгом компании снаружи (сервис-токен)",
     "where": ""},
]


def topology_line() -> str:
    """Однострочная сводка экосистемы для системного промпта гида."""
    parts = []
    for t in TOPOLOGY:
        tag = "" if t["status"] == "live" else " (скоро)"
        where = f" — {t['where']}" if t.get("where") else ""
        parts.append(f"{t['name']}{tag}: {t['use']}{where}")
    if not parts:
        return ""
    return ("Экосистема Tessbrain (советуй нужный инструмент, если вопрос к нему): "
            + "; ".join(parts) + ".")


def list_topology() -> list[dict[str, str]]:
    return list(TOPOLOGY)


__all__ = ["TOPOLOGY", "topology_line", "list_topology"]
