"""
Decision Event Extractor — Слой 1 BWE

LLM-пайплайн, который из встречи извлекает структурированные Decision Events.
Извлекаем тип ситуации, а не конкретику — как работает мудрец.

Оптимизации производительности:
- Батчевая генерация эмбеддингов (одним API-вызовом для всей встречи)
- Sliding-window чанкинг для длинных транскриптов
- Нормализация tension_type к допустимым значениям
- Модели из settings (не захардкожены)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Допустимые tension типы для нормализации
_VALID_TENSIONS = {
    "growth_vs_margin", "speed_vs_quality", "innovation_vs_stability",
    "cost_vs_growth", "build_vs_buy", "hire_vs_outsource",
    "centralize_vs_decentralize", "short_term_vs_long_term",
    "revenue_vs_retention", "other",
}

EXTRACTION_PROMPT = """Ты — аналитик бизнес-решений. Проанализируй транскрипт встречи и извлеки ВСЕ принятые решения.

ВАЖНО: описывай ТИП ситуации, не конкретные детали.
Например: "Конкурент снизил цену на 15%" → "ценовое давление со стороны конкурента".

ТРАНСКРИПТ:
{transcript}

Контекст:
- Виток: {vitek} (1=стартап, 2=рост, 3=зрелость, 4=экспансия)
- Рыночный контекст: {market_context}
- Участники встречи: {participants_hint}

Если в транскрипте есть метки "Speaker 1:", "Speaker 2:" — это диаризация
(система не знает имена, только разделила голоса). Атрибутируй решение
конкретному Speaker N, если ясно из контекста, кто его принял. Если
участники известны и можно сопоставить голос с человеком (по
само-представлению "я Максим" или обращению "Максим, ты что думаешь?") —
укажи resolved name в decision_maker_resolved. Иначе оставь null.

Верни JSON. Если решений нет — {{"decisions": []}}.

{{
  "decisions": [
    {{
      "situation": "нормализованный тип ситуации без конкретики",
      "tension_type": "одно из: growth_vs_margin|speed_vs_quality|innovation_vs_stability|cost_vs_growth|build_vs_buy|hire_vs_outsource|centralize_vs_decentralize|short_term_vs_long_term|revenue_vs_retention|other",
      "options_considered": ["вариант 1", "вариант 2"],
      "decision": "принятое решение (краткое)",
      "rationale": "почему именно это",
      "decision_confidence": "low|medium|high",
      "epistemic_type": "observed|inferred|claimed|predicted (факт наблюдали / вывод / кто-то утверждает / прогноз)",
      "predicted_outcome": "success|failure|uncertain (каким встреча ОЖИДАЛА исход этого решения)",
      "market_context": "рыночный контекст в момент решения",
      "decision_maker_speaker": "Speaker N — если в транскрипте есть метки и можно атрибутировать; иначе null",
      "decision_maker_resolved": "имя из 'Участники встречи' если ясно из контекста кто это; иначе null",
      "attribution_confidence": "low|medium|high — насколько уверены в decision_maker"
    }}
  ]
}}

Правила:
- Только явные решения, не обсуждения
- Нормализуй: убирай конкретные числа/имена, сохраняй суть
- tension_type строго из списка выше
- attribution_confidence='high' только если есть прямое подтверждение
  (само-представление, обращение по имени, явная роль). При сомнении — low."""

RETROSPECTIVE_PROMPT = """Ты — аналитик ретроспектив. Найди упоминания прошлых решений и их исходов.

ТРАНСКРИПТ:
{transcript}

Верни JSON. Если нет — {{"outcomes": []}}.

{{
  "outcomes": [
    {{
      "decision_summary": "краткое описание прошлого решения",
      "outcome_type": "success|failure|partial|unknown",
      "outcome_description": "что конкретно произошло",
      "delay_description": "когда было решение (если упомянуто)",
      "causal_factors": ["фактор 1", "фактор 2"],
      "impact_magnitude": 0.7
    }}
  ]
}}

Ищи: "помните мы решили...", "тогда договорились...", "то решение привело к...", "не сработало когда..."
impact_magnitude: 0.0=незначительно, 1.0=критически"""

# Промпт для chunk-уровневой экстракции (для длинных транскриптов)
CHUNK_EXTRACTION_PROMPT = """Извлеки решения из ФРАГМЕНТА транскрипта встречи.

ФРАГМЕНТ {chunk_num}/{total_chunks}:
{chunk}

Отвечай компактно:
{{"decisions": [
  {{"situation": "...", "tension_type": "...", "decision": "...", "decision_confidence": "medium"}}
]}}

Если решений нет — {{"decisions": []}}"""


class DecisionExtractor:
    """
    LLM-пайплайн извлечения Decision Events из транскриптов.

    Ключевые оптимизации:
    1. Батчевые эмбеддинги — один API-вызов для всех ситуаций
    2. Sliding window — длинные транскрипты режутся с перекрытием
    3. Нормализация tension_type к допустимому enum
    """

    CHUNK_SIZE = 6000      # символов на чанк
    CHUNK_OVERLAP = 500    # перекрытие между чанками
    MIN_TRANSCRIPT_LEN = 80

    def __init__(self, client: AsyncOpenAI, chat_model: str, embedding_model: str):
        self.client = client
        self.chat_model = chat_model
        self.embedding_model = embedding_model

    async def extract_decisions(
        self,
        transcript: str,
        company_vitek: int = 1,
        market_context: str = "",
        participants: list[str] | None = None,
        speaker_mapping: dict[str, str] | None = None,
    ) -> list[dict]:
        """
        Извлечь Decision Events из транскрипта.

        issue #112 T-1 продолжение + T-6: participants — список ожидаемых
        участников встречи (из calendar attendees или явно переданный).
        speaker_mapping — готовый mapping Speaker N → имя (если nil,
        автоматически вычисляется через speaker_resolver на основе
        self-introductions и address-response эвристик).

        LLM использует обе сигнала для resolve'а 'Speaker N → реальное имя'.

        Returns:
            list[dict] с базовыми ключами + decision_maker_speaker /
            decision_maker_resolved / attribution_confidence (новые,
            могут быть None для legacy-вызовов).
        """
        if not transcript or len(transcript.strip()) < self.MIN_TRANSCRIPT_LEN:
            return []

        # issue #112 T-6: auto-resolve speakers через self-introductions
        # если caller не передал явный mapping. Defensive: только если
        # в транскрипте есть Speaker N: метки (иначе нет смысла).
        if speaker_mapping is None and "Speaker" in transcript:
            try:
                from backend.core.transcripts import resolve_speakers_for_extraction
                speaker_mapping = resolve_speakers_for_extraction(
                    transcript, known_participants=participants)
            except Exception:
                speaker_mapping = {}
        speaker_mapping = speaker_mapping or {}

        if len(transcript) <= self.CHUNK_SIZE:
            return await self._extract_single(
                transcript, company_vitek, market_context, participants,
                speaker_mapping)

        # Длинный транскрипт: chunk + merge
        return await self._extract_chunked(
            transcript, company_vitek, market_context, participants,
            speaker_mapping)

    @staticmethod
    def _format_participants_hint(
        participants: list[str] | None,
        speaker_mapping: dict[str, str] | None = None,
    ) -> str:
        """Подготовить human-readable hint для промпта.

        Включает:
          - Список известных участников (Calendar attendees / явные)
          - Speaker mapping из self-introductions (T-6)

        Если ничего нет — fallback на 'не указаны'.
        """
        parts = []
        if participants:
            names = [p.strip() for p in participants if p and p.strip()]
            if names:
                parts.append("Участники: " + ", ".join(names))
        if speaker_mapping:
            mapping_str = ", ".join(
                f"{sp}={name}" for sp, name in speaker_mapping.items()
            )
            if mapping_str:
                parts.append("Распознанные спикеры: " + mapping_str)
        if not parts:
            return "не указаны (LLM атрибутирует только Speaker N)"
        return "; ".join(parts)

    async def _extract_single(
        self,
        transcript: str,
        company_vitek: int,
        market_context: str,
        participants: list[str] | None = None,
        speaker_mapping: dict[str, str] | None = None,
    ) -> list[dict]:
        """Извлечение за один LLM-вызов."""
        prompt = EXTRACTION_PROMPT.format(
            transcript=transcript,
            vitek=company_vitek,
            market_context=market_context or "не указан",
            participants_hint=self._format_participants_hint(
                participants, speaker_mapping),
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=2500,
            )
            data = json.loads(resp.choices[0].message.content)
            decisions = [self._normalize_decision(d) for d in data.get("decisions", [])]
            logger.info(f"[DecisionExtractor] Extracted {len(decisions)} decisions (single)")
            return decisions
        except Exception as e:
            logger.error(f"[DecisionExtractor] Single extraction failed: {e}")
            return []

    async def _extract_chunked(
        self,
        transcript: str,
        company_vitek: int,
        market_context: str,
        participants: list[str] | None = None,
        speaker_mapping: dict[str, str] | None = None,
    ) -> list[dict]:
        """Извлечение из длинного транскрипта по чанкам. participants/
        speaker_mapping в chunk-промпт пока не пробрасываются (chunk
        prompt компактный) — TODO когда станет важно для длинных
        meeting'ов с разной диаризацией внутри."""
        chunks = self._sliding_window(transcript, self.CHUNK_SIZE, self.CHUNK_OVERLAP)
        all_decisions: list[dict] = []
        seen_decisions: set[str] = set()

        import asyncio
        tasks = []
        for i, chunk in enumerate(chunks):
            prompt = CHUNK_EXTRACTION_PROMPT.format(
                chunk_num=i + 1,
                total_chunks=len(chunks),
                chunk=chunk,
            )
            tasks.append(self._llm_extract_compact(prompt))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                continue
            for dec in result:
                key = dec.get("decision", "")[:80].lower()
                if key and key not in seen_decisions:
                    seen_decisions.add(key)
                    dec["market_context"] = dec.get("market_context") or market_context
                    all_decisions.append(self._normalize_decision(dec))

        logger.info(f"[DecisionExtractor] Extracted {len(all_decisions)} decisions (chunked, {len(chunks)} chunks)")
        return all_decisions

    async def _llm_extract_compact(self, prompt: str) -> list[dict]:
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=800,
            )
            data = json.loads(resp.choices[0].message.content)
            return data.get("decisions", [])
        except Exception:
            return []

    async def extract_outcomes_from_retrospective(self, transcript: str) -> list[dict]:
        """
        Извлечь упоминания прошлых решений и их исходов из ретроспективы.
        """
        if not transcript or len(transcript.strip()) < self.MIN_TRANSCRIPT_LEN:
            return []

        prompt = RETROSPECTIVE_PROMPT.format(transcript=transcript[:8000])
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=2000,
            )
            data = json.loads(resp.choices[0].message.content)
            outcomes = data.get("outcomes", [])
            logger.info(f"[DecisionExtractor] Found {len(outcomes)} retrospective outcomes")
            return outcomes
        except Exception as e:
            logger.error(f"[DecisionExtractor] Retrospective extraction failed: {e}")
            return []

    async def batch_generate_situation_embeddings(
        self,
        decisions: list[dict],
    ) -> list[list[float]]:
        """
        Батчевая генерация эмбеддингов для всех решений одним API-вызовом.

        Это критическая оптимизация: вместо N отдельных вызовов — один.
        """
        if not decisions:
            return []

        texts = [
            self._situation_text(d.get("situation", ""), d.get("tension_type", ""), d.get("market_context", ""))
            for d in decisions
        ]

        try:
            resp = await self.client.embeddings.create(
                model=self.embedding_model,
                input=texts,
            )
            # Ответ отсортирован по индексу
            embeddings = [None] * len(texts)
            for item in resp.data:
                embeddings[item.index] = item.embedding
            return [e for e in embeddings if e is not None]
        except Exception as e:
            logger.error(f"[DecisionExtractor] Batch embedding failed: {e}")
            return [[] for _ in decisions]

    async def generate_outcome_embedding(
        self,
        outcome_type: str,
        outcome_description: str,
        causal_factors: Optional[list[str]] = None,
    ) -> list[float]:
        """Эмбеддинг для одного исхода."""
        factors = ", ".join(causal_factors or [])
        text = f"Исход: {outcome_type} | {outcome_description}"
        if factors:
            text += f" | Факторы: {factors}"
        try:
            resp = await self.client.embeddings.create(
                model=self.embedding_model,
                input=text[:2000],
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.debug(f"[DecisionExtractor] Outcome embedding failed: {e}")
            return []

    async def generate_query_embedding(self, situation: str, tension_type: str = "", context: str = "") -> list[float]:
        """Эмбеддинг для запроса к Wisdom Engine."""
        text = self._situation_text(situation, tension_type, context)
        try:
            resp = await self.client.embeddings.create(
                model=self.embedding_model,
                input=text[:2000],
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error(f"[DecisionExtractor] Query embedding failed: {e}")
            return []

    @staticmethod
    def _situation_text(situation: str, tension_type: str = "", context: str = "") -> str:
        """Конструктор текста для эмбеддинга ситуации."""
        text = f"Бизнес-ситуация: {situation}"
        if tension_type:
            text += f" | Тип напряжения: {tension_type}"
        if context:
            text += f" | Контекст: {context[:150]}"
        return text

    @staticmethod
    def _normalize_decision(dec: dict) -> dict:
        """Нормализовать decision: tension_type к допустимым значениям."""
        tension = (dec.get("tension_type") or "other").lower().strip()
        if tension not in _VALID_TENSIONS:
            tension = "other"
        dec["tension_type"] = tension
        # Конфиденция к допустимым значениям
        conf = (dec.get("decision_confidence") or "medium").lower()
        if conf not in ("low", "medium", "high"):
            conf = "medium"
        dec["decision_confidence"] = conf
        # Phase 2 D: эпистемика к допустимым значениям; default claimed
        # (консервативно) — если LLM не классифицировал, поведение прежнее.
        epi = (dec.get("epistemic_type") or "claimed").lower().strip()
        if epi not in ("observed", "inferred", "claimed", "predicted"):
            epi = "claimed"
        dec["epistemic_type"] = epi
        # D1 observer-effect: ожидаемый исход на момент решения; default
        # uncertain (нет ожидания → нет вклада в drift) → обратносовместимо.
        pred = (dec.get("predicted_outcome") or "uncertain").lower().strip()
        if pred not in ("success", "failure", "uncertain"):
            pred = "uncertain"
        dec["predicted_outcome"] = pred
        # issue #112 T-1: атрибуция к спикеру/имени. Нормализуем все
        # три новых поля; null оставляем как None (legacy LLM-ответы
        # без этих полей продолжают работать).
        speaker = dec.get("decision_maker_speaker")
        if isinstance(speaker, str) and speaker.strip().lower() in ("", "null", "none"):
            speaker = None
        dec["decision_maker_speaker"] = speaker
        resolved = dec.get("decision_maker_resolved")
        if isinstance(resolved, str) and resolved.strip().lower() in ("", "null", "none"):
            resolved = None
        dec["decision_maker_resolved"] = resolved
        # attribution_confidence: ENUM {low|medium|high}, default low
        # (консервативно: если LLM не уверен, мы не должны быть уверены).
        attr_conf = (dec.get("attribution_confidence") or "low").lower().strip()
        if attr_conf not in ("low", "medium", "high"):
            attr_conf = "low"
        # Если decision_maker не определён вообще — confidence обязательно low,
        # независимо от того, что LLM сказал (защита от self-overconfidence).
        if not speaker and not resolved:
            attr_conf = "low"
        dec["attribution_confidence"] = attr_conf
        return dec

    @staticmethod
    def _sliding_window(text: str, chunk_size: int, overlap: int) -> list[str]:
        """Разбить текст на перекрывающиеся чанки по символам."""
        chunks = []
        step = chunk_size - overlap
        for i in range(0, len(text), step):
            chunk = text[i:i + chunk_size]
            if len(chunk) >= 200:  # игнорируем слишком короткие хвосты
                chunks.append(chunk)
        return chunks
