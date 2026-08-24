# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Cogni Dimensions Agent (CogniLayer Ф1)
======================================================

Извлекает когнитивные измерения участников из транскрипта встречи:
витки развития по доменам, световой конус (горизонт мышления),
склонности-эквалайзер и фазовое состояние. Выход питает
``extended["cogni"]`` персоны через схему core/cogni/dimensions.py.

Дисциплина фактов — жёстче обычного, потому что это профиль ЧЕЛОВЕКА:
- каждое заявленное измерение обязано нести ДОСЛОВНУЮ цитату из транскрипта;
  цитата дополнительно проверяется кодом (grounding-фильтр) — не нашлась
  в транскрипте → измерение отбрасывается;
- нет сигнала → null, участник почти не говорил → не профилируется вовсе;
- один транскрипт ≠ диагноз: потолок уверенности 0.7, наблюдение, а не ярлык.

Словарь выходных ключей совпадает со схемой dimensions.py СИМВОЛ В СИМВОЛ
(домены/оси/фазы) — нормализация схемы молча отбрасывает неизвестные категории,
поэтому вольные синонимы означают потерю данных.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .base_agent import BaseExtractionAgent
from backend.core.cogni.dimensions import (
    EQUALIZER_AXES,
    PHASE_STATES,
    VITEK_MAX,
    VITEK_MIN,
    VITKI_DOMAINS,
)

logger = logging.getLogger(__name__)

# Один транскрипт — наблюдение, не диагноз: уверенность выше не пропускаем.
_SINGLE_MEETING_CONF_CAP = 0.7
# Минимальная длина цитаты-подтверждения (символов) — «да» не доказательство.
_MIN_QUOTE_LEN = 12


class CogniDimensionsAgent(BaseExtractionAgent):
    """Извлечение когнитивных измерений (CogniLayer) по участникам встречи."""

    def get_entity_type(self) -> str:
        return "cogni_profile"

    def get_instruction(self) -> str:
        return '''Ты — COGNI DIMENSIONS AGENT. Твоя задача — по РЕАЛЬНЫМ репликам участников встречи оценить их когнитивные измерения. Это профиль живого человека: любая выдумка здесь вреднее, чем пропуск.

ЖЕЛЕЗНЫЕ ПРАВИЛА (важнее полноты):
1. Каждое ненулевое измерение ОБЯЗАНО нести "quote" — ДОСЛОВНУЮ цитату из транскрипта (реплику этого человека), которая его подтверждает. Цитата проверяется программно: перефразированная или выдуманная цитата = измерение выбрасывается.
2. Нет явного сигнала в речи — ставь null и confidence 0. Пустой профиль лучше выдуманного.
3. Участника, у которого мало реплик (пара коротких фраз), НЕ профилируй вообще — не включай в массив.
4. Имена — только реальные имена из транскрипта или из speaker_mapping в контексте встречи. Метки "Speaker N" без расшифровки не используй. Не выдумывай участников.
5. Один транскрипт — это наблюдение, не диагноз. Максимальная confidence — 0.7, и только при нескольких независимых подтверждениях в речи. Одна реплика — не выше 0.4.
6. Оценивай только по тому, ЧТО и КАК человек говорит сам. Не по тому, что о нём говорят другие, и не по его должности.

ИЗМЕРЕНИЯ (ключи — строго как здесь, вольные синонимы теряются):

А) vitki — виток развития по доменам. Домены строго: "professional", "technical", "leadership", "personal". Шкала level 1-4:
   1 — исполняет по инструкции, ждёт указаний, вопросы «что мне делать»;
   2 — уверенно решает сам в своей зоне, оптимизирует своё;
   3 — видит систему целиком, ведёт других, говорит про процессы и связи;
   4 — создаёт новое (системы/направления), мыслит стратегией и горизонтами.
   Оценивай ТОЛЬКО домены, по которым в речи есть маркеры. Остальные — не включай.

Б) light_cone — «световой конус» мышления, свободные короткие формулировки ИЗ речи:
   horizon — каким временным горизонтом человек оперирует (то, что он сам называет: «до конца недели», «на год вперёд»);
   scale — масштаб, о котором думает (своя задача / команда / компания / рынок);
   depth — насколько глубоко прослеживает последствия (первый эффект или цепочки «а это приведёт к...»).
   Не заполняй поле, если в речи нет соответствующих формулировок.

В) equalizer — склонности, value от -1 до +1 (0 = баланса/сигнала нет). Оси строго:
   "action_analysis": -1 сначала делает («давайте просто попробуем») ↔ +1 сначала анализирует («сначала посчитаем/проверим»);
   "self_others": -1 фокус на своей зоне/результате ↔ +1 фокус на людях/команде;
   "chaos_order": -1 ценит гибкость, против регламентов ↔ +1 ценит структуру, процессы, порядок;
   "concrete_abstract": -1 цифры/факты/примеры ↔ +1 концепции/метафоры/принципы.
   Включай ось только при явном уклоне в речи. Слабый/противоречивый сигнал — не включай.

Г) phase_state — фазовое состояние человека сейчас, строго одно из: "growth", "peak", "transition", "consolidation" — либо null. Заполняй только если человек сам говорит о своём состоянии/этапе («вникаю в новое», «на пределе», «меняю роль», «навожу порядок в том, что есть»). Поле note — одна строка: его формулировка.

ФОРМАТ ВЫВОДА — JSON-массив, по объекту на участника С ДОСТАТОЧНОЙ РЕЧЬЮ:
```json
[
  {
    "name": "Имя из транскрипта",
    "vitki": {
      "leadership": {"level": 3, "confidence": 0.5, "quote": "дословная реплика-подтверждение"}
    },
    "light_cone": {"horizon": "квартал", "scale": "отдел", "depth": null, "confidence": 0.4, "quote": "дословная реплика"},
    "equalizer": {
      "action_analysis": {"value": 0.6, "confidence": 0.5, "quote": "дословная реплика"}
    },
    "phase_state": {"state": "transition", "confidence": 0.4, "note": "его формулировка", "quote": "дословная реплика"},
    "confidence": 0.5
  }
]
```
Включай в объект только измерения с реальным сигналом (пустые ключи опускай). Если ни у кого нет достаточной речи — верни []. Пустой массив — валидный и честный ответ.'''

    # ── нормализация и grounding ──

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Канонизировать профиль участника: только известные домены/оси/фазы,
        клампинг диапазонов, обязательная цитата на каждое измерение."""
        item = super().normalize(item)
        if not item:
            return None
        name = str(item.get("name") or "").strip()
        if not name or name.lower().startswith(("speaker", "unknown")):
            return None

        out: Dict[str, Any] = {
            "name": name,
            "confidence": self._conf(item.get("confidence")),
            "_agent": item.get("_agent"),
            "_agent_version": item.get("_agent_version"),
            "_extracted_at": item.get("_extracted_at"),
            "_entity_type": item.get("_entity_type"),
        }

        vitki_in = item.get("vitki")
        vitki: Dict[str, Any] = {}
        if isinstance(vitki_in, dict):
            for domain in VITKI_DOMAINS:
                m = self._measure(vitki_in.get(domain))
                if m is None:
                    continue
                level = m.pop("value", None)
                if level is None:
                    continue
                try:
                    m["level"] = max(VITEK_MIN, min(VITEK_MAX, int(round(float(level)))))
                except (TypeError, ValueError):
                    continue
                vitki[domain] = m
        if vitki:
            out["vitki"] = vitki

        lc = item.get("light_cone")
        if isinstance(lc, dict):
            quote = self._quote(lc.get("quote"))
            fields = {k: (str(lc.get(k)).strip() or None) if lc.get(k) else None
                      for k in ("horizon", "scale", "depth")}
            if quote and any(fields.values()):
                out["light_cone"] = {**fields, "confidence": self._conf(lc.get("confidence")),
                                     "quote": quote}

        eq_in = item.get("equalizer")
        eq: Dict[str, Any] = {}
        if isinstance(eq_in, dict):
            for axis in EQUALIZER_AXES:
                m = self._measure(eq_in.get(axis))
                if m is None:
                    continue
                try:
                    m["value"] = max(-1.0, min(1.0, float(m["value"])))
                except (TypeError, ValueError, KeyError):
                    continue
                eq[axis] = m
        if eq:
            out["equalizer"] = eq

        ps = item.get("phase_state")
        if isinstance(ps, dict):
            state = str(ps.get("state") or "").strip() or None
            quote = self._quote(ps.get("quote"))
            if state in PHASE_STATES and quote:
                out["phase_state"] = {
                    "state": state,
                    "confidence": self._conf(ps.get("confidence")),
                    "note": str(ps.get("note") or "").strip()[:300],
                    "quote": quote,
                }

        # профиль без единого измерения не несёт информации
        if not any(k in out for k in ("vitki", "light_cone", "equalizer", "phase_state")):
            return None
        return out

    async def extract(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Стандартный extract + grounding: измерение, чья цитата не находится
        в транскрипте, отбрасывается (защита от перефраза/выдумки)."""
        items = await super().extract(transcript, meeting_context)
        if not items:
            return []
        haystack = self._normalize_text(transcript)
        grounded: List[Dict[str, Any]] = []
        dropped = 0
        for profile in items:
            g, d = self._ground_profile(profile, haystack)
            dropped += d
            if g is not None:
                grounded.append(g)
        if dropped:
            logger.info("🧭 cogni_dimensions: %d ungrounded measure(s) dropped", dropped)
        return grounded

    # ── helpers ──

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Нижний регистр + схлопнутые пробелы + без кавычек-ёлочек/дефисов —
        чтобы дословная цитата находилась несмотря на мелкие расхождения."""
        t = (text or "").lower()
        t = t.replace("ё", "е")
        t = re.sub(r"[«»\"'“”‘’`—–-]", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    @classmethod
    def _quote_found(cls, quote: str, haystack: str) -> bool:
        q = cls._normalize_text(quote)
        if len(q) < _MIN_QUOTE_LEN:
            return False
        if q in haystack:
            return True
        # LLM мог чуть сместить границы реплики — принимаем, только если В
        # ТРАНСКРИПТЕ есть И начало, И конец цитаты (по одному префиксу
        # выдуманный хвост проходил бы «дословным»).
        if len(q) < 40:
            return False
        prefix, suffix = q[:60], q[-60:]
        return prefix in haystack and suffix in haystack

    @classmethod
    def _conf(cls, value: Any) -> float:
        try:
            c = float(value)
        except (TypeError, ValueError):
            return 0.0
        if c != c:  # NaN
            return 0.0
        return max(0.0, min(_SINGLE_MEETING_CONF_CAP, c))

    @classmethod
    def _quote(cls, value: Any) -> Optional[str]:
        q = str(value or "").strip()
        # длину меряем по нормализованному виду — той же мерке, что grounding;
        # иначе «„12“-символьная» цитата из одних кавычек/тире прошла бы здесь
        # и гарантированно отсеялась бы там (ложная диагностика ungrounded)
        if len(cls._normalize_text(q)) < _MIN_QUOTE_LEN:
            return None
        return q[:400]

    @classmethod
    def _measure(cls, raw: Any) -> Optional[Dict[str, Any]]:
        """{value|level, confidence, quote} → канон {value, confidence, quote}.
        Без валидной цитаты измерение не существует."""
        if not isinstance(raw, dict):
            return None
        quote = cls._quote(raw.get("quote"))
        if not quote:
            return None
        value = raw.get("value", raw.get("level"))
        if value is None:
            return None
        return {"value": value, "confidence": cls._conf(raw.get("confidence")),
                "quote": quote}

    @classmethod
    def _ground_profile(
        cls, profile: Dict[str, Any], haystack: str,
    ) -> tuple[Optional[Dict[str, Any]], int]:
        """Выбросить из профиля измерения с ненайденной цитатой.
        Возвращает (профиль или None, сколько измерений отброшено)."""
        dropped = 0
        for key in ("vitki", "equalizer"):
            group = profile.get(key)
            if not isinstance(group, dict):
                continue
            for sub in list(group.keys()):
                if not cls._quote_found(group[sub].get("quote", ""), haystack):
                    del group[sub]
                    dropped += 1
            if not group:
                profile.pop(key, None)
        for key in ("light_cone", "phase_state"):
            m = profile.get(key)
            if isinstance(m, dict) and not cls._quote_found(m.get("quote", ""), haystack):
                profile.pop(key, None)
                dropped += 1
        if not any(k in profile for k in ("vitki", "light_cone", "equalizer", "phase_state")):
            return None, dropped
        return profile, dropped
