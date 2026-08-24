# -*- coding: utf-8 -*-
"""
CrossDepartmentDesyncDetector — Столп B (docs/COMPANY_SELF_MODEL_DESIGN.md §2.B).

«Где цели/KPI двух отделов несовместимы — рассинхрон, которого компания сама
не видит» (маркетинг хочет ×2 пользователей, инженерия −50% нагрузки на ту же
систему).

Архитектура (TESSENT_BRAIN_ARCHITECTURE §0): СКЕЛЕТ + LLM.
- СКЕЛЕТ (детерминированный, этот модуль): попарно сравнивает цели РАЗНЫХ
  отделов и ловит СИЛЬНЫЙ сигнал — одна метрика в ПРОТИВОПОЛОЖНЫХ направлениях
  (↑ vs ↓ на том же); плюс мягкий сигнал — общий дефицитный ресурс.
- LLM (под скелетом): судит неоднозначные пары — но скелет решает, какие пары
  смотреть (а не читает всё подряд).

Несущее правило (§4, не прятать): hard-сигнал — ТОЛЬКО когда конфликт
прослеживается по фактам графа (реальные KPI/обязательства); soft — когда лишь
из формулировок целей (иначе шум на стилистике). Чистый скелет тестируется
везде (только stdlib на импорте).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Маркеры направления (подстрока слова → переживает склонения).
_UP = ("увелич", "повыс", "рост", "нарас", "ускор", "масштаб", "больше", "расшир",
       "grow", "increase", "scale", "faster", "more", "boost", "raise", "expand")
_DOWN = ("сниз", "сократ", "уменьш", "замедл", "меньше", "урез", "режем",
         "reduce", "decreas", "cut", "slow", "fewer", "lower", "minimi")
# Дефицитные ресурсы (для мягкого сигнала «спор за ресурс»). Префиксы.
_RESOURCES = ("бюджет", "ресурс", "команд", "разработчик", "инженер", "сервер",
              "время", "дедлайн", "штат", "найм", "deadline", "budget", "resource",
              "team", "engineer", "server", "headcount", "людей")
_STOP = {"для", "под", "над", "это", "как", "что", "при", "без", "the", "and", "for",
         "вокруг", "чего", "наш", "наша", "наши", "we", "our", "на", "по", "из"}


def _norm(s) -> str:
    return str(s).strip().lower()


def direction(text: str) -> str:
    """Направление цели: 'up' | 'down' | 'mixed' | 'none' (по маркерам-подстрокам)."""
    t = _norm(text)
    up = any(m in t for m in _UP)
    down = any(m in t for m in _DOWN)
    if up and down:
        return "mixed"
    return "up" if up else ("down" if down else "none")


def _is_direction_word(w: str) -> bool:
    return any(m in w for m in _UP) or any(m in w for m in _DOWN)


def metric_signature(text: str) -> set:
    """Префиксы содержательных слов цели (что именно меняется), БЕЗ направлений и
    стоп-слов — чтобы сравнивать «ту же метрику» поверх склонений."""
    out: set = set()
    for w in re.findall(r"[^\W\d_]+", _norm(text), re.UNICODE):
        if len(w) >= 4 and w not in _STOP and not _is_direction_word(w):
            out.add(w[:5])
    return out


def resource_overlap(text_a: str, text_b: str) -> list:
    """Общие дефицитные ресурсы в двух целях (мягкий сигнал спора за ресурс)."""
    def res(text):
        t = _norm(text)
        return {r for r in _RESOURCES if r in t}
    return sorted(res(text_a) & res(text_b))


@dataclass
class Desync:
    department_a: str
    department_b: str
    goal_a: str
    goal_b: str
    conflict_type: str          # metric | resource
    severity: str               # high | soft
    shared: list = field(default_factory=list)
    grounded: bool = False      # прослеживается ли по фактам графа (hard) — ставит оркестратор

    def to_dict(self) -> dict:
        return {
            "department_a": self.department_a, "department_b": self.department_b,
            "goal_a": self.goal_a, "goal_b": self.goal_b,
            "conflict_type": self.conflict_type, "severity": self.severity,
            "shared": self.shared, "grounded": self.grounded,
        }


def detect_pair(goal_a: str, goal_b: str) -> Optional[dict]:
    """Сильный детерминированный сигнал между двумя целями, или None.
    1) metric: одна метрика в ПРОТИВОПОЛОЖНЫХ направлениях (↑ vs ↓) — high;
    2) resource: общий дефицитный ресурс — soft."""
    da, db = direction(goal_a), direction(goal_b)
    shared_metric = metric_signature(goal_a) & metric_signature(goal_b)
    if shared_metric and {da, db} == {"up", "down"}:
        return {"conflict_type": "metric", "severity": "high", "shared": sorted(shared_metric)}
    shared_res = resource_overlap(goal_a, goal_b)
    if shared_res:
        return {"conflict_type": "resource", "severity": "soft", "shared": shared_res}
    return None


def find_desyncs(dept_goals: dict) -> list:
    """Попарно по РАЗНЫМ отделам найти рассинхроны. dept_goals: {отдел: [цели]}.
    Возвращает list[Desync]. Внутри одного отдела не сравниваем (это не рассинхрон
    между отделами)."""
    depts = [d for d in (dept_goals or {}) if dept_goals.get(d)]
    out: list = []
    for i in range(len(depts)):
        for j in range(i + 1, len(depts)):
            da, db = depts[i], depts[j]
            for ga in dept_goals[da]:
                for gb in dept_goals[db]:
                    c = detect_pair(str(ga), str(gb))
                    if c:
                        out.append(Desync(department_a=da, department_b=db,
                                          goal_a=str(ga), goal_b=str(gb), **c))
    return out


# Целе-несущие метки (НЕ сущности): из них берём текст как «цель отдела».
# Person/Team/Department/Product/Pattern — сущности, не цели → исключены.
_GOAL_LABELS = {"project", "kpi", "decision", "goal", "objective", "initiative", "principle"}


def dept_goals_from_graph_nodes(nodes: list) -> dict:
    """Цели по отделам из узлов графа → {отдел: [цель-текст]}. ЧЕСТНО ЧАСТИЧНО:
    в схеме нет узла Goal и решения не тегируются отделом, поэтому берём узлы
    целе-несущих меток (_GOAL_LABELS), у которых ЕСТЬ поле department И целе-текст
    (statement/goal/description/name) — обычно Project/KPI с направлением. Recall
    ограничен разметкой; богатые цели берутся из снапшотов отделов (follow-up).
    Сущности (Person/Team/...) исключены. Чистая функция → тестируется без графа."""
    out: dict = {}
    for d in (nodes or []):
        if not isinstance(d, dict):
            continue
        if _norm(d.get("_label") or d.get("label") or "") not in _GOAL_LABELS:
            continue
        dept = d.get("department")
        if not dept or not str(dept).strip():
            continue
        text = None
        for tk in ("statement", "goal", "description", "summary", "name"):
            if d.get(tk) and str(d[tk]).strip():
                text = str(d[tk]).strip()
                break
        if not text:
            continue
        out.setdefault(str(dept).strip(), []).append(text)
    return out


class CrossDepartmentDesyncDetector:
    """Оркестратор столпа B. Скелет ловит сильный сигнал; известные KPI из графа
    апгрейдят metric-конфликт до grounded=hard."""

    def __init__(self, known_kpis: Optional[set] = None):
        # known_kpis — префиксы известных KPI (из графа) для апгрейда до grounded.
        self.known_kpis = known_kpis or set()

    def detect(self, dept_goals: dict) -> list:
        desyncs = find_desyncs(dept_goals)
        # Hard только когда метрика конфликта = известный KPI компании (граф).
        for d in desyncs:
            if d.conflict_type == "metric" and self.known_kpis:
                if any(s in self.known_kpis for s in d.shared):
                    d.grounded = True
        return desyncs
