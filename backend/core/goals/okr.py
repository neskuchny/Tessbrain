# -*- coding: utf-8 -*-
"""OKR-каркас: ключевые результаты, оценка 0..1, квартальный цикл, грейды.

Зачем. Слой согласованности целей (уровни, каскад, детекция конфликтов,
индекс синхронизации) в системе уже сильный — обычно в OKR-продуктах
слабейшее место именно он. Не хватало самого OKR-объекта: цель была
плоской записью с одним `progress: 0..100`, без ключевых результатов,
без оценки 0..1, без квартального ритма. Прогресс считался по закрытым
задачам — «сделали 5 задач из 10», а не «дошли до цифры».

Здесь — чистые функции этого каркаса (скоринг, кварталы, грейды):
без файлов, без сети, тестируются напрямую. Хранение и API живут в
goal_tracker/routes и зовут эти функции.

Правила честности:
  - KR без данных даёт None, а не 0: «не мерили» ≠ «ноль прогресса»;
  - цель без KR не получает оценку 0..1 вовсе — старый задачный прогресс
    остаётся для неё единственным и честно называется счётчиком задач;
  - метричный KR с target == start некорректен и оценки не имеет:
    делить на ноль молча — значит выдумать прогресс.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

KR_KINDS = ("metric", "milestone", "binary")
COMMITMENTS = ("committed", "aspirational")

# Классическая OKR-шкала: ≥0.7 для амбициозной цели — успех;
# committed-цель обязана дойти до 1.0.
ASPIRATIONAL_OK = 0.7


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_kr(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Привести KR из входных данных к канонической записи. Мусор → None."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        return None
    kind = str(raw.get("kind") or "metric").strip().lower()
    if kind not in KR_KINDS:
        kind = "metric"
    kr: Dict[str, Any] = {
        "id": str(raw.get("id") or uuid.uuid4()),
        "title": title,
        "kind": kind,
        "unit": str(raw.get("unit") or "").strip(),
    }
    if kind == "metric":
        kr["start"] = _num(raw.get("start"))
        kr["target"] = _num(raw.get("target"))
        kr["current"] = _num(raw.get("current"))
        # связь с реестром метрик: имя показателя из данных компании
        mn = str(raw.get("metric_name") or "").strip()
        if mn:
            kr["metric_name"] = mn
    elif kind == "milestone":
        kr["fraction"] = _num(raw.get("fraction"))
        kr["done"] = bool(raw.get("done"))
    else:  # binary
        kr["done"] = bool(raw.get("done"))
    return kr


def score_kr(kr: Dict[str, Any]) -> Optional[float]:
    """Оценка одного KR в 0..1. None — «оценить нечем», и это не ноль."""
    kind = kr.get("kind")
    if kind == "binary":
        return 1.0 if kr.get("done") else 0.0
    if kind == "milestone":
        if kr.get("done"):
            return 1.0
        f = _num(kr.get("fraction"))
        return max(0.0, min(1.0, f)) if f is not None else 0.0
    # metric
    start, target, current = (_num(kr.get("start")), _num(kr.get("target")),
                              _num(kr.get("current")))
    if start is None or target is None:
        return None
    if target == start:
        # Некорректный KR: цель «из 5 в 5» не измеряет ничего.
        return None
    if current is None:
        return None
    return max(0.0, min(1.0, (current - start) / (target - start)))


def score_goal(key_results: List[Dict[str, Any]]) -> Optional[float]:
    """Оценка цели = среднее по оценённым KR. Ни один KR не оценён → None."""
    scores = [s for s in (score_kr(k) for k in key_results or [])
              if s is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 3)


def grade_goal(score: Optional[float], commitment: str) -> Dict[str, str]:
    """Грейд при закрытии цикла — по правилам, а не по ощущению.

    committed: обязательная цель, ожидание 1.0 — всё ниже это «не
    выполнено» (так задумано методом: committed не ставят с запасом).
    aspirational: ≥0.7 — успех, 0.4..0.7 — прогресс, ниже — мимо.
    """
    if score is None:
        return {"grade": "unmeasured",
                "label": "не измерено — у цели нет оценённых ключевых результатов"}
    if commitment == "committed":
        if score >= 0.999:
            return {"grade": "done", "label": "выполнено"}
        return {"grade": "missed",
                "label": f"не выполнено ({score:.0%} — обязательная цель "
                         f"требует 100%)"}
    if score >= ASPIRATIONAL_OK:
        return {"grade": "done", "label": f"успех ({score:.0%})"}
    if score >= 0.4:
        return {"grade": "progress", "label": f"частично ({score:.0%})"}
    return {"grade": "missed", "label": f"мимо ({score:.0%})"}


# ── Квартальный цикл ────────────────────────────────────────────────────

def quarter_of(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def quarter_bounds(cycle: str) -> Optional[Dict[str, str]]:
    """'2026-Q3' → {'start': '2026-07-01', 'end': '2026-09-30'}. Мусор → None."""
    try:
        year_s, q_s = str(cycle).strip().split("-Q")
        year, q = int(year_s), int(q_s)
        if not (1 <= q <= 4 and 2000 <= year <= 2100):
            return None
    except (ValueError, AttributeError):
        return None
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    days = {1: 31, 2: 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
            else 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30,
            10: 31, 11: 30, 12: 31}[end_month]
    return {"start": f"{year}-{start_month:02d}-01",
            "end": f"{year}-{end_month:02d}-{days}"}


def make_checkin(*, author: str, note: str = "",
                 confidence: Optional[float] = None,
                 kr_scores: Optional[Dict[str, Optional[float]]] = None,
                 goal_score: Optional[float] = None) -> Dict[str, Any]:
    """Запись чекина по цели: кто, когда, что сказал, где цель сейчас."""
    c = _num(confidence)
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "author": str(author or ""),
        "note": str(note or "")[:2000],
        "confidence": max(0.0, min(1.0, c)) if c is not None else None,
        "kr_scores": dict(kr_scores or {}),
        "goal_score": goal_score,
    }
