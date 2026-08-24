# -*- coding: utf-8 -*-
"""
Signal library — кодифицированные семиотические сигналы из встреч
(REUSE-IN-CODE из claude_skills/ai_business_audit/signal-extraction.md).

Зачем (узкое место — recall детекции): раньше слепые зоны брались только из
готовых Pattern-узлов. Этот слой извлекает КАНДИДАТ-сигналы прямо из текста
встреч (Nash-ловушки, перегрузка, провальные внедрения — ровно «что компания не
видит»). Recall растёт.

Дисциплина (precision не жертвуем): per-meeting детектор консервативен (ловит
ХАРАКТЕРНУЮ формулировку, не общие слова). А слепой зоной тег становится ТОЛЬКО
если повторился в ≥3 РАЗНЫХ встречах (правило BlindSpotScanner) — recurrence и
держит precision. Чистый код (stdlib) → тестируется везде.

Паттерны — дословно из signal-extraction.md (группы 2.2, 4.2, 5.2, 1.3).
"""
from __future__ import annotations

import re
from typing import Optional

# (regex характерной формулировки, тег-паттерн). Консервативно: маркер должен
# быть распознаваемой фразой, иначе шум. Все — из каталога сигналов.
_SIGNAL_PATTERNS = [
    # ГРУППА 5.2 — Nash-ловушки (сильнейшие «никто не видит как систему»):
    (r"все\s+знают.*никто\s+не\s+(реша|дела)", "nash_координации"),
    (r"невыгодно\s+перв|перв(ому|ым)\s+(делать|начин)", "nash_первого_хода"),
    (r"(тянет|тянут)\s+одеяло|одеяло\s+на\s+себя|каждый\s+отдел.*на\s+себя", "nash_нулевой_суммы"),
    (r"всё\s+равно\s+не\s+оцен|прощ?е\s+не\s+делать", "nash_демотивации"),
    # ГРУППА 2.2 — перегрузка ключевых людей / централизация:
    (r"(всё|все)\s+через\s+(себя|меня)\s+пропуска|зам(ы|ыкается)\s+на\s+(себя|одн)", "централизация"),
    (r"нет\s+времени\s+на|не\s+успева(ю|ем)\s+(всё|все)", "перегрузка_ключевых"),
    # ГРУППА 4.2 — провальное внедрение (КРИТИЧЕСКИЙ):
    (r"(купил|покупал|внедрял).*никто\s+не\s+(польз|использу)|никто\s+не\s+польз", "провальное_внедрение"),
    # ГРУППА 1.3 — очереди/ожидание:
    (r"клиенты\s+ждут|ждут\s+по\s+\d", "очереди_ожидание"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE | re.UNICODE), tag) for p, tag in _SIGNAL_PATTERNS]


def extract_signal_tags(text: str) -> list:
    """Какие семиотические сигналы (теги-паттерны) присутствуют в тексте встречи.
    Консервативно — по характерной формулировке. Дубли тегов схлопываются."""
    if not text:
        return []
    t = str(text)
    out: list = []
    for rx, tag in _COMPILED:
        if rx.search(t) and tag not in out:
            out.append(tag)
    return out


def signals_to_episodes(meetings: list) -> list:
    """Список встреч [{meeting_id, text}] → эпизоды [{pattern, meeting_id}] для
    BlindSpotScanner.detect_recurring. Тег станет слепой зоной только при повторе
    в ≥3 РАЗНЫХ встречах (precision держит recurrence-правило)."""
    episodes: list = []
    for m in (meetings or []):
        if not isinstance(m, dict):
            continue
        mid = m.get("meeting_id") or m.get("id")
        for tag in extract_signal_tags(m.get("text") or m.get("content") or ""):
            episodes.append({"pattern": tag, "meeting_id": mid})
    return episodes
