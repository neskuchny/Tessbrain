# -*- coding: utf-8 -*-
"""
Team Misalignment — обнаружение РАСКОЛА между командами
(docs/CAPABILITY_READINESS.md; целевой кейс: «команда A делает одно,
команда B противоположное — у вас потом будут проблемы»).

ПОЧЕМУ ОТДЕЛЬНО: contradiction_detector ловит противоречия ФАКТОВ при записи
в граф; blind_spot_scanner — эмерджентные паттерны. Межкомандный раскол —
другое: обе позиции могут быть внутренне непротиворечивы, конфликт виден
только при СОПОСТАВЛЕНИИ ПО КОМАНДАМ («маркетинг ведёт в self-serve, продажи
обещают персональное демо»).

РАЗДЕЛЕНИЕ (скелет+LLM):
- СКЕЛЕТ (код): подбор кандидатов — пары позиций РАЗНЫХ команд на ОДНУ тему
  (пересечение значимых токенов); внутрикомандные пары исключены; темы без
  пересечения не сравниваются (не жжём LLM на всё-со-всем). Агрегация и
  severity — в коде.
- LLM (инъекция, опц.): вердикт по паре «противоречат ли направления» С
  ОБЪЯСНЕНИЕМ. Без LLM модуль честно возвращает кандидатов со статусом
  'unverified' (скелет-режим). Невалидный ответ судьи → 'unknown', не выдумка.

Источник позиций в проде: решения/заявления команд (event_log decisions с
team в payload, протоколы встреч команд, snapshots). Glue — отдельным шагом;
модуль принимает позиции как данные. Чистый stdlib + инъекция LLM.
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

_STOP = {
    "и", "а", "но", "по", "для", "про", "о", "об", "с", "со", "в", "во", "на",
    "к", "ко", "из", "у", "от", "до", "за", "мы", "будем", "наша", "наши",
    "что", "как", "это", "the", "a", "an", "for", "to", "of", "in", "on", "we",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stem(w: str) -> str:
    """Грубая основа слова: первые 6 символов. Русская морфология ломает
    точное совпадение токенов («онбординг» ≠ «онбордингом», «клиентов» ≠
    «клиенту») — префикс-основа детерминированно сводит формы вместе."""
    return w if len(w) <= 6 else w[:6]


def _topic_tokens(text: str) -> set:
    return {_stem(w.lower()) for w in re.findall(r"[\w\-]+", text or "", re.UNICODE)
            if len(w) > 2 and w.lower() not in _STOP}


@dataclass(frozen=True)
class TeamPosition:
    """Позиция/решение команды. at — ISO-дата (для окна и хронологии)."""
    team: str
    statement: str
    at: str = ""
    source: str = ""        # meeting_id / doc_id


@dataclass(frozen=True)
class MisalignmentFinding:
    """Найденный (или кандидатный) межкомандный конфликт."""
    team_a: str
    team_b: str
    statement_a: str
    statement_b: str
    topic_overlap: Tuple[str, ...]
    status: str             # 'conflict' | 'aligned' | 'unknown' | 'unverified'
    explanation: str = ""
    severity: str = "medium"   # low|medium|high — в КОДЕ
    sources: Tuple[str, ...] = ()

    def to_text(self) -> str:
        mark = {"conflict": "⚠️", "aligned": "✅", "unknown": "❔",
                "unverified": "•"}[self.status]
        return (f"{mark} [{self.team_a} ↔ {self.team_b}] "
                f"({', '.join(self.topic_overlap[:4])}): "
                f"«{self.statement_a}» vs «{self.statement_b}»"
                + (f" — {self.explanation}" if self.explanation else ""))


def find_cross_team_candidates(positions: List[TeamPosition],
                               *, min_topic_overlap: int = 2) -> List[dict]:
    """СКЕЛЕТ: пары позиций РАЗНЫХ команд с пересечением темы ≥ N токенов.
    Детерминированно, без LLM. O(n²) по позициям — позиции уже агрегаты
    (решения команд), не сырые сообщения."""
    out = []
    for i in range(len(positions)):
        for j in range(i + 1, len(positions)):
            a, b = positions[i], positions[j]
            if a.team.strip().lower() == b.team.strip().lower():
                continue                      # внутрикомандное — не раскол
            overlap = _topic_tokens(a.statement) & _topic_tokens(b.statement)
            if len(overlap) >= min_topic_overlap:
                out.append({"a": a, "b": b, "overlap": tuple(sorted(overlap))})
    return out


_JUDGE_SYSTEM = (
    "Ты — аналитик организационных рисков. Даны пары заявлений/решений РАЗНЫХ "
    "команд одной компании по пересекающейся теме. Для КАЖДОЙ пары определи: "
    "'conflict' — направления ПРОТИВОРЕЧАТ друг другу (если обе команды продолжат, "
    "компания получит проблему); 'aligned' — совместимы/дополняют. Объясни одним "
    "предложением ЧТО именно столкнётся. Не выдумывай конфликт там, где его нет. "
    'Верни ТОЛЬКО JSON-массив {"index": int, "status": "conflict"|"aligned", '
    '"explanation": str} — по одному объекту на пару, в том же порядке.'
)


def _parse_json_array(raw: str) -> List[dict]:
    """Устойчивый парс (тот же приём, что в rubric_engine)."""
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", raw):
        try:
            data, _ = dec.raw_decode(raw[m.start():])
        except Exception:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return []


def _severity(overlap_len: int, status: str) -> str:
    if status != "conflict":
        return "low"
    return "high" if overlap_len >= 4 else "medium"


def detect_team_misalignment(
    positions: List[TeamPosition],
    llm: Optional[Callable[[str, str], str]] = None,
    *, min_topic_overlap: int = 2,
) -> List[MisalignmentFinding]:
    """Полный проход: кандидаты (код) → вердикты (LLM, опц.) → severity (код).

    Без llm — кандидаты со статусом 'unverified' (честно: тема общая,
    конфликт не проверен). С llm — conflict/aligned/unknown по каждой паре.

    min_topic_overlap: в скелет-режиме (без llm) держите 2 — меньше шума.
    С LLM-судьёй рекомендуется 1: живой замер (gpt-4o-mini) — ловит конфликт
    с пересечением в один токен («скидки»: Продажи 20% vs Финансы заморозка),
    а несвязанные пары судья честно помечает aligned (ложных конфликтов 0).
    """
    candidates = find_cross_team_candidates(positions,
                                            min_topic_overlap=min_topic_overlap)
    if not candidates:
        return []

    verdicts: dict = {}
    if llm is not None:
        lines = []
        for idx, c in enumerate(candidates):
            lines.append(f"Пара {idx}:\n  [{c['a'].team}] {c['a'].statement}\n"
                         f"  [{c['b'].team}] {c['b'].statement}")
        raw = llm(_JUDGE_SYSTEM, "ПАРЫ:\n" + "\n".join(lines) + "\n\nJSON-массив вердиктов:")
        items = _parse_json_array(raw)
        by_index = {}
        for k, it in enumerate(items):
            try:
                by_index[int(it.get("index", k))] = it
            except Exception:
                by_index[k] = it
        verdicts = by_index

    findings = []
    for idx, c in enumerate(candidates):
        if llm is None:
            status, expl = "unverified", ""
        else:
            it = verdicts.get(idx)
            st = str((it or {}).get("status", "")).lower()
            if st in ("conflict", "aligned"):
                status, expl = st, str((it or {}).get("explanation", ""))[:300]
            else:
                status, expl = "unknown", "судья не дал валидного вердикта"
        findings.append(MisalignmentFinding(
            team_a=c["a"].team, team_b=c["b"].team,
            statement_a=c["a"].statement, statement_b=c["b"].statement,
            topic_overlap=c["overlap"], status=status, explanation=expl,
            severity=_severity(len(c["overlap"]), status),
            sources=tuple(s for s in (c["a"].source, c["b"].source) if s)))
    # конфликты вперёд, по severity
    order = {"conflict": 0, "unknown": 1, "unverified": 2, "aligned": 3}
    sev = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f.status], sev[f.severity]))
    return findings
