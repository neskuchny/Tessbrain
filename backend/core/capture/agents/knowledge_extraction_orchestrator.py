# -*- coding: utf-8 -*-
"""
Knowledge Extraction Orchestrator
=================================

Координирует извлечение структурированных знаний из встреч.

Типы извлекаемых знаний (14 категорий):
1. Meeting Summary - краткое описание встречи
2. Cases - кейсы, примеры, истории успеха/неудач
3. Methodologies - методологии, процессы, подходы
4. Templates - шаблоны, формы, структуры
5. Regulations - регламенты, правила, политики
6. Best Practices - лучшие практики
7. Guidelines - рекомендации, советы
8. Insights - инсайты, выводы, наблюдения
9. Standards - стандарты, нормативы
10. Achievements - достижения, результаты, метрики
11. Expert Knowledge - экспертные знания
12. Playbooks - playbook'и, сценарии действий
13. Antipatterns - антипаттерны, что НЕ делать
14. Workflows - рабочие процессы

Вдохновлено tess_old/module_16_knowledge_extraction.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.core.chunking.text_splitter import ChunkingService
from backend.core.llm.adaptive import AdaptiveConcurrency
from backend.core.llm.usage_tracker import UsageContext

from .achievement_extractor import AchievementExtractorAgent
from .antipattern_extractor import AntipatternExtractorAgent
from .expert_knowledge_extractor import ExpertKnowledgeExtractorAgent
from .insight_extractor import InsightExtractorAgent
from .standard_extractor import StandardExtractorAgent

logger = logging.getLogger(__name__)


class KnowledgeCategory(str, Enum):
    """Категории извлекаемых знаний."""
    SUMMARY = "summary"
    CASE = "case"
    METHODOLOGY = "methodology"
    TEMPLATE = "template"
    REGULATION = "regulation"
    BEST_PRACTICE = "best_practice"
    GUIDELINE = "guideline"
    INSIGHT = "insight"
    STANDARD = "standard"
    ACHIEVEMENT = "achievement"
    EXPERT_KNOWLEDGE = "expert_knowledge"
    PLAYBOOK = "playbook"
    ANTIPATTERN = "antipattern"
    WORKFLOW = "workflow"


@dataclass
class ExtractedKnowledge:
    """Извлечённое знание."""
    id: str
    category: KnowledgeCategory
    title: str
    content: str
    source_meeting_id: str

    # Метаданные
    confidence: float = 0.8
    importance: float = 0.5

    # Связи
    related_entities: List[str] = field(default_factory=list)
    related_topics: List[str] = field(default_factory=list)
    speakers: List[str] = field(default_factory=list)

    # Теги
    tags: List[str] = field(default_factory=list)

    # Временные метки
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # Дополнительные данные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category.value if isinstance(self.category, KnowledgeCategory) else self.category,
            "title": self.title,
            "content": self.content,
            "source_meeting_id": self.source_meeting_id,
            "confidence": self.confidence,
            "importance": self.importance,
            "related_entities": self.related_entities,
            "related_topics": self.related_topics,
            "speakers": self.speakers,
            "tags": self.tags,
            "extracted_at": self.extracted_at,
            "metadata": self.metadata
        }


@dataclass
class MeetingSummary:
    """Резюме встречи."""
    meeting_id: str
    summary: str
    main_topics: List[str]
    participants: List[str]
    content_tags: List[str]
    value_score: float  # 1-10
    has_actionable_content: bool
    stats: Dict[str, int]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "summary": self.summary,
            "main_topics": self.main_topics,
            "participants": self.participants,
            "content_tags": self.content_tags,
            "value_score": self.value_score,
            "has_actionable_content": self.has_actionable_content,
            "stats": self.stats,
            "timestamp": self.timestamp
        }


class KnowledgeExtractionOrchestrator:
    """
    Оркестратор извлечения знаний из встреч.

    Извлекает 14 категорий знаний через LLM.
    """

    def __init__(self, llm_router=None, graph_builder=None, vector_indexer=None):
        """
        Args:
            llm_router: LLM роутер для генерации
            graph_builder: GraphBuilder для сохранения знаний
            vector_indexer: VectorIndexer для индексации знаний
        """
        self.llm = llm_router
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        self._knowledge_counter = 0

        # Сколько категорий знаний извлекать параллельно. 13 LLM-вызовов
        # независимы, но у gemini-flash-lite есть rate-limit — поэтому
        # не «все разом», а ограниченная одновременность (по умолчанию 5).
        try:
            self._extract_concurrency = max(1, int(os.getenv("KNOWLEDGE_EXTRACT_CONCURRENCY", "5")))
        except (TypeError, ValueError):
            self._extract_concurrency = 5

        # Специализированные агенты
        if llm_router:
            self.achievement_extractor = AchievementExtractorAgent(llm_router)
            self.expert_extractor = ExpertKnowledgeExtractorAgent(llm_router)
            self.antipattern_extractor = AntipatternExtractorAgent(llm_router)
            self.insight_extractor = InsightExtractorAgent(llm_router)
            self.standard_extractor = StandardExtractorAgent(llm_router)
        else:
            self.achievement_extractor = None
            self.expert_extractor = None
            self.antipattern_extractor = None
            self.insight_extractor = None
            self.standard_extractor = None

        logger.info(f"✅ KnowledgeExtractionOrchestrator initialized (graph={graph_builder is not None}, vector={vector_indexer is not None})")

    async def extract_knowledge(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        transcript: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Извлекает все типы знаний из встречи.

        Args:
            meeting_id: ID встречи
            meeting_data: Данные из CaptureOrchestrator (decisions, tasks, participants...)
            transcript: Транскрипт встречи (опционально)
            context: Дополнительный контекст

        Returns:
            {
                "meeting_id": str,
                "summary": MeetingSummary,
                "cases": List[ExtractedKnowledge],
                "methodologies": List[ExtractedKnowledge],
                ...
                "extraction_time": float,
                "total_extracted": int
            }
        """
        logger.info(f"📚 Извлечение знаний из встречи: {meeting_id}")
        start_time = datetime.now()

        results = {
            "meeting_id": meeting_id,
            "summary": None,
            "cases": [],
            "methodologies": [],
            "templates": [],
            "regulations": [],
            "best_practices": [],
            "guidelines": [],
            "insights": [],
            "standards": [],
            "achievements": [],
            "expert_knowledge": [],
            "playbooks": [],
            "antipatterns": [],
            "workflows": [],
            "extraction_time": 0.0,
            "total_extracted": 0,
            "timestamp": start_time.isoformat()
        }

        try:
            # 1. MEETING SUMMARY
            logger.info("   [1/14] Meeting Summary...")
            summary = await self._generate_meeting_summary(meeting_id, meeting_data, context)
            results["summary"] = summary.to_dict() if summary else None

            # Если нет LLM, используем rule-based extraction
            if not self.llm:
                logger.warning("⚠️ LLM not available, using rule-based extraction")
                return results

            # 2-14. Извлекаем остальные категории через LLM.
            # NB: TEMPLATE и REGULATION здесь НЕ извлекаем — для них есть
            # отдельные специализированные фазы (_run_regulation_extraction /
            # _run_template_extraction) со своими промптами и узлами
            # Regulation/Template. Дубль здесь только плодил Knowledge-узлы и
            # лишние LLM-вызовы.
            # Generic-категории сливаем в группы (один LLM-вызов на группу) —
            # внутри группы понятия пересекаются, и модель раз решает, в какой
            # бакет отнести факт (промпт явно запрещает дублирование). Это
            # сокращает число вызовов без потери качества.
            merge_groups = [
                [  # процессы / сценарии / последовательности + кейсы
                    (KnowledgeCategory.CASE, "cases", "кейсы, примеры, истории успеха или неудач"),
                    (KnowledgeCategory.METHODOLOGY, "methodologies", "методологии, процессы, подходы к решению задач"),
                    (KnowledgeCategory.PLAYBOOK, "playbooks", "playbook'и, пошаговые сценарии действий"),
                    (KnowledgeCategory.WORKFLOW, "workflows", "рабочие процессы, последовательности шагов"),
                ],
                [  # рекомендательные
                    (KnowledgeCategory.BEST_PRACTICE, "best_practices", "лучшие практики (что работает хорошо)"),
                    (KnowledgeCategory.GUIDELINE, "guidelines", "рекомендации, советы, указания"),
                ],
            ]
            # Спец-категории со своими агентами/промптами — отдельными вызовами
            # (НЕ сливаем: у каждой своя логика извлечения).
            solo_categories = [
                (KnowledgeCategory.INSIGHT, "insights", "инсайты, выводы, наблюдения"),
                (KnowledgeCategory.STANDARD, "standards", "стандарты, нормативы"),
                (KnowledgeCategory.ACHIEVEMENT, "achievements", "достижения, результаты, метрики"),
                (KnowledgeCategory.EXPERT_KNOWLEDGE, "expert_knowledge", "экспертные знания, профессиональные советы"),
                (KnowledgeCategory.ANTIPATTERN, "antipatterns", "антипаттерны, что НЕ нужно делать, ошибки"),
            ]

            # Подготавливаем текст для анализа. Если транскрипт уже идёт
            # кешируемым префиксом CONTEXT (set_context_for_caching на том же
            # llm), не дублируем его в теле — экономим ~1–1.5k токенов на КАЖДЫЙ
            # из 13 вызовов. Структурированные данные кладём всегда.
            _ctx_cached = bool(
                getattr(self.llm, "has_cached_context", None)
                and self.llm.has_cached_context()
            )
            analysis_text = self._prepare_analysis_text(
                meeting_data, transcript, include_transcript=not _ctx_cached
            )
            if _ctx_cached:
                logger.info("📦 Транскрипт идёт кешируемым префиксом — не дублируем в теле промптов")

            # Извлечение одной категории (один или несколько LLM-вызовов).
            async def _extract_one(category: KnowledgeCategory, description: str) -> List[ExtractedKnowledge]:
                # Используем специализированные агенты
                if category == KnowledgeCategory.ACHIEVEMENT and self.achievement_extractor:
                    achievements = await self.achievement_extractor.extract_achievements(meeting_data, transcript or "")
                    return [self._convert_to_knowledge(ach, category, meeting_id) for ach in achievements]

                elif category == KnowledgeCategory.EXPERT_KNOWLEDGE and self.expert_extractor:
                    experts = await self.expert_extractor.extract_expert_knowledge(meeting_data, transcript or "")
                    return [self._convert_to_knowledge(exp, category, meeting_id) for exp in experts]

                elif category == KnowledgeCategory.ANTIPATTERN and self.antipattern_extractor:
                    antis = await self.antipattern_extractor.extract_antipatterns(transcript or "", meeting_id)
                    return [self._convert_to_knowledge(anti, category, meeting_id) for anti in antis]

                elif category == KnowledgeCategory.INSIGHT and self.insight_extractor:
                    insights = await self.insight_extractor.extract_insights(transcript or "", meeting_id)
                    return [self._convert_to_knowledge(ins, category, meeting_id) for ins in insights]

                elif category == KnowledgeCategory.STANDARD and self.standard_extractor:
                    standards = await self.standard_extractor.extract_standards(transcript or "", meeting_id)
                    return [self._convert_to_knowledge(std, category, meeting_id) for std in standards]

                # Fallback на общий метод
                return await self._extract_category(
                    meeting_id=meeting_id,
                    category=category,
                    description=description,
                    text=analysis_text,
                    meeting_data=meeting_data
                )

            # ═══ Параллельное извлечение: группы + spec-агенты ═══
            # Concurrently, под АДАПТИВНЫМ лимитом (AIMD): при rate-limit
            # gemini-flash-lite сам сжимается и повторяет задачу с backoff, при
            # стабильности растёт. Система «не выпадает в ошибку» и не теряет
            # категории.
            _limiter = AdaptiveConcurrency(self._extract_concurrency)
            _done = 0
            _total = len(merge_groups) + len(solo_categories)

            async def _run_group(group):
                nonlocal _done
                try:
                    res = await _limiter.run(
                        lambda: self._extract_category_group(meeting_id, group, analysis_text)
                    )
                except Exception as e:
                    logger.error(f"❌ Error extracting group: {e}")
                    res = {rk: [] for (_c, rk, _d) in group}
                _done += 1
                _labels = "+".join(rk for (_c, rk, _d) in group)
                _n = sum(len(v) for v in res.values())
                logger.info(f"   [{_done}/{_total}] group({_labels}) ✓ ({_n})")
                return {rk: [k.to_dict() for k in res.get(rk, [])] for (_c, rk, _d) in group}

            async def _run_solo(category, result_key, description):
                nonlocal _done
                try:
                    extracted = await _limiter.run(lambda: _extract_one(category, description))
                except Exception as e:
                    logger.error(f"❌ Error extracting {category.value}: {e}")
                    extracted = []
                _done += 1
                logger.info(f"   [{_done}/{_total}] {category.value} ✓ ({len(extracted)})")
                return {result_key: [k.to_dict() for k in extracted]}

            _dicts = await asyncio.gather(
                *[_run_group(g) for g in merge_groups],
                *[_run_solo(c, rk, d) for (c, rk, d) in solo_categories],
            )
            for _d in _dicts:
                results.update(_d)

            if _limiter.rate_limit_hits:
                logger.info(
                    f"⚙️ Knowledge extraction: rate-limit срабатывал "
                    f"{_limiter.rate_limit_hits}×, минимальная одновременность "
                    f"{_limiter.min_seen} (старт {self._extract_concurrency})"
                )

            # Подсчёт статистики
            total = sum(len(results[k]) for k in [
                "cases", "methodologies", "templates", "regulations",
                "best_practices", "guidelines", "insights", "standards",
                "achievements", "expert_knowledge", "playbooks", "antipatterns", "workflows"
            ])

            results["total_extracted"] = total
            results["extraction_time"] = (datetime.now() - start_time).total_seconds()

            logger.info(f"✅ Извлечено {total} элементов знаний за {results['extraction_time']:.2f}s")

            # Сохраняем в граф
            if self.graph_builder:
                await self._save_to_graph(meeting_id, results, transcript)

            return results

        except Exception as e:
            logger.error(f"❌ Ошибка извлечения знаний: {e}")
            results["error"] = str(e)
            return results

    def _convert_to_knowledge(self, item: Any, category: KnowledgeCategory, meeting_id: str) -> ExtractedKnowledge:
        """Конвертирует результат специализированного агента в ExtractedKnowledge."""
        self._knowledge_counter += 1
        knowledge_id = f"knowledge_{category.value}_{self._knowledge_counter}_{meeting_id[:8]}"

        # Формируем контент в зависимости от типа объекта.
        # NB: не у всех агентов item имеет .description (напр. ExpertKnowledge
        # несёт .content) — раньше прямое item.description роняло ВЕСЬ extract
        # знаний в 0 items. Берём безопасно, ниже .content при наличии override.
        content = getattr(item, "description", "") or ""
        if hasattr(item, 'impact') and item.impact:
            content += f"\n\nImpact: {item.impact}"
        if hasattr(item, 'root_cause') and item.root_cause:
            content += f"\nRoot Cause: {item.root_cause}"
        if hasattr(item, 'consequences') and item.consequences:
            content += f"\n\nConsequences: {item.consequences}"
        if hasattr(item, 'solution') and item.solution:
            content += f"\nSolution: {item.solution}"
        if hasattr(item, 'content') and item.content: # Expert knowledge content field override
            content = item.content
        if hasattr(item, 'implications') and item.implications:
            content += f"\n\nImplications: {item.implications}"
        if hasattr(item, 'threshold') and item.threshold:
            content += f"\n\nThreshold: {item.threshold}"

        return ExtractedKnowledge(
            id=knowledge_id,
            category=category,
            title=item.title,
            content=content,
            source_meeting_id=meeting_id,
            confidence=item.confidence,
            metadata=item.to_dict()
        )

    async def _generate_meeting_summary(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        context: Optional[Dict[str, Any]]
    ) -> Optional[MeetingSummary]:
        """Генерирует резюме встречи."""
        try:
            # Извлекаем основные данные
            participants = meeting_data.get("participants", [])
            decisions = meeting_data.get("decisions", [])
            tasks = meeting_data.get("tasks", [])
            meeting_data.get("opinions", [])
            ideas = meeting_data.get("ideas", [])
            meeting_data.get("numbers", [])
            kpis = meeting_data.get("kpis", [])

            # Определяем основные темы
            main_topics = self._extract_main_topics(meeting_data)

            # Определяем типы контента
            content_tags = self._determine_content_tags(meeting_data)

            # Оцениваем ценность встречи
            value_score = self._calculate_value_score(meeting_data, content_tags)

            # Генерируем summary через LLM
            summary_text = await self._generate_summary_text(meeting_id, meeting_data, main_topics)

            # Проверяем наличие actionable content
            has_actionable = len(tasks) > 0 or len(decisions) > 0

            # Извлекаем имена участников
            participant_names = []
            for p in participants:
                if isinstance(p, dict):
                    name = p.get("name") or p.get("participant_name")
                    if name:
                        participant_names.append(name)

            return MeetingSummary(
                meeting_id=meeting_id,
                summary=summary_text,
                main_topics=main_topics,
                participants=participant_names,
                content_tags=content_tags,
                value_score=value_score,
                has_actionable_content=has_actionable,
                stats={
                    "decisions_count": len(decisions),
                    "tasks_count": len(tasks),
                    "participants_count": len(participants),
                    "ideas_count": len(ideas),
                    "kpis_count": len(kpis)
                }
            )

        except Exception as e:
            logger.error(f"❌ Ошибка генерации summary: {e}")
            return None

    def _extract_main_topics(self, meeting_data: Dict[str, Any]) -> List[str]:
        """Извлекает основные темы из данных встречи."""
        topics = set()

        # Из проектов
        for project in meeting_data.get("projects", []):
            if isinstance(project, dict):
                name = project.get("project") or project.get("project_name") or project.get("name")
                if name and name not in ["None", "Не указано", "Unknown"]:
                    topics.add(name)

        # Из организаций
        for org in meeting_data.get("organizations", []):
            if isinstance(org, dict):
                name = org.get("name") or org.get("org_name")
                if name:
                    topics.add(name)

        # Из продуктов
        for product in meeting_data.get("products", []):
            if isinstance(product, dict):
                name = product.get("product_name") or product.get("name")
                if name:
                    topics.add(name)

        # Если нет конкретных тем
        if not topics:
            if meeting_data.get("decisions"):
                topics.add("принятие решений")
            if meeting_data.get("tasks"):
                topics.add("планирование задач")
            if meeting_data.get("numbers") or meeting_data.get("kpis"):
                topics.add("обсуждение метрик")

        return list(topics)[:5]

    def _determine_content_tags(self, meeting_data: Dict[str, Any]) -> List[str]:
        """Определяет типы контента во встрече."""
        tags = set()

        # Проверяем наличие кейсов
        for opinion in meeting_data.get("opinions", []):
            if isinstance(opinion, dict):
                sentiment = str(opinion.get("sentiment", "")).lower()
                if sentiment in ["positive", "позитивный"]:
                    tags.add("case")
                elif sentiment in ["negative", "негативный"]:
                    tags.add("anti_pattern")

        # Проверяем методологии
        if len(meeting_data.get("ideas", [])) > 3:
            tags.add("methodology")

        # Проверяем регламенты
        for task in meeting_data.get("tasks", []):
            if isinstance(task, dict):
                summary = str(task.get("task_summary", "")).lower()
                if any(w in summary for w in ["регламент", "процесс", "правило", "инструкция"]):
                    tags.add("regulation")

        # Проверяем экспертные знания
        for participant in meeting_data.get("participants", []):
            if isinstance(participant, dict):
                role = str(participant.get("role", "")).lower()
                if any(w in role for w in ["эксперт", "специалист", "директор", "руководитель"]):
                    tags.add("expert_knowledge")
                    break

        # Проверяем достижения
        if meeting_data.get("numbers") or meeting_data.get("kpis"):
            tags.add("insight")
            tags.add("achievement")

        if not tags:
            tags.add("general")

        return list(tags)

    def _calculate_value_score(
        self,
        meeting_data: Dict[str, Any],
        content_tags: List[str]
    ) -> float:
        """Оценивает ценность встречи от 1 до 10."""
        score = 3.0

        # Actionable content
        if meeting_data.get("decisions") or meeting_data.get("tasks"):
            score += 2.0

        # Теги контента
        if "expert_knowledge" in content_tags:
            score += 2.0
        if "methodology" in content_tags:
            score += 2.0
        if "case" in content_tags:
            score += 1.0

        # Участники
        participants = meeting_data.get("participants", [])
        if isinstance(participants, list) and len(participants) > 2:
            score += 1.0

        # Числовые данные
        if meeting_data.get("numbers") or meeting_data.get("kpis"):
            score += 1.0

        return min(10.0, score)

    async def _generate_summary_text(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        main_topics: List[str]
    ) -> str:
        """Генерирует текст резюме через LLM."""
        if not self.llm:
            return self._generate_fallback_summary(meeting_id, meeting_data, main_topics)

        try:
            # Готовим контекст
            context_parts = []

            participants = meeting_data.get("participants", [])
            if participants:
                names = [p.get("name", "Unknown") for p in participants if isinstance(p, dict)][:5]
                context_parts.append(f"Участники: {', '.join(names)}")

            decisions = meeting_data.get("decisions", [])
            if decisions:
                summaries = [d.get("decision_summary", "") for d in decisions[:3] if isinstance(d, dict)]
                context_parts.append(f"Решения: {'; '.join(summaries)}")

            tasks = meeting_data.get("tasks", [])
            if tasks:
                summaries = [t.get("task_summary", "") for t in tasks[:3] if isinstance(t, dict)]
                context_parts.append(f"Задачи: {'; '.join(summaries)}")

            ideas = meeting_data.get("ideas", [])
            if ideas:
                summaries = [i.get("idea_summary", "") for i in ideas[:2] if isinstance(i, dict)]
                context_parts.append(f"Идеи: {'; '.join(summaries)}")

            context_text = "\n".join(context_parts)

            prompt = f"""Создай КРАТКОЕ (200-300 символов) описание встречи.

Встреча: {meeting_id}
Данные:
{context_text}

Основные темы: {', '.join(main_topics)}

ТРЕБОВАНИЯ:
1. Укажи ГЛАВНУЮ ТЕМУ встречи
2. Перечисли КЛЮЧЕВЫЕ РЕШЕНИЯ (если есть)
3. НЕ используй общие фразы ("обсуждались вопросы")
4. Будь КОНКРЕТЕН: называй проекты, людей, числа

Ответ (только текст summary, без форматирования):"""

            # Трекинг использования LLM
            async with UsageContext(agent_mode="capture", request_type="meeting_summary"):
                response = await self.llm.generate(prompt)
                summary = response.strip()

                if len(summary) > 300:
                    summary = summary[:297] + "..."

                return summary

        except Exception as e:
            logger.warning(f"⚠️ LLM summary failed: {e}")
            return self._generate_fallback_summary(meeting_id, meeting_data, main_topics)

    def _generate_fallback_summary(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        main_topics: List[str]
    ) -> str:
        """Fallback summary без LLM."""
        parts = []

        if main_topics:
            parts.append(f"Встреча по темам: {', '.join(main_topics[:2])}")

        decisions_count = len(meeting_data.get("decisions", []))
        tasks_count = len(meeting_data.get("tasks", []))

        if decisions_count > 0:
            parts.append(f"{decisions_count} решений")
        if tasks_count > 0:
            parts.append(f"{tasks_count} задач")

        return ". ".join(parts) + "." if parts else f"Встреча {meeting_id}."

    def _prepare_analysis_text(
        self,
        meeting_data: Dict[str, Any],
        transcript: Optional[str],
        include_transcript: bool = True,
    ) -> str:
        """Подготавливает текст для анализа.

        include_transcript=False: транскрипт НЕ кладём в тело (он уже идёт
        кешируемым префиксом CONTEXT у клиента) — избегаем двойной отправки.
        Структурированные данные (decisions/tasks/…) кладём всегда: их в
        CONTEXT нет.
        """
        parts = []

        # Добавляем транскрипт (только если он не идёт кешируемым префиксом)
        if transcript and include_transcript:
            parts.append(f"ТРАНСКРИПТ:\n{transcript[:5000]}")

        # Добавляем извлечённые данные
        for key in ["decisions", "tasks", "opinions", "ideas", "kpis"]:
            items = meeting_data.get(key, [])
            if items:
                items_text = []
                for item in items[:10]:
                    if isinstance(item, dict):
                        summary = item.get(f"{key[:-1]}_summary") or item.get("summary") or str(item)
                        items_text.append(f"- {summary}")

                if items_text:
                    parts.append(f"\n{key.upper()}:\n" + "\n".join(items_text))

        return "\n\n".join(parts)

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        """float() без падения: 'high'/'medium'/None → default."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _item_to_knowledge(
        self, item: Dict[str, Any], category: KnowledgeCategory, meeting_id: str
    ) -> ExtractedKnowledge:
        """Конвертирует один LLM-item в ExtractedKnowledge (единая логика id)."""
        self._knowledge_counter += 1
        knowledge_id = f"knowledge_{category.value}_{self._knowledge_counter}_{meeting_id[:8]}"
        return ExtractedKnowledge(
            id=knowledge_id,
            category=category,
            title=item.get("title", "Без названия"),
            content=item.get("content", ""),
            source_meeting_id=meeting_id,
            confidence=self._safe_float(item.get("confidence"), 0.8),
            importance=self._safe_float(item.get("importance"), 0.5),
            related_entities=item.get("related_entities", []) or [],
            tags=item.get("tags", []) or [],
            speakers=[item.get("speaker")] if item.get("speaker") else [],
        )

    async def _extract_category_group(
        self,
        meeting_id: str,
        group: List[tuple],
        text: str,
    ) -> Dict[str, List[ExtractedKnowledge]]:
        """Извлекает НЕСКОЛЬКО близких категорий ОДНИМ LLM-вызовом.

        group: список (category, result_key, description). Промпт явно требует
        не дублировать факт между категориями (минимизируем путаницу LLM).
        Возвращает {result_key: [ExtractedKnowledge, ...]}.
        """
        out: Dict[str, List[ExtractedKnowledge]] = {rk: [] for (_c, rk, _d) in group}
        if not self.llm:
            return out

        cats_block = "\n".join(
            f'- "{rk}": {desc}' for (_cat, rk, desc) in group
        )
        body = f"ТЕКСТ:\n{text[:4000]}\n\n" if text and text.strip() else ""
        prompt = f"""Проанализируй встречу и извлеки знания по НЕСКОЛЬКИМ категориям.

КАТЕГОРИИ (ключ → что извлекать):
{cats_block}

{body}ФОРМАТ ОТВЕТА (строго JSON-объект, по одному массиву на КАЖДЫЙ ключ):
{{
{chr(10).join(f'    "{rk}": [{{"title": "...", "content": "...", "confidence": 0.0-1.0, "importance": 0.0-1.0, "related_entities": [], "tags": [], "speaker": ""}}]' for (_c, rk, _d) in group)}
}}

ПРАВИЛА:
1. Извлекай ТОЛЬКО явно присутствующее в тексте; ничего не выдумывай. Если в
   категории ничего нет — пустой массив.
2. НЕ дублируй один и тот же факт в разные категории — выбери НАИБОЛЕЕ подходящую.
3. content — по сказанному, related_entities и speaker заполняй только если они
   названы в тексте (speaker пустой, если непонятно кто).
4. confidence — уверенность (честно, ниже если выводится косвенно), importance —
   важность для организации (числа 0..1).

Ответь ТОЛЬКО JSON:"""

        try:
            async with UsageContext(agent_mode="capture", request_type="knowledge_extract_group"):
                response = await self.llm.generate(prompt)
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                return out
            data = json.loads(json_match.group())
            for (category, result_key, _desc) in group:
                items = data.get(result_key, []) or []
                if isinstance(items, list):
                    out[result_key] = [
                        self._item_to_knowledge(it, category, meeting_id)
                        for it in items if isinstance(it, dict)
                    ]
        except Exception as e:
            logger.error(f"❌ Error extracting group [{', '.join(rk for _c, rk, _d in group)}]: {e}")
        return out

    async def _extract_category(
        self,
        meeting_id: str,
        category: KnowledgeCategory,
        description: str,
        text: str,
        meeting_data: Dict[str, Any]
    ) -> List[ExtractedKnowledge]:
        """Извлекает знания определённой категории."""
        if not self.llm:
            return []

        try:
            prompt = f"""Проанализируй текст встречи и извлеки {description}.

ТЕКСТ:
{text[:4000]}

ФОРМАТ ОТВЕТА (JSON):
{{
    "items": [
        {{
            "title": "Краткий заголовок (до 100 символов)",
            "content": "Полное описание",
            "confidence": 0.0-1.0,
            "importance": 0.0-1.0,
            "related_entities": ["сущность1", "сущность2"],
            "tags": ["тег1", "тег2"],
            "speaker": "Кто сказал (если известно)"
        }}
    ]
}}

ПРАВИЛА:
1. Извлекай ТОЛЬКО явно присутствующие знания
2. Если таких знаний нет, верни пустой массив
3. Confidence отражает уверенность в правильности
4. Importance отражает важность для организации

Ответь ТОЛЬКО JSON:"""

            # Трекинг использования LLM
            async with UsageContext(agent_mode="capture", request_type=f"knowledge_extract_{category.value}"):
                response = await self.llm.generate(prompt)

                # Парсим ответ
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if not json_match:
                    return []

                data = json.loads(json_match.group())
                items = data.get("items", [])

                return [
                    self._item_to_knowledge(item, category, meeting_id)
                    for item in items if isinstance(item, dict)
                ]

        except Exception as e:
            logger.error(f"❌ Error extracting {category.value}: {e}")
            return []

    async def save_extracted(self, meeting_id: str, results: Dict[str, Any],
                             transcript: Optional[str] = None) -> None:
        """Персист УЖЕ извлечённых знаний — без единого LLM-вызова.

        Для досейки из дампов (scripts/seed_demo.py): extract-фазы стоят
        денег и уже отработали при создании дампа, а запись в граф и
        векторы — чистая. llm_router при этом не нужен (__init__ допускает
        None), нужны только graph_builder/vector_indexer.
        """
        await self._save_to_graph(meeting_id, results, transcript)

    async def _save_to_graph(self, meeting_id: str, results: Dict[str, Any], transcript: Optional[str] = None):
        """Сохраняет извлечённые знания в граф и индексирует в векторное хранилище."""
        saved_count = 0
        indexed_count = 0

        # Канонический id узла встречи в графе — `meeting_<uuid>` (см.
        # knowledge_sync.create_node label="Meeting"). Сюда meeting_id приходит
        # голым uuid → линковали chunk/knowledge на несуществующий узел и сыпали
        # «⚠️ Nodes not found for relationship». Нормализуем с защитой от
        # двойного префикса (в capture-пути id уже бывает с `meeting_`).
        meeting_node_id = (
            meeting_id if str(meeting_id).startswith("meeting_")
            else f"meeting_{meeting_id}"
        )

        try:
            # 1. Чанкинг и сохранение чанков (Mutual Indexing)
            chunks = []
            if transcript and self.graph_builder:
                try:
                    chunks = ChunkingService.split_text(transcript)
                    for chunk in chunks:
                        chunk_id = f"chunk_{meeting_id}_{chunk['index']}"

                        # Создаём узел Chunk
                        await self.graph_builder.create_node(
                            node_id=chunk_id,
                            label="Chunk",
                            properties={
                                "chunk_id": chunk_id,
                                "text": chunk["text"],
                                "index": chunk["index"],
                                "char_start": chunk["char_start"],
                                "char_end": chunk["char_end"],
                                "source_meeting_id": meeting_id
                            }
                        )

                        # Связываем с встречей
                        await self.graph_builder._create_relationship(
                            chunk_id, meeting_node_id,
                            "PART_OF",
                            {"index": chunk["index"]}
                        )

                    logger.info(f"📄 Created {len(chunks)} chunks for meeting {meeting_id}")
                except Exception as e:
                    logger.error(f"❌ Error creating chunks: {e}")

            # Сохраняем каждую категорию знаний
            for category_key in [
                "cases", "methodologies", "templates", "regulations",
                "best_practices", "guidelines", "insights", "standards",
                "achievements", "expert_knowledge", "playbooks", "antipatterns", "workflows"
            ]:
                items = results.get(category_key, [])
                for item in items:
                    # Создаём узел знания в графе
                    if self.graph_builder:
                        await self.graph_builder.create_node(
                            node_id=item["id"],
                            label="Knowledge",
                            properties={
                                "name": item["title"],
                                "category": item["category"],
                                "content": item["content"],
                                "confidence": item["confidence"],
                                "importance": item["importance"],
                                "tags": item["tags"],
                                "extracted_at": item["extracted_at"]
                            }
                        )

                        # Создаём связь с встречей
                        await self.graph_builder._create_relationship(
                            item["id"], meeting_node_id,
                            "EXTRACTED_FROM",
                            {"extracted_at": item["extracted_at"]}
                        )
                        saved_count += 1

                        # GROUNDING: Ищем связь с чанком
                        if chunks and item["content"]:
                            # Ищем цитату в чанках (берем первые 200 символов контента как "цитату")
                            # Это эвристика. В идеале агент должен возвращать точную цитату.
                            quote_to_search = item["content"][:200]
                            chunk_idx = ChunkingService.find_chunk_for_quote(quote_to_search, chunks)

                            if chunk_idx is not None:
                                chunk_id = f"chunk_{meeting_id}_{chunk_idx}"
                                await self.graph_builder._create_relationship(
                                    item["id"], chunk_id,
                                    "MENTIONED_IN",
                                    {"confidence": 0.9} # Высокая уверенность, т.к. нашли текст
                                )
                            else:
                                # Попробуем найти по названию
                                chunk_idx = ChunkingService.find_chunk_for_quote(item["title"], chunks)
                                if chunk_idx is not None:
                                    chunk_id = f"chunk_{meeting_id}_{chunk_idx}"
                                    await self.graph_builder._create_relationship(
                                        item["id"], chunk_id,
                                        "MENTIONED_IN",
                                        {"confidence": 0.7}
                                    )

                    # Индексируем в векторное хранилище
                    if self.vector_indexer:
                        try:
                            # Создаём текст для индексации
                            index_text = f"{item['title']}\n\n{item['content']}"
                            if item.get("tags"):
                                index_text += f"\n\nТеги: {', '.join(item['tags'])}"

                            await self.vector_indexer.add_document(
                                text=index_text,
                                metadata={
                                    "id": item["id"],
                                    "node_id": item["id"],
                                    "label": "Knowledge",
                                    "category": item["category"],
                                    "title": item["title"],
                                    "meeting_id": meeting_id,
                                    "confidence": item["confidence"],
                                    "importance": item["importance"],
                                    "tags": item.get("tags", [])
                                }
                            )
                            indexed_count += 1
                        except Exception as e:
                            logger.warning(f"⚠️ Vector indexing failed for {item['id']}: {e}")

            logger.info(f"✅ Знания сохранены: {saved_count} в граф, {indexed_count} в вектор для встречи {meeting_id}")

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения знаний: {e}")

