# -*- coding: utf-8 -*-
"""cogni-adapt — единый сервис адаптации вывода под человека (CogniLayer Ф1.2).

Методология (§5): ЛЮБОЙ вывод системы для конкретного человека — ответ в чате,
ТЗ, отчёт, обратная связь, пересказ стратегии — должен проходить через один
слой адаптации «как говорить именно с ним». До сих пор такая адаптация жила в
двух несвязанных местах со своей логикой: chat-персонализация
(`llm/system_prompt_builder`) и «Трансляция стратегии»
(`broadcast/strategy_relay._persona_frame/_personalize_prompt`). Дубли расходятся
и не видят новых когнитивных измерений (Ф1.1).

Здесь — общий контракт:
  Persona → CogniFrame → директива/преамбула для LLM.

CogniFrame собирает и коммуникативный профиль (стиль/словарь/метафоры/абстракция/
язык/реальные фразы), и когнитивные измерения из ``extended["cogni"]``
(эквалайзер/фаза/световой конус) — но ТОЛЬКО там, где сигнал реален
(confidence ≥ порога). Неуверенное измерение не рендерится: пустой фрейм лучше
выдуманного портрета (та же дисциплина честности, что и в промптах
кристаллизации). Never-raise: любая ошибка → пустой фрейм, вызов не падает.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.cogni.dimensions import get_cogni

logger = logging.getLogger(__name__)

# Порог «реального сигнала»: ниже — измерение считаем неуверенным и не рендерим.
_MIN_CONF = 0.4
# Порог значимого уклона оси эквалайзера (|value|): ближе к 0 — баланс, молчим.
_MIN_LEAN = 0.34

# Человекочитаемые ярлыки уклона осей эквалайзера (низкий полюс / высокий полюс).
_AXIS_LABELS: Dict[str, tuple[str, str]] = {
    "action_analysis": ("склонен к действию (сначала делает)",
                        "склонен к анализу (сначала считает)"),
    "self_others": ("фокус на своей зоне/результате",
                    "фокус на людях и команде"),
    "chaos_order": ("ценит гибкость выше регламента",
                    "ценит структуру и порядок"),
    "concrete_abstract": ("мыслит конкретикой, цифрами, фактами",
                         "мыслит концепциями и метафорами"),
}

_PHASE_LABELS = {
    "growth": "фаза роста",
    "peak": "фаза пика",
    "transition": "фаза перехода",
    "consolidation": "фаза консолидации",
}


@dataclass
class CogniFrame:
    """Фрейм мышления человека — то, как с ним говорить. Все поля опциональны:
    заполнено только то, что реально измерено. Пустой фрейм валиден."""

    language: Optional[str] = None
    style: Optional[str] = None
    vocabulary: Optional[str] = None
    abstraction: Optional[str] = None
    metaphors: List[str] = field(default_factory=list)
    speech_examples: List[str] = field(default_factory=list)
    # из когнитивных измерений (Ф1.1) — только уверенные
    equalizer_leanings: List[str] = field(default_factory=list)
    phase: Optional[str] = None
    light_cone: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.language, self.style, self.vocabulary, self.abstraction,
            self.metaphors, self.speech_examples, self.equalizer_leanings,
            self.phase, self.light_cone,
        ])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language, "style": self.style,
            "vocabulary": self.vocabulary, "abstraction": self.abstraction,
            "metaphors": list(self.metaphors),
            "speech_examples": list(self.speech_examples),
            "equalizer_leanings": list(self.equalizer_leanings),
            "phase": self.phase, "light_cone": list(self.light_cone),
        }


EMPTY_FRAME = CogniFrame()


def _frame_from_persona(persona: Any) -> CogniFrame:
    """Собрать фрейм из объекта Persona (синхронно, never-raise)."""
    fr = CogniFrame()
    if persona is None:
        return fr
    try:
        cc = getattr(persona, "communication_cognitive", None)
        if cc is not None:
            style = getattr(cc, "preferred_style", None)
            fr.style = getattr(style, "value", None) or (str(style) if style else None)
            fr.vocabulary = getattr(cc, "vocabulary_register", None)
            lvl = getattr(cc, "abstraction_level", None)
            fr.abstraction = getattr(lvl, "value", None) or (str(lvl) if lvl else None)
            fr.language = getattr(cc, "preferred_language", None) or "ru"
            fr.metaphors = [str(m) for m in (getattr(cc, "metaphor_domains", None) or []) if m][:5]
            patterns = list(getattr(cc, "speech_patterns", None) or [])[:5]
            fr.speech_examples = [
                str(sp.get("phrase", "")) for sp in patterns
                if isinstance(sp, dict) and sp.get("phrase")
            ][:3]
    except Exception:
        logger.debug("cogni-adapt: communication frame skipped", exc_info=True)

    # когнитивные измерения — только уверенные (Ф1.1)
    try:
        cogni = get_cogni(getattr(persona, "extended", None))
        fr.equalizer_leanings = _equalizer_leanings(cogni.get("equalizer") or {})
        fr.phase = _phase_label(cogni.get("phase_state") or {})
        fr.light_cone = _light_cone_lines(cogni.get("light_cone") or {})
    except Exception:
        logger.debug("cogni-adapt: dimensions skipped", exc_info=True)

    # стиль DEFAULT/пустой — не сигнал, убираем, чтобы не рендерить шум
    if fr.style in (None, "", "default"):
        fr.style = None
    if fr.abstraction in ("balanced",):
        fr.abstraction = None
    return fr


def _equalizer_leanings(eq: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for axis, (low, high) in _AXIS_LABELS.items():
        a = eq.get(axis)
        if not isinstance(a, dict):
            continue
        try:
            val = float(a.get("value") or 0.0)
            conf = float(a.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if conf < _MIN_CONF or abs(val) < _MIN_LEAN:
            continue
        out.append(high if val > 0 else low)
    return out


def _phase_label(ps: Dict[str, Any]) -> Optional[str]:
    try:
        if float(ps.get("confidence") or 0.0) < _MIN_CONF:
            return None
    except (TypeError, ValueError):
        return None
    state = ps.get("state")
    label = _PHASE_LABELS.get(state)
    note = str(ps.get("note") or "").strip()
    if not label:
        return None
    return f"{label} ({note})" if note else label


def _light_cone_lines(lc: Dict[str, Any]) -> List[str]:
    try:
        if float(lc.get("confidence") or 0.0) < _MIN_CONF:
            return []
    except (TypeError, ValueError):
        return []
    out: List[str] = []
    if lc.get("horizon"):
        out.append(f"горизонт мышления: {lc['horizon']}")
    if lc.get("scale"):
        out.append(f"масштаб последствий: {lc['scale']}")
    if lc.get("depth"):
        out.append(f"глубина последствий: {lc['depth']}")
    return out


async def load_frame(user_id: str, *, persona: Any = None) -> CogniFrame:
    """Загрузить фрейм мышления для аккаунта. Если ``persona`` передана —
    используем её (без повторного чтения из стора). Пусто — валидный ответ."""
    if persona is None and user_id:
        try:
            from backend.core.persona.store import get_persona_store
            store = get_persona_store()
            persona = store.get(user_id)
            if asyncio.iscoroutine(persona):
                persona = await persona
        except Exception:
            logger.debug("cogni-adapt: persona load skipped", exc_info=True)
            return CogniFrame()
    return _frame_from_persona(persona)


def frame_block(frame: CogniFrame) -> str:
    """Описание фрейма человека для вставки в промпт (bullet-строки).

    Пустой фрейм → честная строка «профиль не накоплен», чтобы генератор писал
    просто и не выдумывал персональность."""
    if frame is None or frame.is_empty():
        return ("- профиль общения пока не накоплен: пиши просто, по-русски, "
                "по делу, без канцелярита и без выдуманной «персональности»")
    lines: List[str] = []
    if frame.language:
        lines.append(f"- язык ответа: {frame.language}")
    if frame.style:
        lines.append(f"- стиль общения: {frame.style}")
    if frame.vocabulary:
        lines.append(f"- словарь/регистр: {frame.vocabulary}")
    if frame.abstraction:
        lines.append(f"- уровень абстракции: {frame.abstraction}")
    if frame.metaphors:
        lines.append("- метафоры, которые человек сам использует: "
                     + ", ".join(frame.metaphors[:5]))
    if frame.equalizer_leanings:
        lines.append("- когнитивные склонности: "
                     + "; ".join(frame.equalizer_leanings))
    if frame.phase:
        lines.append(f"- фаза развития: {frame.phase}")
    if frame.light_cone:
        lines.append("- " + "; ".join(frame.light_cone))
    if frame.speech_examples:
        lines.append("- примеры его реальных фраз: "
                     + " | ".join(frame.speech_examples[:3]))
    return "\n".join(lines)


# Правила адаптации — общие для любого вывода. Дисциплина честности: адаптируем
# ФОРМУ, не содержание; персональность не выдумываем.
_ADAPT_RULES = (
    "ПРАВИЛА АДАПТАЦИИ:\n"
    "1. Адаптируй только ФОРМУ (язык, стиль, метафоры, уровень детализации) — "
    "НЕ добавляй фактов, цифр или обещаний, которых нет в исходном материале.\n"
    "2. Используй его метафоры/склонности, только если они заданы выше; иначе — "
    "просто и конкретно, без выдуманной «подстройки».\n"
    "3. Смысл сохраняется дословно; меняется подача, не содержание."
)


def frame_directive(frame: CogniFrame, *, header: str = "ФРЕЙМ МЫШЛЕНИЯ ЧЕЛОВЕКА") -> str:
    """Готовый блок-директива для промпта: описание фрейма + правила адаптации.

    Вставляется генератором ПЕРЕД материалом, который надо подать под человека."""
    return f"{header}:\n{frame_block(frame)}\n\n{_ADAPT_RULES}"


def frame_preamble(frame: CogniFrame) -> str:
    """Компактная преамбула для system prompt (детерминированно, без LLM).

    Аналог chat-персонализации, но из единого фрейма. Пустой фрейм → ''."""
    if frame is None or frame.is_empty():
        return ""
    return ("User cognitive frame (adapt form, not facts):\n"
            + frame_block(frame))


def adapt_system_prompt(base_system: Optional[str], frame: CogniFrame) -> Optional[str]:
    """Приклеить преамбулу фрейма к базовому system prompt (перед ним)."""
    pre = frame_preamble(frame)
    if not pre:
        return base_system
    if not base_system:
        return pre
    return f"{pre}\n\n{base_system}"


if __name__ == "__main__":  # быстрый смоук без внешних зависимостей
    from backend.core.cogni.dimensions import (
        default_cogni, set_equalizer_axis, set_phase_state,
    )

    class _CC:
        class _S:
            value = "narrative"
        class _A:
            value = "abstract"
        preferred_style = _S()
        vocabulary_register = "tech_business"
        abstraction_level = _A()
        preferred_language = "ru"
        metaphor_domains = ["sports", "engineering"]
        speech_patterns = [{"phrase": "погнали", "confidence": 0.9}]

    cog = default_cogni()
    set_equalizer_axis(cog, "chaos_order", 0.8, 0.7)      # уверенный уклон → рендерим
    set_equalizer_axis(cog, "self_others", 0.2, 0.9)      # слабый уклон → молчим
    set_equalizer_axis(cog, "action_analysis", -0.9, 0.1) # низкая conf → молчим
    set_phase_state(cog, "growth", 0.6, "найм x2")

    class _P:
        communication_cognitive = _CC()
        extended = {"cogni": cog}

    fr = _frame_from_persona(_P())
    assert fr.language == "ru" and fr.style == "narrative"
    assert "engineering" in fr.metaphors
    assert any("структ" in s for s in fr.equalizer_leanings), fr.equalizer_leanings
    assert not any("фокус на" in s for s in fr.equalizer_leanings)  # слабый/неувер.
    assert fr.phase and "рост" in fr.phase
    blk = frame_block(fr)
    assert "narrative" in blk and "найм x2" in blk
    d = frame_directive(fr)
    assert "ПРАВИЛА АДАПТАЦИИ" in d and "НЕ добавляй фактов" in d

    empty = frame_block(CogniFrame())
    assert "не накоплен" in empty
    assert frame_preamble(CogniFrame()) == ""
    assert adapt_system_prompt("BASE", CogniFrame()) == "BASE"
    print("cogni.adapt smoke OK")
