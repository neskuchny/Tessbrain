# -*- coding: utf-8 -*-
"""
psych → PersonSnapshot merge (CAPABILITY_READINESS C8, вторая часть).

ЗАЧЕМ: PsychologicalAnalyzer пишет узлы `PsychologicalProfile` в граф со
свойствами personality_type / dominant_traits / communication_style /
motivation_drivers / team_role / strengths. PersonSnapshot имеет поля
strengths / communication_style — но НЕ читает эти узлы. Снапшот сотрудника
выходит без психпрофиля, который уже собран.

Дизайн (как со всем слоем — скелет в коде, LLM не нужен):
- merge — чистая функция (snapshot, profiles) -> snapshot. Тестируется без
  графа/LLM.
- При нескольких psych-профилях (по разным встречам): берём union для
  strengths/dominant_traits/motivation_drivers (топ-N по частоте упоминаний);
  communication_style/personality_type/team_role — самые свежие непустые;
- НЕ перетираем уже выставленные поля snapshot, кроме явных пустых;
- персистенс остаётся работой `_generate_person_snapshot` (мы только
  обогащаем результат).

ЧЕСТНОСТЬ §1: если профилей нет — snapshot возвращается без изменений
(не выдумываем strengths).
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, List, Optional


def _take_top(items: Iterable[str], n: int) -> List[str]:
    """Топ-N строк по частоте упоминания, порядок стабильный
    (first-seen wins при одинаковой частоте)."""
    seen_order: List[str] = []
    seen_set = set()
    cleaned = []
    for it in items or []:
        if it is None:
            continue
        s = str(it).strip()
        if not s:
            continue
        cleaned.append(s)
        key = s.casefold()
        if key not in seen_set:
            seen_set.add(key)
            seen_order.append(s)
    if not cleaned:
        return []
    counts = Counter(s.casefold() for s in cleaned)
    return sorted(
        seen_order,
        key=lambda s: (-counts[s.casefold()], seen_order.index(s))
    )[:max(1, n)]


def _latest_nonempty(profiles: Iterable[dict], key: str) -> Optional[str]:
    """Самое свежее непустое значение поля по списку профилей.
    Свежесть — по полю created_at (ISO); без неё сохраняем порядок входа."""
    items: list = []
    for i, p in enumerate(profiles or []):
        if not isinstance(p, dict):
            continue
        v = p.get(key)
        if not v:
            continue
        items.append((p.get("created_at") or "", i, str(v).strip()))
    if not items:
        return None
    items.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return items[0][2] or None


def merge_psych_into_person(
    snapshot,
    psych_profiles: List[dict],
    *,
    max_strengths: int = 6,
    max_traits: int = 5,
    max_motives: int = 4,
) -> dict:
    """Обогатить PersonSnapshot данными из psych-профилей. Мутирует snapshot
    и возвращает stats (что было добавлено). Best-effort: пустой список или
    мусор — snapshot не трогаем."""
    stats = {
        "profiles": 0, "strengths_added": 0, "traits_added": 0,
        "motives_added": 0, "communication_style_set": False,
        "personality_type_set": False, "team_role_set": False,
    }
    valid = [p for p in (psych_profiles or []) if isinstance(p, dict)]
    if not valid:
        return stats
    stats["profiles"] = len(valid)

    # 1. strengths — union поверх существующих, top по частоте
    incoming_strengths: list = []
    for p in valid:
        for s in (p.get("strengths") or []):
            incoming_strengths.append(s)
    if incoming_strengths:
        existing = list(getattr(snapshot, "strengths", []) or [])
        merged = _take_top(existing + incoming_strengths, max_strengths)
        delta = [s for s in merged if s not in existing]
        if delta:
            snapshot.strengths = merged
            stats["strengths_added"] = len(delta)

    # 2. dominant_traits — обогащаем areas_for_improvement не трогаем,
    # сохраняем в metadata (новое поле без правки модели — через delta_details)
    incoming_traits: list = []
    for p in valid:
        for t in (p.get("dominant_traits") or []):
            incoming_traits.append(t)
    if incoming_traits:
        top_traits = _take_top(incoming_traits, max_traits)
        details = dict(getattr(snapshot, "delta_details", {}) or {})
        psych = dict(details.get("psych", {}))
        psych["dominant_traits"] = top_traits
        details["psych"] = psych
        snapshot.delta_details = details
        stats["traits_added"] = len(top_traits)

    # 3. motivation_drivers
    incoming_motives: list = []
    for p in valid:
        for m in (p.get("motivation_drivers") or []):
            incoming_motives.append(m)
    if incoming_motives:
        top_motives = _take_top(incoming_motives, max_motives)
        details = dict(getattr(snapshot, "delta_details", {}) or {})
        psych = dict(details.get("psych", {}))
        psych["motivation_drivers"] = top_motives
        details["psych"] = psych
        snapshot.delta_details = details
        stats["motives_added"] = len(top_motives)

    # 4. communication_style — НЕ перетираем уже выставленное
    existing_style = (getattr(snapshot, "communication_style", "") or "").strip()
    if not existing_style:
        latest_style = _latest_nonempty(valid, "communication_style")
        if latest_style:
            snapshot.communication_style = latest_style
            stats["communication_style_set"] = True

    # 5. personality_type и team_role — в delta_details.psych (нет полей в модели)
    for key, stats_key in (("personality_type", "personality_type_set"),
                           ("team_role", "team_role_set")):
        val = _latest_nonempty(valid, key)
        if val:
            details = dict(getattr(snapshot, "delta_details", {}) or {})
            psych = dict(details.get("psych", {}))
            psych[key] = val
            details["psych"] = psych
            snapshot.delta_details = details
            stats[stats_key] = True

    return stats


def filter_profiles_for_person(all_nodes: Iterable[dict], person_name: str) -> List[dict]:
    """Из всех узлов графа выделить PsychologicalProfile для данного человека
    (case-insensitive по полю name). Чистая функция."""
    if not person_name:
        return []
    target = person_name.casefold().strip()
    out: List[dict] = []
    for n in all_nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("_label") != "PsychologicalProfile":
            continue
        nm = (n.get("name") or "").casefold().strip()
        if nm == target or target in nm or nm in target:
            out.append(n)
    return out
