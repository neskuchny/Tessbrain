# -*- coding: utf-8 -*-
"""
BlindSpotScanner — Столп A (docs/COMPANY_SELF_MODEL_DESIGN.md §2.A).

«Ответить на то, что компания сама не видит» — эмерджентные паттерны, которые
не виден ни одному сотруднику по отдельности.

Архитектура (TESSENT_BRAIN_ARCHITECTURE §0): СКЕЛЕТ + LLM.
- СКЕЛЕТ (детерминированный, этот модуль): ЗАЗЕМЛЯЕТ слепые зоны в ФАКТЫ —
  паттерн, повторяющийся в ≥3 РАЗНЫХ встречах (правило эмерджентности: один
  участник видит один случай, система видит, что он повторяется); систематический
  observer-drift; неразрешённые противоречия графа. У каждой слепой зоны —
  непустое evidence (ссылки на встречи/события).
- LLM (под скелетом, композируется отдельно через company_hypotheses): держит
  конкурирующие гипотезы о ПРИЧИНЕ, ev_info направляет, ГДЕ её подтвердить.

Несущее правило (§4, не прятать): слепая зона подтверждается ФАКТАМИ ГРАФА
(повтор/drift/противоречие), НЕ уверенностью LLM. «LLM не нашёл» ≠ «нет». Без
evidence — не эмитим. Чистый скелет тестируется везде (только stdlib).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


def _norm(s) -> str:
    return str(s).strip().lower()


@dataclass
class BlindSpot:
    pattern: str
    kind: str                       # recurring | drift | contradiction
    severity: str                   # high | medium | low
    evidence: list = field(default_factory=list)  # ГРУНТ: ссылки на встречи/события (непусто!)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern, "kind": self.kind, "severity": self.severity,
            "evidence": self.evidence, "detail": self.detail,
        }


def detect_recurring(episodes: list, min_recurrence: int = 3) -> list:
    """Паттерны, повторяющиеся в ≥min_recurrence РАЗНЫХ встречах (= слепая зона:
    никто по отдельности не видит, что это система). episodes: list[{pattern,
    meeting_id}]. КЛЮЧЕВОЕ: считаем РАЗНЫЕ встречи, не общее число упоминаний —
    5 раз в одной встрече ≠ паттерн."""
    by_pattern: dict = {}
    for e in (episodes or []):
        if not isinstance(e, dict):
            continue
        p = e.get("pattern")
        if not p or not str(p).strip():
            continue
        key = _norm(p)
        slot = by_pattern.setdefault(key, {"label": str(p).strip(), "meetings": set()})
        m = e.get("meeting_id")
        if m is not None and str(m).strip():
            slot["meetings"].add(str(m))
    out: list = []
    for slot in by_pattern.values():
        n = len(slot["meetings"])
        if n >= min_recurrence:
            out.append(BlindSpot(
                pattern=slot["label"], kind="recurring",
                severity="high" if n >= 5 else "medium",
                evidence=sorted(slot["meetings"]),
                detail=f"повторяется в {n} разных встречах",
            ))
    return out


def blind_spot_from_drift(drift: Optional[dict]) -> list:
    """Слепая зона из observer-drift (D1): систематическая переоценка исходов —
    встречи дрейфуют от реальности. Только при значимом дрейфе на ≥3 решениях."""
    if not drift:
        return []
    n = drift.get("evaluated", 0)
    di = drift.get("drift_index", 0.0)
    if n >= 3 and di >= 0.3:
        return [BlindSpot(
            pattern="систематическая переоценка исходов решений", kind="drift",
            severity="high" if di >= 0.6 else "medium",
            evidence=[f"evaluated={n}", f"drift_index={di}"],
            detail="встречи систематически переоценивают результаты (observer-effect)",
        )]
    return []


def recurring_from_pattern_nodes(nodes: list, min_recurrence: int = 3) -> list:
    """Слепые зоны из узлов Pattern графа (pattern_engine УЖЕ детектирует повторы:
    description + frequency). Pattern с frequency≥min → слепая зона; evidence —
    examples (ссылки на встречи) либо 'frequency=N'. Чистая функция."""
    out: list = []
    for d in (nodes or []):
        if not isinstance(d, dict) or _norm(d.get("_label") or d.get("label") or "") != "pattern":
            continue
        try:
            freq = int(d.get("frequency") or 0)
        except (ValueError, TypeError):
            freq = 0
        desc = d.get("description") or d.get("name")
        if not desc or freq < min_recurrence:
            continue
        ex = d.get("examples")
        evidence = [str(x) for x in ex] if isinstance(ex, list) and ex else [f"frequency={freq}"]
        out.append(BlindSpot(
            pattern=str(desc).strip(), kind="recurring",
            severity="high" if freq >= 5 else "medium",
            evidence=evidence, detail=f"паттерн повторяется (frequency={freq})",
        ))
    return out


def contradictions_from_graph_nodes(nodes: list) -> list:
    """Противоречия из узлов Contradiction графа → формат для scan(). ЧЕСТНО:
    только при обоих фактах (new_fact И old_fact) — иначе нет грунта. Чистая."""
    out: list = []
    for d in (nodes or []):
        if not isinstance(d, dict) or _norm(d.get("_label") or d.get("label") or "") != "contradiction":
            continue
        nf, of = d.get("new_fact"), d.get("old_fact")
        if not (nf and of):
            continue
        ev = [str(x) for x in (of, nf, d.get("contradiction_id")) if x]
        out.append({
            "summary": f"{of} ↔ {nf}",
            "evidence": ev,
            "severity": "low" if d.get("resolution") else "high",
            "detail": d.get("contradiction_type") or "",
        })
    return out


def proactive_insights(blind_spots: list, desyncs: Optional[list] = None) -> list:
    """Находки скелета (слепые зоны + рассинхроны) → инсайт-дикты для проактивной
    доставки (ночной cycle → submit_night_insights). ЧЕСТНО: эмитим только
    заземлённое (у слепых зон есть evidence; рассинхроны — с указанием отделов).
    Пусто → []. Чистая функция (формат под NightInsight, без тяжёлых импортов)."""
    sev_to_prio = {"high": "high", "medium": "medium", "low": "low"}
    sev_to_conf = {"high": 0.8, "medium": 0.62, "low": 0.52}
    out: list = []
    for b in (blind_spots or []):
        if not isinstance(b, dict) or not b.get("evidence"):
            continue  # без evidence не эмитим (факт ≠ уверенность)
        sev = b.get("severity", "medium")
        out.append({
            "insight_type": "risk",
            "priority": sev_to_prio.get(sev, "medium"),
            "title": f"Слепая зона: {b.get('pattern', '')}"[:120],
            "description": b.get("detail") or b.get("pattern", ""),
            "evidence": list(b.get("evidence") or []),
            "actions": ["Разобрать на ближайшей встрече", "Назначить владельца"],
            "confidence": sev_to_conf.get(sev, 0.6),
            "kind": b.get("kind", "recurring"),
        })
    for d in (desyncs or []):
        if not isinstance(d, dict):
            continue
        grounded = bool(d.get("grounded"))
        a, bb = d.get("department_a"), d.get("department_b")
        out.append({
            "insight_type": "risk",
            "priority": "high" if grounded else "medium",
            "title": f"Рассинхрон отделов: {a}↔{bb}"[:120],
            "description": f"{d.get('conflict_type', 'конфликт')}: «{d.get('goal_a', '')}» vs «{d.get('goal_b', '')}»",
            "evidence": [f"{a} vs {bb}"] + (d.get("shared") or []),
            "actions": ["Согласовать цели отделов", "Эскалировать руководителю"],
            "confidence": 0.7 if grounded else 0.55,
            "kind": "desync",
        })
    return out


class BlindSpotScanner:
    """Оркестратор столпа A. Скелет заземляет слепые зоны в факты; ev_info
    (company_hypotheses) композируется отдельно для анализа ПРИЧИНЫ."""

    _SEV_ORDER = {"high": 0, "medium": 1, "low": 2}

    def __init__(self, min_recurrence: int = 3):
        self.min_recurrence = min_recurrence

    def scan(self, episodes: list, drift: Optional[dict] = None,
             contradictions: Optional[list] = None) -> list:
        """Собрать заземлённые слепые зоны из сигналов. Каждая — с непустым
        evidence (иначе не эмитим: уверенность ≠ факт)."""
        spots: list = detect_recurring(episodes, self.min_recurrence)
        spots += blind_spot_from_drift(drift)
        for c in (contradictions or []):
            if not isinstance(c, dict):
                continue
            ev = c.get("evidence") or []
            if ev:  # ЧЕСТНО: только противоречия, прослеживаемые по графу
                spots.append(BlindSpot(
                    pattern=c.get("summary") or "неразрешённое противоречие",
                    kind="contradiction",
                    severity=c.get("severity", "medium"),
                    evidence=[str(x) for x in ev],
                    detail=c.get("detail", ""),
                ))
        # Ранжируем: high>medium>low, затем по объёму доказательств.
        return sorted(spots, key=lambda b: (self._SEV_ORDER.get(b.severity, 3), -len(b.evidence)))

    def scan_from_graph(self, nodes: list, drift: Optional[dict] = None) -> list:
        """Скан прямо из узлов per-user графа: Pattern-узлы (повторы) +
        Contradiction-узлы (противоречия) + observer-drift. Ранжировано."""
        spots = recurring_from_pattern_nodes(nodes, self.min_recurrence)
        spots += blind_spot_from_drift(drift)
        for c in contradictions_from_graph_nodes(nodes):
            spots.append(BlindSpot(
                pattern=c["summary"], kind="contradiction", severity=c["severity"],
                evidence=c["evidence"], detail=c.get("detail", ""),
            ))
        return sorted(spots, key=lambda b: (self._SEV_ORDER.get(b.severity, 3), -len(b.evidence)))
