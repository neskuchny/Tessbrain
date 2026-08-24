# -*- coding: utf-8 -*-
"""Сохранение полного психоанализа встречи — то, что раньше выбрасывалось.

PsychologicalAnalyzer считает шесть блоков на каждой встрече, но в граф
писались только два: профили личности и риски конфликтов. Скрытые мотивы,
групповая динамика (альянсы, кто на кого влияет), прогнозы поведения и
рекомендации по управлению вычислялись — деньги на LLM тратились — и
выбрасывались. По решению владельца продукта теперь сохраняются.

Дизайн:
- Один тип узла PsychInsight с полем kind (motives / group_dynamics /
  behavior_forecast / recommendation) вместо четырёх новых типов — меньше
  раздувания схемы, различие по свойству.
- Мотивы и прогнозы поведения помечаются sensitive=True и получают
  повышенный гриф: «признаки манипуляции» и «стремление к власти» — самое
  острое содержимое системы, оно не должно всплывать в карточке для всех.
- Эти узлы НЕ индексируются в векторный поиск и BM25 — сознательно.
  Запрос «кто у нас манипулирует» не должен работать через общий поиск;
  чтение этого слоя — отдельное решение с отдельным контролем доступа.

Модуль чистый (stdlib): строит payload'ы узлов и рёбер, ничего не пишет —
запись делает knowledge_sync теми же create_node/create_relationship,
что и остальные узлы встречи.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

NODE_LABEL = "PsychInsight"
REL_TYPE = "HAS_PSYCH_INSIGHT"

# Гриф для чувствительных видов. На файловом графовом бэкенде уровень 4
# скрывает узел от всех, кроме admin/founder (ROLE_MAX_ACCESS_LEVEL);
# ограничение Neo4j-пути честно описано в docs («Честно о границах»).
SENSITIVE_ACCESS_LEVEL = 4

KIND_MOTIVES = "motives"
KIND_DYNAMICS = "group_dynamics"
KIND_FORECAST = "behavior_forecast"
KIND_RECOMMENDATION = "recommendation"

# Виды, которые никогда не должны попадать в общие поисковые индексы.
SENSITIVE_KINDS = frozenset({KIND_MOTIVES, KIND_FORECAST})

_MAX_ITEMS = 20          # защита от разбухшего LLM-ответа
_MAX_TEXT = 2000         # кап на текстовое поле узла


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value or "")[:limit]


def _clip_list(items: Any, limit: int = 10) -> List[str]:
    out = []
    for it in items if isinstance(items, list) else []:
        s = _clip(it, 500)
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def build_psych_insight_payloads(
    psych: Dict[str, Any],
    *,
    meeting_id: str,
    user_id: str,
    created_at: str,
) -> List[Dict[str, Any]]:
    """Payload'ы узлов PsychInsight из результата психоанализа.

    Возвращает список dict:
      {node_id, properties, person_name | None}
    person_name — к кому привязать ребро Person -HAS_PSYCH_INSIGHT->
    (резолв имени в person_id остаётся за вызывающим). Meeting-ребро
    вешается на каждый узел безусловно.
    """
    out: List[Dict[str, Any]] = []
    psych = psych or {}

    def _base(kind: str, idx: int) -> Tuple[str, Dict[str, Any]]:
        node_id = f"psychinsight_{kind}_{meeting_id}_{idx}"
        props: Dict[str, Any] = {
            "kind": kind,
            "meeting_id": meeting_id,
            "user_id": user_id,
            "created_at": created_at,
            "source_agent": "psychological_analyzer",
        }
        if kind in SENSITIVE_KINDS:
            props["sensitive"] = True
            props["access_level"] = SENSITIVE_ACCESS_LEVEL
        return node_id, props

    # ── Скрытые мотивы: по человеку ──────────────────────────────────────
    for i, m in enumerate((psych.get("hidden_motives") or [])[:_MAX_ITEMS]):
        if not isinstance(m, dict):
            continue
        who = _clip(m.get("participant"), 200)
        if not who:
            # Мотив без человека бесполезен и опасен: висел бы в графе как
            # анонимное обвинение. Не сохраняем.
            continue
        node_id, props = _base(KIND_MOTIVES, i)
        props.update({
            "name": f"Мотивы: {who}",
            "participant": who,
            "stated_position": _clip(m.get("stated_position")),
            "true_intentions": _clip_list(m.get("true_intentions")),
            "personal_agenda": _clip_list(m.get("personal_agenda")),
            "fears": _clip_list(m.get("fears")),
            "power_seeking": _clip(m.get("power_seeking")),
            "defense_mechanisms": _clip_list(m.get("defense_mechanisms")),
            "manipulation_signs": _clip_list(m.get("manipulation_signs")),
            "authenticity_score": float(m.get("authenticity_score") or 0),
            "risk_level": _clip(m.get("risk_level"), 20),
        })
        out.append({"node_id": node_id, "properties": props, "person_name": who})

    # ── Групповая динамика: одна на встречу ──────────────────────────────
    dyn = psych.get("group_dynamics") or {}
    if isinstance(dyn, dict) and dyn:
        node_id, props = _base(KIND_DYNAMICS, 0)
        props.update({
            "name": "Групповая динамика встречи",
            "trust_level": _clip(dyn.get("trust_level"), 20),
            "psychological_safety": _clip(dyn.get("psychological_safety"), 20),
            "hidden_conflicts": (dyn.get("hidden_conflicts") or [])[:10],
            "alliances": (dyn.get("alliances") or [])[:10],
            "power_dynamics": (dyn.get("power_dynamics") or [])[:10],
            "collective_fears": _clip_list(dyn.get("collective_fears")),
            "team_culture": dyn.get("team_culture") or {},
            "group_maturity": _clip(dyn.get("group_maturity"), 40),
            "improvement_areas": _clip_list(dyn.get("improvement_areas")),
        })
        out.append({"node_id": node_id, "properties": props, "person_name": None})

    # ── Прогнозы поведения: по человеку ──────────────────────────────────
    for i, p in enumerate((psych.get("behavioral_predictions") or [])[:_MAX_ITEMS]):
        if not isinstance(p, dict):
            continue
        who = _clip(p.get("participant"), 200)
        if not who:
            continue
        node_id, props = _base(KIND_FORECAST, i)
        props.update({
            "name": f"Прогноз поведения: {who}",
            "participant": who,
            "stress_reaction": _clip(p.get("stress_reaction")),
            "conflict_behavior": _clip(p.get("conflict_behavior")),
            "change_adaptation": _clip(p.get("change_adaptation")),
            "deadline_pressure": _clip(p.get("deadline_pressure")),
            "criticism_response": _clip(p.get("criticism_response")),
            "leadership_emergence": _clip(p.get("leadership_emergence")),
            "collaboration_style": _clip(p.get("collaboration_style")),
            "likely_triggers": _clip_list(p.get("likely_triggers")),
            "positive_triggers": _clip_list(p.get("positive_triggers")),
            "prediction_confidence": float(p.get("prediction_confidence") or 0),
        })
        out.append({"node_id": node_id, "properties": props, "person_name": who})

    # ── Рекомендации по управлению ───────────────────────────────────────
    for i, r in enumerate((psych.get("management_recommendations") or [])[:_MAX_ITEMS]):
        if not isinstance(r, dict):
            continue
        text = _clip(r.get("recommendation"))
        if not text:
            continue
        node_id, props = _base(KIND_RECOMMENDATION, i)
        target = _clip(r.get("target"), 200)
        # «вся команда» и подобное — не человек; ребро только к встрече.
        person = target if target and not _is_team_target(target) else None
        props.update({
            "name": f"Рекомендация: {text[:80]}",
            "category": _clip(r.get("category"), 100),
            "recommendation": text,
            "target": target,
            "rationale": _clip(r.get("rationale")),
            "implementation": _clip(r.get("implementation")),
            "expected_outcome": _clip(r.get("expected_outcome")),
            "priority": _clip(r.get("priority"), 20),
            "timing": _clip(r.get("timing"), 200),
        })
        out.append({"node_id": node_id, "properties": props, "person_name": person})

    return out


_TEAM_MARKERS = ("команд", "все ", "всей", "всех", "team", "everyone", "группа", "групп")


def _is_team_target(target: str) -> bool:
    t = target.strip().lower()
    return any(m in t for m in _TEAM_MARKERS)
