# -*- coding: utf-8 -*-
"""Когнитивные измерения профиля — формализация слота ``extended["cogni"]``
(CogniLayer Ф1.1).

`core/persona/models.py` типизированно покрывает 2–3 измерения из ~10
методологии (стиль речи, метафоры, уровень абстракции, поведение). Остальные
измерения CogniLayer — витки развития по доменам, световой конус, эквалайзер,
фазовое состояние — жили в модели как недокументированный free-form слот
``extended["cogni"]``: место было, но не было ни схемы, ни валидации, ни
аксессоров, поэтому туда никто ничего не клал.

Этот модуль даёт формальный контракт слота: дефолты (всё «неизвестно»),
нормализацию (клампинг диапазонов, отбрасывание мусора) и аксессоры для
дисциплинированной записи. Ключевой принцип — тот же, что во всех промптах
кристаллизации: **пустое лучше выдуманного**. Каждое измерение по умолчанию
`unknown` с `confidence=0.0`; значение появляется, только когда есть реальный
сигнал (из встреч/аккаунта), и всегда несёт свою уверенность и evidence.

Слот сериализуется в `Persona.to_public_dict()["extended"]["cogni"]` — поэтому
`normalize_cogni` вызывается на чтении, чтобы наружу шла валидная структура
даже если внутри лежал частично заполненный/битый dict.

Never-raise: любая нормализация возвращает валидный скелет, не роняет вызов.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

COGNI_SCHEMA_VERSION = "1.0"

# 4 витка развития — та же шкала, что у wisdom-движка (vitek_phase_min/max).
VITEK_MIN, VITEK_MAX = 1, 4
VITKI_DOMAINS = ("professional", "technical", "leadership", "personal")

# Эквалайзер: оси-ползунки в диапазоне [-1..+1], 0 = баланс. Стартовый набор из
# методологии + concrete_abstract (его мы уже честно измеряем в персоне —
# CommunicationCognitive.abstraction_level, см. derive_from_persona).
#   action_analysis  : -1 действие  ↔ +1 анализ
#   self_others      : -1 на себя   ↔ +1 на других
#   chaos_order      : -1 гибкость  ↔ +1 структура
#   concrete_abstract: -1 конкретика↔ +1 абстракция
EQUALIZER_AXES = ("action_analysis", "self_others", "chaos_order", "concrete_abstract")

# Фазовое состояние — из wisdom-движка (рост/пик/переход/консолидация).
PHASE_STATES = ("growth", "peak", "transition", "consolidation")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(x: Any, lo: float, hi: float, default: float = 0.0) -> float:
    """Число в [lo..hi]; мусор → default. Никогда не бросает."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _conf(x: Any) -> float:
    """Уверенность 0..1 (мусор → 0.0)."""
    return _clamp(x, 0.0, 1.0, 0.0)


def _str_or_none(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def _evidence(x: Any, *, limit: int = 8) -> List[str]:
    """Список коротких строк-подтверждений (цитаты/meeting_id). Мусор → []."""
    if not isinstance(x, (list, tuple)):
        return []
    out: List[str] = []
    for item in x:
        s = str(item).strip()
        if s:
            out.append(s[:400])
        if len(out) >= limit:
            break
    return out


# ── дефолтные скелеты (всё «неизвестно», честная нулевая уверенность) ──

def _default_vitok() -> Dict[str, Any]:
    return {"level": None, "confidence": 0.0, "evidence": []}


def _default_light_cone() -> Dict[str, Any]:
    # горизонт/масштаб/глубина последствий — свободные строки ИЗ данных, не enum,
    # чтобы не навязывать словарь и не провоцировать выдумку категорий.
    return {"horizon": None, "scale": None, "depth": None,
            "confidence": 0.0, "evidence": []}


def _default_axis() -> Dict[str, Any]:
    return {"value": 0.0, "confidence": 0.0}


def _default_phase_state() -> Dict[str, Any]:
    return {"state": None, "confidence": 0.0, "note": ""}


def default_cogni() -> Dict[str, Any]:
    """Валидный пустой профиль когнитивных измерений (всё unknown)."""
    return {
        "schema_version": COGNI_SCHEMA_VERSION,
        "vitki": {d: _default_vitok() for d in VITKI_DOMAINS},
        "light_cone": _default_light_cone(),
        "equalizer": {ax: _default_axis() for ax in EQUALIZER_AXES},
        "phase_state": _default_phase_state(),
        "updated_at": None,
    }


# ── нормализация (клампинг + отбрасывание мусора) ──

def normalize_cogni(raw: Any) -> Dict[str, Any]:
    """Привести любой (частичный/битый) cogni-dict к валидной схеме.

    Неизвестные ключи отбрасываются, значения клампятся в диапазоны,
    отсутствующие измерения добираются дефолтами. Никогда не бросает —
    на вход можно давать None / не-dict."""
    out = default_cogni()
    if not isinstance(raw, dict):
        return out

    # витки по доменам
    vitki_in = raw.get("vitki")
    if isinstance(vitki_in, dict):
        for domain in VITKI_DOMAINS:
            v = vitki_in.get(domain)
            if not isinstance(v, dict):
                continue
            lvl = v.get("level")
            if lvl is not None:
                lvl = int(_clamp(lvl, VITEK_MIN, VITEK_MAX, VITEK_MIN))
            out["vitki"][domain] = {
                "level": lvl,
                "confidence": _conf(v.get("confidence")),
                "evidence": _evidence(v.get("evidence")),
            }

    # световой конус
    lc = raw.get("light_cone")
    if isinstance(lc, dict):
        out["light_cone"] = {
            "horizon": _str_or_none(lc.get("horizon")),
            "scale": _str_or_none(lc.get("scale")),
            "depth": _str_or_none(lc.get("depth")),
            "confidence": _conf(lc.get("confidence")),
            "evidence": _evidence(lc.get("evidence")),
        }

    # эквалайзер
    eq = raw.get("equalizer")
    if isinstance(eq, dict):
        for ax in EQUALIZER_AXES:
            a = eq.get(ax)
            if not isinstance(a, dict):
                continue
            out["equalizer"][ax] = {
                "value": _clamp(a.get("value"), -1.0, 1.0, 0.0),
                "confidence": _conf(a.get("confidence")),
            }

    # фазовое состояние
    ps = raw.get("phase_state")
    if isinstance(ps, dict):
        state = _str_or_none(ps.get("state"))
        if state is not None and state not in PHASE_STATES:
            state = None  # неизвестная фаза — не тащим выдуманную категорию
        out["phase_state"] = {
            "state": state,
            "confidence": _conf(ps.get("confidence")),
            "note": (str(ps.get("note") or "").strip())[:600],
        }

    out["updated_at"] = _str_or_none(raw.get("updated_at"))
    return out


# ── чтение/запись поверх persona.extended ──

def get_cogni(extended: Any) -> Dict[str, Any]:
    """Нормализованный cogni из ``persona.extended`` (dict или None)."""
    if isinstance(extended, dict):
        return normalize_cogni(extended.get("cogni"))
    return default_cogni()


def set_vitok(
    cogni: Dict[str, Any], domain: str, level: Optional[int],
    confidence: float, evidence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Записать виток по домену. Домен вне VITKI_DOMAINS игнорируется."""
    if domain not in VITKI_DOMAINS:
        return cogni
    lvl = None if level is None else int(_clamp(level, VITEK_MIN, VITEK_MAX, VITEK_MIN))
    cogni.setdefault("vitki", {})[domain] = {
        "level": lvl, "confidence": _conf(confidence),
        "evidence": _evidence(evidence or []),
    }
    cogni["updated_at"] = _now_iso()
    return cogni


def set_light_cone(
    cogni: Dict[str, Any], *, horizon: Any = None, scale: Any = None,
    depth: Any = None, confidence: float = 0.0,
    evidence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cogni["light_cone"] = {
        "horizon": _str_or_none(horizon), "scale": _str_or_none(scale),
        "depth": _str_or_none(depth), "confidence": _conf(confidence),
        "evidence": _evidence(evidence or []),
    }
    cogni["updated_at"] = _now_iso()
    return cogni


def set_equalizer_axis(
    cogni: Dict[str, Any], axis: str, value: float, confidence: float,
) -> Dict[str, Any]:
    """Ось эквалайзера в [-1..+1]. Ось вне EQUALIZER_AXES игнорируется."""
    if axis not in EQUALIZER_AXES:
        return cogni
    cogni.setdefault("equalizer", {})[axis] = {
        "value": _clamp(value, -1.0, 1.0, 0.0), "confidence": _conf(confidence),
    }
    cogni["updated_at"] = _now_iso()
    return cogni


def set_phase_state(
    cogni: Dict[str, Any], state: Optional[str], confidence: float, note: str = "",
) -> Dict[str, Any]:
    if state is not None and state not in PHASE_STATES:
        state = None
    cogni["phase_state"] = {
        "state": state, "confidence": _conf(confidence),
        "note": (note or "").strip()[:600],
    }
    cogni["updated_at"] = _now_iso()
    return cogni


def apply_to_persona(persona: Any, cogni: Dict[str, Any]) -> None:
    """Положить нормализованный cogni в ``persona.extended["cogni"]``."""
    ext = getattr(persona, "extended", None)
    if not isinstance(ext, dict):
        ext = {}
        try:
            persona.extended = ext
        except Exception:
            return
    ext["cogni"] = normalize_cogni(cogni)


# ── слияние двух источников (аккаунт + встречи) ──

def _pick_measure(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Из двух измерений взять более уверенное (при равенстве — b/incoming)."""
    return b if _conf(b.get("confidence")) >= _conf(a.get("confidence")) else a


def merge_cogni(base: Any, incoming: Any) -> Dict[str, Any]:
    """Слить два cogni-профиля, оставляя более уверенное значение по каждому
    измерению. Симметрично к нормализации — оба входа нормализуются."""
    a = normalize_cogni(base)
    b = normalize_cogni(incoming)
    out = default_cogni()

    for domain in VITKI_DOMAINS:
        va, vb = a["vitki"][domain], b["vitki"][domain]
        # неизвестный уровень (None) уступает известному независимо от conf
        if vb["level"] is None and va["level"] is not None:
            out["vitki"][domain] = va
        elif va["level"] is None and vb["level"] is not None:
            out["vitki"][domain] = vb
        else:
            out["vitki"][domain] = _pick_measure(va, vb)

    out["light_cone"] = _pick_measure(a["light_cone"], b["light_cone"])
    for ax in EQUALIZER_AXES:
        out["equalizer"][ax] = _pick_measure(a["equalizer"][ax], b["equalizer"][ax])
    out["phase_state"] = _pick_measure(a["phase_state"], b["phase_state"])
    out["updated_at"] = _now_iso()
    return out


# ── честная деривация из уже типизированных полей персоны ──

_ABSTRACTION_TO_AXIS = {"concrete": -1.0, "balanced": 0.0, "abstract": 1.0}


def derive_from_persona(persona: Any) -> Dict[str, Any]:
    """Заполнить cogni ТОЛЬКО из сигналов, которые персона уже честно измеряет.

    Единственный прямой маппинг сегодня — ``abstraction_level`` (concrete/
    balanced/abstract) в ось эквалайзера ``concrete_abstract``: это буквально то
    же самое измерение, а не догадка. Всё остальное остаётся `unknown` до
    появления реального сигнала из встреч (Ф1.2+). Пустое лучше выдуманного."""
    cogni = get_cogni(getattr(persona, "extended", None))
    cc = getattr(persona, "communication_cognitive", None)
    level = getattr(getattr(cc, "abstraction_level", None), "value", None)
    if level in _ABSTRACTION_TO_AXIS:
        # уверенность скромная: один сигнал, не сойка измерений
        set_equalizer_axis(cogni, "concrete_abstract",
                            _ABSTRACTION_TO_AXIS[level], 0.5)
    return cogni


if __name__ == "__main__":  # быстрый смоук
    c = default_cogni()
    assert c["vitki"]["professional"]["level"] is None
    assert set(c["equalizer"]) == set(EQUALIZER_AXES)

    set_vitok(c, "leadership", 9, 1.5, ["m1", "m2"])  # клампится: level→4, conf→1
    assert c["vitki"]["leadership"]["level"] == VITEK_MAX
    assert c["vitki"]["leadership"]["confidence"] == 1.0
    set_vitok(c, "bogus", 3, 0.5)  # неизвестный домен — игнор
    assert "bogus" not in c["vitki"]

    set_equalizer_axis(c, "chaos_order", 2.0, 0.9)  # value клампится к +1
    assert c["equalizer"]["chaos_order"]["value"] == 1.0
    set_phase_state(c, "peak", 0.7, "рост выручки")
    assert c["phase_state"]["state"] == "peak"
    set_phase_state(c, "invented", 0.9)  # неизвестная фаза → None
    assert c["phase_state"]["state"] is None

    dirty = {"vitki": {"technical": {"level": "3", "confidence": "0.8"}},
             "equalizer": {"self_others": {"value": "-0.4"}},
             "phase_state": {"state": "growth"}, "junk": 1}
    n = normalize_cogni(dirty)
    assert n["vitki"]["technical"]["level"] == 3
    assert abs(n["equalizer"]["self_others"]["value"] + 0.4) < 1e-9
    assert "junk" not in n

    m = merge_cogni({"vitki": {"personal": {"level": 2, "confidence": 0.3}}},
                    {"vitki": {"personal": {"level": 3, "confidence": 0.9}}})
    assert m["vitki"]["personal"]["level"] == 3  # выбрали более уверенное

    class _CC:
        class _L:
            value = "abstract"
        abstraction_level = _L()

    class _P:
        extended: dict = {}
        communication_cognitive = _CC()

    d = derive_from_persona(_P())
    assert d["equalizer"]["concrete_abstract"]["value"] == 1.0
    print("cogni.dimensions smoke OK")
