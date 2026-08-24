# -*- coding: utf-8 -*-
"""
Enhanced TZ Generator — улучшенная генерация технических заданий.

Объединяет лучшее из 3 систем:
- DataDrivenTaskSystem: граф-поиск, 6-стадийный пайплайн, анализ полноты
- SIMA TZ Generator: прямая загрузка встреч/документов, insights
- Task Spec Orchestrator: execution framework

Работает как стратегия "tz" в Brain чате.

Стадии:
1. UNDERSTAND  — LLM анализирует что именно нужно (тип ТЗ, данные, сущности)
2. COLLECT     — Enhanced Search + Graph + Documents + Meetings (параллельно)
3. ANALYZE     — анализ полноты, выявление пробелов
4. STRUCTURE   — структурирование: факты, мнения, цифры, допущения
5. GENERATE    — генерация ТЗ через LLM с type-specific промптом
6. VALIDATE    — проверка: есть ли пробелы, рекомендации
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)

logger = logging.getLogger("enhanced_tz_generator")


@dataclass
class TZRequest:
    """Входные данные для генерации ТЗ."""
    description: str                          # Описание задачи
    user_id: str = ""
    meeting_ids: List[str] = field(default_factory=list)    # Конкретные встречи
    document_ids: List[str] = field(default_factory=list)   # Конкретные документы
    project_filter: List[str] = field(default_factory=list) # Фильтр по проектам
    extra_context: str = ""                   # Дополнительный контекст (из сессии)
    output_format: str = "full"               # full | compact | executive


@dataclass
class TZResult:
    """Результат генерации ТЗ."""
    markdown: str = ""
    task_type: str = ""
    completeness_score: float = 0.0
    data_sources_used: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    stages_log: List[str] = field(default_factory=list)
    processing_time_ms: int = 0


class EnhancedTZGenerator:
    """
    Улучшенный генератор ТЗ для Brain чата.

    Использует:
    - Enhanced Search для поиска по базе знаний
    - Graph Builder для прямого доступа к графу
    - LLM Router для генерации
    - Document Indexer для загрузки документов
    """

    # Типы задач и их специфические данные
    TASK_TYPES = {
        "api": {"required": ["endpoints", "data_models", "auth"], "label": "API/Интеграция"},
        "landing": {"required": ["product_info", "features", "audience"], "label": "Лендинг/Маркетинг"},
        "finmodel": {"required": ["metrics", "costs", "revenue", "timeline"], "label": "Финансовая модель"},
        "feature": {"required": ["requirements", "user_flow", "acceptance_criteria"], "label": "Функциональность"},
        "report": {"required": ["metrics", "data_sources", "audience"], "label": "Отчёт/Аналитика"},
        "automation": {"required": ["process", "triggers", "integrations"], "label": "Автоматизация"},
        "architecture": {"required": ["components", "data_flow", "tech_stack"], "label": "Архитектура"},
        "general": {"required": ["requirements", "scope", "acceptance_criteria"], "label": "Общее ТЗ"},
    }

    def __init__(
        self,
        llm=None,
        enhanced_search=None,
        graph_builder=None,
        template_resolver=None,
    ):
        self.llm = llm
        self.enhanced_search = enhanced_search
        self.graph_builder = graph_builder
        # W25: Optional async resolver `(task_type) -> Optional[dict]`.
        # При наличии — Generate stage инжектит per-tenant template prompt
        # (см. core/tz/template_prompt.py). Default None → старое поведение
        # (только in-memory TASK_TYPES seeds).
        self.template_resolver = template_resolver

    async def generate(self, request: TZRequest) -> TZResult:
        """
        Основной метод генерации ТЗ.

        Args:
            request: TZRequest с описанием задачи и фильтрами

        Returns:
            TZResult с markdown-ТЗ и метаданными
        """
        start = time.time()
        stages = []

        # ═══ СТАДИЯ 1: UNDERSTAND ═══
        stages.append("1. Анализ задачи...")
        understanding = await self._stage_understand(request.description, request.extra_context)
        task_type = understanding.get("task_type", "general")
        stages.append(f"   Тип: {self.TASK_TYPES.get(task_type, {}).get('label', task_type)}")
        stages.append(f"   Подзапросы: {len(understanding.get('search_queries', []))}")

        # ═══ СТАДИЯ 2: COLLECT ═══
        stages.append("2. Сбор данных...")
        collected = await self._stage_collect(
            understanding, request
        )
        stages.append(f"   Источников: {collected.get('total_sources', 0)}")
        stages.append(f"   Из графа: {collected.get('graph_count', 0)}, из поиска: {collected.get('search_count', 0)}")
        if collected.get('meetings_count', 0):
            stages.append(f"   Встреч: {collected['meetings_count']}")
        if collected.get('documents_count', 0):
            stages.append(f"   Документов: {collected['documents_count']}")

        # ═══ СТАДИЯ 3: ANALYZE ═══
        stages.append("3. Анализ полноты данных...")
        analysis = await self._stage_analyze(understanding, collected)
        stages.append(f"   Полнота: {analysis.get('completeness_score', 0)}/10")
        if analysis.get('gaps'):
            stages.append(f"   Пробелы: {', '.join(analysis['gaps'][:3])}")

        # Доп. поиск если полнота < 5
        if analysis.get('completeness_score', 10) < 5 and analysis.get('additional_queries'):
            stages.append("   Дополнительный поиск...")
            extra_collected = await self._search_additional(analysis['additional_queries'], request.user_id)
            # Merge
            collected['search_results'] = collected.get('search_results', '') + '\n\n' + extra_collected
            collected['total_sources'] = collected.get('total_sources', 0) + 1

        # ═══ СТАДИЯ 4: STRUCTURE ═══
        stages.append("4. Структурирование данных...")
        structured = await self._stage_structure(understanding, collected)
        stages.append(f"   Факты: {len(structured.get('facts', []))}")
        stages.append(f"   Мнения: {len(structured.get('opinions', []))}")
        stages.append(f"   Цифры: {len(structured.get('numbers', []))}")
        stages.append(f"   Допущения: {len(structured.get('assumptions', []))}")

        # ═══ СТАДИЯ 5: GENERATE ═══
        stages.append("5. Генерация ТЗ...")
        markdown = await self._stage_generate(
            request.description, understanding, structured, analysis,
            output_format=request.output_format
        )
        stages.append(f"   Размер: {len(markdown)} символов")

        # ═══ СТАДИЯ 6: VALIDATE ═══
        stages.append("6. Валидация...")
        validation = await self._stage_validate(markdown, understanding, analysis)
        stages.append(f"   Оценка: {validation.get('quality_score', 0)}/10")

        processing_time = int((time.time() - start) * 1000)
        stages.append(f"Готово за {processing_time}ms")

        return TZResult(
            markdown=markdown,
            task_type=task_type,
            completeness_score=analysis.get('completeness_score', 0),
            data_sources_used=collected.get('data_sources_used', []),
            gaps=analysis.get('gaps', []),
            assumptions=structured.get('assumptions', []),
            stages_log=stages,
            processing_time_ms=processing_time
        )

    # ═══════════════════════════════════════════════════════
    #  Stage implementations
    # ═══════════════════════════════════════════════════════

    async def _stage_understand(self, description: str, extra_context: str = "") -> Dict:
        """Стадия 1: Понимание задачи."""

        prompt = f"""Проанализируй задачу для создания Технического Задания (ТЗ).

ЗАДАЧА: {description}

{f"КОНТЕКСТ РАЗГОВОРА: {extra_context[:500]}" if extra_context else ""}

Верни JSON:
{{
    "task_type": "api|landing|finmodel|feature|report|automation|architecture|general",
    "task_summary": "краткое описание задачи (1-2 предложения)",
    "target_entity": {{
        "type": "product|project|process|company",
        "name": "название сущности"
    }},
    "required_data": ["какие данные нужны для этого ТЗ"],
    "search_queries": ["поисковый запрос 1", "запрос 2", "запрос 3"],
    "key_questions": ["какие вопросы нужно ответить в ТЗ"]
}}

ПРАВИЛА:
- search_queries — конкретные поисковые запросы к базе знаний (макс. 5)
- required_data — типы данных: requirements, metrics, costs, tech_stack, user_flow, competitors, audience, risks
- key_questions — главные вопросы, на которые ТЗ должно ответить"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты аналитик. Отвечай ТОЛЬКО JSON.",
                temperature=0.2,
                max_tokens=1000
            )
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning(f"Stage 1 (understand) LLM failed: {e}")

        return {
            "task_type": "general",
            "task_summary": description[:200],
            "target_entity": {"type": "project", "name": ""},
            "required_data": ["requirements", "scope"],
            "search_queries": [description],
            "key_questions": []
        }

    async def _stage_collect(self, understanding: Dict, request: TZRequest) -> Dict:
        """Стадия 2: Сбор данных из всех источников."""

        collected = {
            "search_results": "",
            "graph_data": "",
            "meeting_data": "",
            "document_data": "",
            "total_sources": 0,
            "graph_count": 0,
            "search_count": 0,
            "meetings_count": 0,
            "documents_count": 0,
            "data_sources_used": []
        }

        # 2a-0. Реальные цифры компании (метрики план/факт + таблицы/CRM):
        # без этого ТЗ-финмодель строилась только на пересказах со встреч.
        try:
            from backend.core.ontology.numbers_context import numbers_block
            nb = numbers_block(request.user_id, request.description)
            if nb.get("text"):
                collected["search_results"] += f"\n{nb['text']}\n"
                collected["data_sources_used"].extend(nb.get("sources", []))
                collected["total_sources"] += len(nb.get("sources", []))
        except Exception:
            logger.debug("TZ numbers_block skipped", exc_info=True)

        # 2a. Enhanced Search по каждому подзапросу
        search_queries = understanding.get("search_queries", [request.description])
        for sq in search_queries[:5]:
            try:
                if self.enhanced_search:
                    result = await self.enhanced_search.search(
                        query=sq,
                        user_id=request.user_id,
                        search_depth="standard"
                    )
                    answer = getattr(result, 'answer', str(result))
                    sources = getattr(result, 'sources', [])

                    collected["search_results"] += f"\n### Поиск: {sq}\n{answer}\n"
                    collected["search_count"] += 1
                    collected["total_sources"] += len(sources)

                    for src in sources[:5]:
                        src_name = src.get("name", "") if isinstance(src, dict) else str(src)
                        if src_name and src_name not in collected["data_sources_used"]:
                            collected["data_sources_used"].append(src_name)
            except Exception as e:
                logger.warning(f"Search failed for '{sq[:50]}': {e}")

        # 2b. Graph: прямой поиск сущностей
        if self.graph_builder and hasattr(self.graph_builder, 'nx_graph') and self.graph_builder.nx_graph:
            try:
                target = understanding.get("target_entity", {})
                target_name = target.get("name", "")

                graph_facts = []
                nx = self.graph_builder.nx_graph

                # Ищем узлы по типам, важным для ТЗ
                important_types = ["Product", "Project", "Task", "Decision", "KPI", "Concept", "Regulation"]

                for _node_id, data in nx.nodes(data=True):
                    node_type = data.get("type", "")
                    node_name = data.get("name", "")

                    if node_type in important_types:
                        # Фильтр по target entity если указан
                        if target_name and target_name.lower() not in str(data).lower():
                            continue

                        summary = data.get("summary", "") or data.get("description", "")
                        if summary:
                            graph_facts.append(f"[{node_type}] {node_name}: {summary[:300]}")
                            collected["graph_count"] += 1

                            if collected["graph_count"] >= 30:
                                break

                if graph_facts:
                    collected["graph_data"] = "\n".join(graph_facts)
                    collected["total_sources"] += collected["graph_count"]
                    collected["data_sources_used"].append(f"Graph: {collected['graph_count']} nodes")

            except Exception as e:
                logger.warning(f"Graph search failed: {e}")

        # 2c. Прямая загрузка встреч (если указаны)
        if request.meeting_ids:
            try:
                from backend.core.sima.tessent_provider import fetch_meeting_transcript
                for mid in request.meeting_ids[:5]:
                    try:
                        data = await fetch_meeting_transcript(request.user_id, mid)
                        if data and data.get("content"):
                            name = data.get("name", mid)
                            content = data["content"][:5000]
                            collected["meeting_data"] += f"\n--- Встреча: {name} ---\n{content}\n"
                            collected["meetings_count"] += 1
                            collected["total_sources"] += 1
                            collected["data_sources_used"].append(f"Meeting: {name}")
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
            except ImportError:
                logger.warning("fetch_meeting_transcript not available")

        # 2d. Прямая загрузка документов (если указаны)
        if request.document_ids:
            try:
                from backend.core.documents import DocumentIndexer
                doc_indexer = DocumentIndexer(user_id=request.user_id)
                await doc_indexer.initialize()

                for did in request.document_ids[:5]:
                    try:
                        ctx = await doc_indexer.get_document_context(
                            document_id=did,
                            query=request.description,
                            max_chunks=5
                        )
                        if ctx:
                            collected["document_data"] += f"\n--- Документ ---\n{ctx}\n"
                            collected["documents_count"] += 1
                            collected["total_sources"] += 1
                            collected["data_sources_used"].append(f"Document: {did}")
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
            except ImportError:
                logger.warning("DocumentIndexer not available")

        return collected

    async def _stage_analyze(self, understanding: Dict, collected: Dict) -> Dict:
        """Стадия 3: Анализ полноты данных."""

        required = understanding.get("required_data", [])
        has_data = []

        if collected.get("search_results"):
            has_data.append("search_results")
        if collected.get("graph_data"):
            has_data.append("graph_data")
        if collected.get("meeting_data"):
            has_data.append("meeting_data")
        if collected.get("document_data"):
            has_data.append("document_data")

        # Быстрая оценка без LLM
        total = collected.get("total_sources", 0)
        if total >= 15:
            score = 8.0
        elif total >= 8:
            score = 6.5
        elif total >= 3:
            score = 5.0
        elif total >= 1:
            score = 3.0
        else:
            score = 1.0

        # LLM уточняет
        data_summary = f"""Найдено данных: {total} источников
- Из поиска: {collected.get('search_count', 0)} ответов
- Из графа: {collected.get('graph_count', 0)} узлов
- Встреч: {collected.get('meetings_count', 0)}
- Документов: {collected.get('documents_count', 0)}

Требуемые данные: {', '.join(required)}
Доступные типы: {', '.join(has_data)}"""

        prompt = f"""Оцени полноту данных для создания ТЗ.

ЗАДАЧА: {understanding.get('task_summary', '')}
ТИП: {understanding.get('task_type', 'general')}

{data_summary}

Верни JSON:
{{
    "completeness_score": 7.0,
    "gaps": ["чего не хватает"],
    "additional_queries": ["доп. поисковый запрос если нужен"],
    "recommendations": "краткая рекомендация"
}}

Оценка 1-10. Если >= 5, данных достаточно для ТЗ (пусть с допущениями)."""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Отвечай ТОЛЬКО JSON.",
                temperature=0.2,
                max_tokens=500
            )
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning(f"Stage 3 (analyze) failed: {e}")

        return {
            "completeness_score": score,
            "gaps": [],
            "additional_queries": [],
            "recommendations": ""
        }

    async def _search_additional(self, queries: List[str], user_id: str) -> str:
        """Дополнительный поиск по рекомендации анализатора."""
        results = []
        for q in queries[:2]:
            try:
                if self.enhanced_search:
                    result = await self.enhanced_search.search(
                        query=q, user_id=user_id, search_depth="standard"
                    )
                    answer = getattr(result, 'answer', str(result))
                    results.append(f"### Доп. поиск: {q}\n{answer}")
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
        return "\n\n".join(results)

    async def _stage_structure(self, understanding: Dict, collected: Dict) -> Dict:
        """Стадия 4: Структурирование данных (факты, мнения, цифры, допущения)."""

        # Собираем весь контекст
        all_data = ""
        if collected.get("search_results"):
            all_data += f"=== РЕЗУЛЬТАТЫ ПОИСКА ===\n{collected['search_results'][:6000]}\n\n"
        if collected.get("graph_data"):
            all_data += f"=== ДАННЫЕ ИЗ ГРАФА ЗНАНИЙ ===\n{collected['graph_data'][:4000]}\n\n"
        if collected.get("meeting_data"):
            all_data += f"=== ДАННЫЕ ИЗ ВСТРЕЧ ===\n{collected['meeting_data'][:4000]}\n\n"
        if collected.get("document_data"):
            all_data += f"=== ДАННЫЕ ИЗ ДОКУМЕНТОВ ===\n{collected['document_data'][:4000]}\n\n"

        if not all_data.strip():
            return {"facts": [], "opinions": [], "numbers": [], "assumptions": ["Данные не найдены — ТЗ будет общим"]}

        prompt = f"""Разбери найденные данные на категории для ТЗ.

ЗАДАЧА: {understanding.get('task_summary', '')}

ДАННЫЕ:
{all_data[:12000]}

Верни JSON:
{{
    "facts": ["факт 1 (источник)", "факт 2 (источник)", ...],
    "opinions": ["мнение 1 (кто сказал)", ...],
    "numbers": ["метрика/цифра 1 (источник)", ...],
    "assumptions": ["допущение 1 (на чём основано)", ...],
    "key_decisions": ["решение 1 (когда/где принято)", ...],
    "risks": ["риск 1", ...]
}}

ПРАВИЛА:
- facts: объективные утверждения, подтверждённые данными
- opinions: субъективные высказывания участников встреч
- numbers: конкретные цифры, метрики, сроки, бюджеты
- assumptions: то, что мы предполагаем за неимением данных
- key_decisions: уже принятые решения
- risks: выявленные или потенциальные риски
- Максимум 15 элементов в каждой категории"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты аналитик. Отвечай ТОЛЬКО JSON.",
                temperature=0.2,
                max_tokens=3000
            )
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning(f"Stage 4 (structure) failed: {e}")

        return {"facts": [], "opinions": [], "numbers": [], "assumptions": ["Автоматическая структуризация недоступна"]}

    async def _stage_generate(
        self,
        description: str,
        understanding: Dict,
        structured: Dict,
        analysis: Dict,
        output_format: str = "full"
    ) -> str:
        """Стадия 5: Генерация ТЗ."""

        task_type = understanding.get("task_type", "general")

        # Формируем данные
        facts_text = "\n".join([f"- {f}" for f in structured.get("facts", [])]) or "Не найдены"
        opinions_text = "\n".join([f"- {o}" for o in structured.get("opinions", [])]) or "Нет"
        numbers_text = "\n".join([f"- {n}" for n in structured.get("numbers", [])]) or "Нет"
        assumptions_text = "\n".join([f"- {a}" for a in structured.get("assumptions", [])]) or "Нет"
        decisions_text = "\n".join([f"- {d}" for d in structured.get("key_decisions", [])]) or "Нет"
        risks_text = "\n".join([f"- {r}" for r in structured.get("risks", [])]) or "Нет"

        completeness = analysis.get("completeness_score", 5)
        gaps_text = "\n".join([f"- {g}" for g in analysis.get("gaps", [])]) or "Нет"

        # Системный промпт по типу задачи
        system_instructions = self._get_type_specific_instructions(task_type)

        # W25: per-tenant template injection. Если caller передал
        # template_resolver, пытаемся подтянуть структуру (TZTemplate-dict)
        # для текущего task_type и вставить как доп. инструкцию для LLM.
        # Best-effort: при ошибке resolver'а просто продолжаем без template'а.
        template_prompt_block = ""
        if self.template_resolver is not None:
            try:
                template_dict = await self.template_resolver(task_type)
            except Exception as e:
                logger.debug("template_resolver failed: %s", e)
                template_dict = None
            if template_dict:
                try:
                    from backend.core.tz.template_prompt import (
                        render_template_prompt,
                    )
                    template_prompt_block = render_template_prompt(template_dict)
                except Exception as e:
                    logger.debug("render_template_prompt failed: %s", e)

        # Формат вывода
        if output_format == "compact":
            format_instruction = "Сделай КОМПАКТНОЕ ТЗ (2-3 страницы), только ключевые разделы."
        elif output_format == "executive":
            format_instruction = "Сделай EXECUTIVE SUMMARY (1 страница) с ключевыми решениями."
        else:
            format_instruction = "Сделай ПОЛНОЕ ДЕТАЛЬНОЕ ТЗ со всеми разделами."

        prompt = f"""Ты — старший бизнес-аналитик. Создай профессиональное Техническое Задание.

═══════════════════════════════════════════════════
ЗАДАЧА: {description}
ТИП: {self.TASK_TYPES.get(task_type, {}).get('label', task_type)}
═══════════════════════════════════════════════════

ДАННЫЕ КОМПАНИИ (используй для наполнения ТЗ):

ФАКТЫ (проверенные данные):
{facts_text}

МНЕНИЯ КОМАНДЫ (из встреч):
{opinions_text}

ЦИФРЫ И МЕТРИКИ:
{numbers_text}

ПРИНЯТЫЕ РЕШЕНИЯ:
{decisions_text}

ДОПУЩЕНИЯ (не подтверждено данными):
{assumptions_text}

ВЫЯВЛЕННЫЕ РИСКИ:
{risks_text}

ПОЛНОТА ДАННЫХ: {completeness}/10
ПРОБЕЛЫ В ДАННЫХ:
{gaps_text}

═══════════════════════════════════════════════════

{system_instructions}

{template_prompt_block}

{format_instruction}

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Используй ТОЛЬКО данные из раздела "ДАННЫЕ КОМПАНИИ"
2. Чётко помечай что основано на ФАКТАХ, а что на ДОПУЩЕНИЯХ
3. Каждый раздел должен ссылаться на источники данных
4. Если данных не хватает — так и пиши, НЕ ВЫДУМЫВАЙ
5. ТЗ должно быть ГОТОВО к передаче разработчику
6. Пиши на РУССКОМ языке

Верни ГОТОВОЕ ТЗ в формате Markdown."""

        try:
            result = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты старший бизнес-аналитик, создающий технические задания.",
                temperature=0.4,
                max_tokens=8000
            )
            return result or "Не удалось сгенерировать ТЗ"
        except Exception as e:
            logger.error(f"Stage 5 (generate) failed: {e}")
            return f"Ошибка генерации ТЗ: {str(e)[:100]}"

    async def _stage_validate(self, markdown: str, understanding: Dict, analysis: Dict) -> Dict:
        """Стадия 6: Быстрая валидация ТЗ."""

        # Проверяем наличие ключевых секций
        expected_sections = ["цел", "требован", "критери", "риск", "сроки"]
        found_sections = []
        missing_sections = []

        markdown_lower = markdown.lower()
        for section in expected_sections:
            if section in markdown_lower:
                found_sections.append(section)
            else:
                missing_sections.append(section)

        quality = min(10, max(1, len(found_sections) * 2))

        return {
            "quality_score": quality,
            "found_sections": found_sections,
            "missing_sections": missing_sections,
            "word_count": len(markdown.split())
        }

    def _get_type_specific_instructions(self, task_type: str) -> str:
        """Инструкции для генерации ТЗ по типу задачи."""

        instructions = {
            "api": """СТРУКТУРА ТЗ ДЛЯ API/ИНТЕГРАЦИИ:
1. Обзор — цель интеграции, системы, бизнес-ценность
2. Архитектура — схема взаимодействия, протоколы
3. Endpoints — метод, URL, параметры, ответы, коды ошибок
4. Модели данных — структуры запросов/ответов (JSON-примеры)
5. Аутентификация — механизм, токены, роли
6. Обработка ошибок — retry, fallback, алерты
7. Нефункциональные требования — нагрузка, latency, доступность
8. План тестирования
9. Риски и зависимости
10. Timeline и этапы""",

            "landing": """СТРУКТУРА ТЗ ДЛЯ ЛЕНДИНГА:
1. О продукте — позиционирование, УТП, аудитория
2. Hero Section — заголовок, подзаголовок, CTA (ГОТОВЫЕ ТЕКСТЫ!)
3. Блоки возможностей — каждая фича с описанием и иконкой
4. Блок "Как это работает" — пошаговый процесс
5. Social proof — кейсы, отзывы, цифры
6. Pricing — тарифы (если есть данные)
7. FAQ
8. Технические требования — адаптивность, скорость, SEO
9. Дизайн-референсы — стиль, цвета
10. Метрики успеха""",

            "finmodel": """СТРУКТУРА ТЗ ДЛЯ ФИНМОДЕЛИ:
1. Обзор — цель модели, период, ключевые метрики
2. Допущения — все вводные данные с источниками
3. Структура доходов — источники, прогноз, сезонность
4. Структура расходов — постоянные, переменные, единовременные
5. Unit-экономика — CAC, LTV, Payback Period
6. P&L прогноз — помесячный/квартальный
7. Cash Flow
8. Точка безубыточности
9. Сценарии — оптимистичный, базовый, пессимистичный
10. Риски и чувствительность""",

            "feature": """СТРУКТУРА ТЗ ДЛЯ ФУНКЦИОНАЛЬНОСТИ:
1. Обзор — цель, бизнес-ценность, метрика успеха
2. User Story — как пользователь взаимодействует
3. Функциональные требования — детально каждое
4. UI/UX — wireframes, user flow, состояния
5. Бизнес-логика — правила, валидации, edge cases
6. API (если есть) — endpoints
7. Критерии приёмки — чеклист
8. Нефункциональные требования
9. Зависимости
10. Timeline""",

            "architecture": """СТРУКТУРА ТЗ ДЛЯ АРХИТЕКТУРЫ:
1. Обзор — цели, ограничения, принципы
2. Компоненты системы — описание каждого
3. Data Flow — потоки данных между компонентами
4. Tech Stack — технологии с обоснованием выбора
5. Модель данных — схема БД, связи
6. Интеграции — внешние системы
7. Масштабирование — стратегия, bottlenecks
8. Безопасность — аутентификация, авторизация, шифрование
9. Мониторинг — метрики, алерты, логирование
10. Deployment — CI/CD, окружения"""
        }

        return instructions.get(task_type, """СТРУКТУРА ОБЩЕГО ТЗ:
1. Обзор — цель, бизнес-контекст, целевая аудитория
2. Scope — что входит и что НЕ входит
3. Функциональные требования — с приоритетами (Must/Should/Could)
4. Нефункциональные требования — производительность, безопасность
5. Ограничения и допущения
6. Критерии приёмки
7. Зависимости и риски
8. Timeline и этапы
9. Ресурсы
10. Приложения""")
