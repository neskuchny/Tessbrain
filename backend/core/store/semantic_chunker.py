# -*- coding: utf-8 -*-
"""
TESSENT BRAIN — Semantic Chunker (Step 4)
==========================================

Заменяет фиксированный чанкинг (1500 chars) на семантический.
Два уровня:
  Level 1: Topic Segmentation — разбивка по сменам тем обсуждения
  Level 2: Proposition Extraction — атомарные утверждения из каждой темы

Каждый уровень индексируется отдельно:
  - Topics: для overview-запросов ("о чём была встреча?")
  - Propositions: для точных запросов ("какой бюджет на Q1?")
  - Raw chunks: остаются для backward compatibility

Использование:
    chunker = SemanticChunker()
    result = await chunker.chunk_transcript(transcript, meeting_context)
    # result.topics — список тем с summaries
    # result.propositions — атомарные утверждения
    # result.raw_chunks — raw chunks (backward compat)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Proposition:
    """Атомарное утверждение, извлечённое из текста."""
    proposition_id: str = ""
    statement: str = ""                # "Бюджет проекта Alpha — 100M рублей на Q1"
    canonical_statement: str = ""      # Нормализованная форма для памяти / retrieval
    prop_type: str = "fact"            # fact | opinion | decision | question | commitment | emotion
    attribution: str = ""              # Кто сказал (имя спикера)
    confidence: float = 0.8
    entities: List[str] = field(default_factory=list)   # Упомянутые сущности
    temporal: str = ""                 # Временная привязка ("Q1 2025", "на прошлой неделе")
    topic_index: int = 0               # К какой теме относится
    meeting_id: str = ""               # Для прямого source attribution
    status: str = "active"             # active | superseded | conflicted (v1 пока active)
    extraction_method: str = "llm_semantic_chunker"
    source_start: int = -1             # Смещение внутри topic text
    source_end: int = -1               # Смещение внутри topic text
    source_text: str = ""              # Исходный фрагмент текста


@dataclass
class TopicSegment:
    """Тематический сегмент транскрипта."""
    topic_index: int = 0
    topic_name: str = ""               # "Обсуждение бюджета проекта Alpha"
    topic_summary: str = ""            # 2-3 предложения
    text: str = ""                     # Полный текст сегмента
    start_char: int = 0
    end_char: int = 0
    propositions: List[Proposition] = field(default_factory=list)
    speaker: str = ""                  # Основной спикер (если определён)


@dataclass
class SemanticChunkResult:
    """Результат семантического чанкинга."""
    topics: List[TopicSegment] = field(default_factory=list)
    propositions: List[Proposition] = field(default_factory=list)
    raw_chunks: List[Dict[str, Any]] = field(default_factory=list)
    meeting_id: str = ""
    total_propositions: int = 0
    total_topics: int = 0


class SemanticChunker:
    """
    Семантический чанкер транскриптов.

    Level 1: Topic Segmentation (LLM) — разбивка по темам
    Level 2: Proposition Extraction (LLM) — атомарные утверждения
    Level 0: Raw Chunks (алгоритмический) — backward compatibility
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client
        self._initialized = False

    async def _get_llm(self):
        """Lazy init LLM client."""
        if self.llm:
            return self.llm
        try:
            from backend.core.llm.gemini_client import get_gemini_client
            self.llm = get_gemini_client()
            return self.llm
        except Exception as e:
            logger.warning(f"LLM not available for semantic chunking: {e}")
            return None

    async def chunk_transcript(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None,
        meeting_id: str = "",
        extract_propositions: bool = True,
    ) -> SemanticChunkResult:
        """
        Основной метод: семантический чанкинг транскрипта.

        Args:
            transcript: Текст транскрипта
            meeting_context: Контекст встречи (title, date, participants)
            meeting_id: ID встречи
            extract_propositions: Извлекать ли propositions (Tier 2+)

        Returns:
            SemanticChunkResult с topics, propositions и raw_chunks
        """
        result = SemanticChunkResult(meeting_id=meeting_id)

        if not transcript or len(transcript) < 100:
            return result

        # Level 0: Raw chunks (всегда, backward compat)
        result.raw_chunks = self._split_raw_chunks(transcript)

        # Level 1: Topic segmentation
        llm = await self._get_llm()
        if llm:
            try:
                result.topics = await self._segment_by_topics(transcript, meeting_context, llm)
                result.total_topics = len(result.topics)
            except Exception as e:
                logger.warning(f"Topic segmentation failed, using fallback: {e}")
                result.topics = self._fallback_topic_segmentation(transcript)
                result.total_topics = len(result.topics)
        else:
            result.topics = self._fallback_topic_segmentation(transcript)
            result.total_topics = len(result.topics)

        # Level 2: Proposition extraction (per topic)
        if extract_propositions and llm and result.topics:
            try:
                all_propositions = []
                for topic in result.topics:
                    props = await self._extract_propositions(
                        topic.text, topic.topic_index, meeting_id, llm
                    )
                    topic.propositions = props
                    all_propositions.extend(props)

                result.propositions = all_propositions
                result.total_propositions = len(all_propositions)
            except Exception as e:
                logger.warning(f"Proposition extraction failed: {e}")

        logger.info(
            f"📝 Semantic chunking: {result.total_topics} topics, "
            f"{result.total_propositions} propositions, "
            f"{len(result.raw_chunks)} raw chunks"
        )

        return result

    def _split_raw_chunks(
        self, text: str, chunk_size: int = 1500, overlap: int = 200
    ) -> List[Dict[str, Any]]:
        """Level 0: фиксированные чанки (backward compat)."""
        chunks = []
        for start in range(0, len(text), chunk_size - overlap):
            chunk_text = text[start:start + chunk_size]
            if len(chunk_text) > 50:
                chunks.append({
                    "text": chunk_text,
                    "start": start,
                    "end": min(start + chunk_size, len(text)),
                })
        return chunks

    async def _segment_by_topics(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]],
        llm,
    ) -> List[TopicSegment]:
        """Level 1: разбивка по темам через LLM."""
        title = meeting_context.get("title", "") if meeting_context else ""

        # Ограничиваем длину для LLM (первые 15000 символов)
        text_for_llm = transcript[:15000]

        prompt = f"""Разбей транскрипт встречи на тематические блоки (2-8 блоков).
Для каждого блока укажи:
- topic_name: короткое название темы (3-7 слов)
- topic_summary: краткое содержание (1-2 предложения)
- start_marker: первые 5-10 слов этого блока в тексте (точная цитата)

Встреча: {title}

Транскрипт:
{text_for_llm}

Ответь СТРОГО в JSON формате:
[
  {{"topic_name": "...", "topic_summary": "...", "start_marker": "..."}},
  ...
]"""

        try:
            response = await llm.generate(prompt, temperature=0.2)
            topics_data = self._parse_json_from_response(response)

            if not topics_data or not isinstance(topics_data, list):
                return self._fallback_topic_segmentation(transcript)

            # Маппим маркеры на позиции в тексте
            segments = []
            for i, td in enumerate(topics_data):
                marker = td.get("start_marker", "")
                # Ищем позицию маркера в тексте
                pos = transcript.lower().find(marker.lower()[:30]) if marker else -1
                if pos == -1:
                    pos = int(len(transcript) * i / len(topics_data))

                segments.append(TopicSegment(
                    topic_index=i,
                    topic_name=td.get("topic_name", f"Тема {i+1}"),
                    topic_summary=td.get("topic_summary", ""),
                    start_char=pos,
                ))

            # Вычисляем end_char и текст
            for i in range(len(segments)):
                if i + 1 < len(segments):
                    segments[i].end_char = segments[i + 1].start_char
                else:
                    segments[i].end_char = len(transcript)
                segments[i].text = transcript[segments[i].start_char:segments[i].end_char]

            return segments

        except Exception as e:
            logger.warning(f"Topic segmentation LLM failed: {e}")
            return self._fallback_topic_segmentation(transcript)

    def _fallback_topic_segmentation(self, transcript: str) -> List[TopicSegment]:
        """Fallback: разбить на ~3000-символьные блоки по абзацам."""
        segments = []
        # Разбиваем по двойным переводам строки или длинным паузам
        parts = re.split(r'\n\s*\n|\. (?=[A-ZА-ЯЁ])', transcript)

        current_text = ""
        current_start = 0
        topic_idx = 0

        for part in parts:
            if len(current_text) + len(part) > 3000 and current_text:
                segments.append(TopicSegment(
                    topic_index=topic_idx,
                    topic_name=f"Блок {topic_idx + 1}",
                    topic_summary="",
                    text=current_text,
                    start_char=current_start,
                    end_char=current_start + len(current_text),
                ))
                topic_idx += 1
                current_start += len(current_text)
                current_text = part
            else:
                current_text += part

        if current_text:
            segments.append(TopicSegment(
                topic_index=topic_idx,
                topic_name=f"Блок {topic_idx + 1}",
                topic_summary="",
                text=current_text,
                start_char=current_start,
                end_char=current_start + len(current_text),
            ))

        return segments if segments else [TopicSegment(
            topic_index=0, topic_name="Встреча", text=transcript,
            start_char=0, end_char=len(transcript),
        )]

    async def _extract_propositions(
        self,
        topic_text: str,
        topic_index: int,
        meeting_id: str,
        llm,
    ) -> List[Proposition]:
        """Level 2: извлечение атомарных утверждений из темы."""
        if not topic_text or len(topic_text) < 50:
            return []

        # Ограничиваем для LLM
        text_for_llm = topic_text[:5000]

        prompt = f"""Извлеки все ключевые утверждения из текста. Каждое утверждение — это ОДИН факт, мнение, решение или вопрос.

Для каждого утверждения укажи:
- statement: само утверждение (1 предложение)
- type: тип (fact, opinion, decision, question, commitment)
- attribution: кто сказал (имя, или "" если неизвестно)
- confidence: уверенность 0.0-1.0
- entities: упомянутые сущности (имена, проекты, продукты)
- temporal: временная привязка (например "Q1 2025", "на этой неделе", "" если не указана)

Текст:
{text_for_llm}

Ответь СТРОГО в JSON:
[
  {{"statement": "...", "type": "fact", "attribution": "...", "confidence": 0.9, "entities": ["..."], "temporal": ""}},
  ...
]
Максимум 15 утверждений. Только самые важные."""

        try:
            response = await llm.generate(prompt, temperature=0.1)
            props_data = self._parse_json_from_response(response)

            if not props_data or not isinstance(props_data, list):
                return []

            propositions = []
            for i, pd in enumerate(props_data[:15]):
                statement = str(pd.get("statement", "")).strip()
                canonical_statement = re.sub(r"\s+", " ", statement).strip()
                source_start, source_end, source_text = self._estimate_source_span(
                    topic_text=topic_text,
                    statement=statement,
                )
                prop = Proposition(
                    proposition_id=f"prop_{meeting_id}_{topic_index}_{i}",
                    statement=statement,
                    canonical_statement=canonical_statement or statement,
                    prop_type=pd.get("type", "fact"),
                    attribution=pd.get("attribution", ""),
                    confidence=float(pd.get("confidence", 0.8)),
                    entities=pd.get("entities", []),
                    temporal=str(pd.get("temporal", "")).strip(),
                    topic_index=topic_index,
                    meeting_id=meeting_id,
                    status="active",
                    extraction_method="llm_semantic_chunker",
                    source_start=source_start,
                    source_end=source_end,
                    source_text=source_text,
                )
                if prop.statement:
                    propositions.append(prop)

            return propositions

        except Exception as e:
            logger.warning(f"Proposition extraction failed for topic {topic_index}: {e}")
            return []

    def _estimate_source_span(self, topic_text: str, statement: str) -> tuple[int, int, str]:
        """
        Грубая привязка proposition к исходному фрагменту текста.

        Нам не нужен идеальный sentence alignment на первом этапе, но нужен
        воспроизводимый source snippet для evidence и дальнейшей эволюции в BAU.
        """
        if not topic_text or not statement:
            return -1, -1, ""

        search_candidates = [
            statement.strip(),
            statement.strip()[:80],
            statement.strip()[:40],
        ]

        topic_lower = topic_text.lower()
        for candidate in search_candidates:
            probe = candidate.strip()
            if len(probe) < 10:
                continue
            pos = topic_lower.find(probe.lower())
            if pos != -1:
                end = min(len(topic_text), pos + max(len(probe), 220))
                return pos, end, topic_text[pos:end].strip()

        fallback_text = statement.strip()
        return -1, -1, fallback_text[:220]

    def _parse_json_from_response(self, response: str) -> Any:
        """Парсинг JSON из ответа LLM (с обработкой markdown)."""
        import json

        if not response:
            return None

        text = response.strip()

        # Убираем markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        # Ищем JSON array
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end + 1]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON from LLM response: {text[:200]}...")
            return None
