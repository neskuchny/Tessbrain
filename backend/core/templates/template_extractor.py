# -*- coding: utf-8 -*-
"""
Template Extractor Agent - Извлечение шаблонов и чек-листов из встреч.

Типы извлекаемых данных:
- Чек-листы (checklist) - списки пунктов для выполнения
- Структуры (structure) - структуры документов
- Шаблоны документов (document) - шаблоны договоров, брифов, ТЗ
- Процессы (process) - пошаговые планы
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from backend.core.llm.usage_tracker import UsageContext

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTemplate:
    """Извлечённый шаблон."""
    template_id: str = field(default_factory=lambda: str(uuid4()))

    # Тип
    template_type: str = "checklist"  # checklist, structure, document, process

    # Содержание
    title: str = ""
    description: str = ""

    # Пункты/разделы
    items: List[str] = field(default_factory=list)

    # Когда использовать
    use_case: str = ""

    # Категория
    category: str = ""  # marketing, development, hr, sales, etc.

    # Источник
    source_meeting_id: Optional[str] = None

    # Метаданные
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    confidence: float = 0.8

    # Статистика использования
    usage_count: int = 0
    last_used_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_type": self.template_type,
            "title": self.title,
            "description": self.description,
            "items": self.items,
            "use_case": self.use_case,
            "category": self.category,
            "source_meeting_id": self.source_meeting_id,
            "extracted_at": self.extracted_at,
            "confidence": self.confidence,
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at
        }


class TemplateExtractorAgent:
    """
    Агент для извлечения шаблонов и чек-листов из встреч.

    Использование:
    ```python
    agent = TemplateExtractorAgent(llm_router=router)
    templates = await agent.extract_templates(meeting_id, meeting_data)
    ```
    """

    # Ключевые слова для определения потенциала шаблонов
    TEMPLATE_KEYWORDS = [
        "шаблон", "чек-лист", "структура", "план", "схема",
        "пункты", "разделы", "этапы", "список", "checklist",
        "template", "format", "формат", "образец", "алгоритм",
        "последовательность", "процесс", "инструкция"
    ]

    def __init__(self, llm_router=None, graph_builder=None, vector_indexer=None):
        self.llm_router = llm_router
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        logger.info("✅ TemplateExtractorAgent initialized")

    async def extract_templates(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> List[ExtractedTemplate]:
        """
        Извлекает шаблоны из встречи.

        Args:
            meeting_id: ID встречи
            meeting_data: Данные встречи
            context: Дополнительный контекст

        Returns:
            Список извлечённых шаблонов
        """
        logger.info(f"[*] Извлечение шаблонов: {meeting_id}")

        if not self._has_template_potential(meeting_data):
            logger.info("   [SKIP] Нет признаков шаблонов")
            return []

        try:
            templates = await self._extract_with_llm(meeting_id, meeting_data, context)

            if templates:
                logger.info(f"   [OK] Извлечено шаблонов: {len(templates)}")
                for template in templates:
                    logger.info(f"        - [{template.template_type}] {template.title}")

                # Сохраняем в граф
                if self.graph_builder:
                    await self._save_to_graph(templates, meeting_id)
            else:
                logger.info("   [OK] Шаблоны не обнаружены")

            return templates

        except Exception as e:
            logger.error(f"[ERROR] Ошибка извлечения шаблонов: {e}", exc_info=True)
            return []

    def _has_template_potential(self, meeting_data: Dict[str, Any]) -> bool:
        """Проверяет признаки шаблонов."""

        tasks = meeting_data.get("tasks", [])
        ideas = meeting_data.get("ideas", [])
        transcript = (
            meeting_data.get("transcript_raw", "") or
            meeting_data.get("transcript", "")
        ).lower()

        # Для длинных транскриптов всегда проверяем через LLM
        if len(transcript) > 5000:
            return True

        # Проверяем tasks
        for task in tasks:
            if isinstance(task, dict):
                text = str(task.get("task_summary", task.get("task", ""))).lower()
                if any(keyword in text for keyword in self.TEMPLATE_KEYWORDS):
                    return True

        # Если много задач (может быть чек-лист) - снижен порог
        if len(tasks) >= 3:
            return True

        # Проверяем ideas
        for idea in ideas:
            if isinstance(idea, dict):
                text = str(idea.get("idea_summary", idea.get("idea", ""))).lower()
                if any(keyword in text for keyword in self.TEMPLATE_KEYWORDS):
                    return True

        # Проверяем транскрипт - снижен порог с 2 до 1
        keyword_count = sum(1 for kw in self.TEMPLATE_KEYWORDS if kw in transcript)
        if keyword_count >= 1:
            return True

        return False

    async def _extract_with_llm(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> List[ExtractedTemplate]:
        """Извлекает шаблоны через LLM."""

        if not self.llm_router:
            logger.warning("   ⚠️ LLM Router not available")
            return []

        tasks = meeting_data.get("tasks", [])
        ideas = meeting_data.get("ideas", [])
        decisions = meeting_data.get("decisions", [])
        transcript = meeting_data.get("transcript_raw", "") or meeting_data.get("transcript", "")

        system_prompt = """Ты - эксперт по извлечению шаблонов и чек-листов.

ТВОЯ ЗАДАЧА: Найти СТРУКТУРИРОВАННЫЕ ШАБЛОНЫ - чек-листы, планы, структуры.

ЧТО ТАКОЕ ШАБЛОН:
✅ Checklist: Список пунктов для выполнения (запуск кампании, проверка проекта)
✅ Structure: Структура документа (разделы презентации, отчета)
✅ Document: Шаблон документа (договор, бриф, ТЗ)
✅ Process: Пошаговый план (онбординг, настройка, запуск)

ЧТО НЕ ЯВЛЯЕТСЯ ШАБЛОНОМ:
❌ Одиночная задача без структуры
❌ Общее описание без конкретных пунктов
❌ Идея без детализации

ТРЕБОВАНИЯ:
1. Должна быть четкая СТРУКТУРА с пунктами/этапами
2. Должно быть понятно КОГДА использовать
3. Минимум 3 пункта в шаблоне
4. НЕ придумывай то, чего нет в данных

Возвращай ТОЛЬКО валидный JSON."""

        # Форматируем данные
        tasks_text = self._format_items(tasks, "task_summary", "task")
        ideas_text = self._format_items(ideas[:10], "idea_summary", "idea")
        decisions_text = self._format_items(decisions[:5], "decision_summary", "decision")

        user_prompt = f"""Встреча: {meeting_id}

ТРАНСКРИПТ:
{transcript[:4000]}

ЗАДАЧИ:
{tasks_text}

ИДЕИ:
{ideas_text}

РЕШЕНИЯ:
{decisions_text}

Извлеки все шаблоны, чек-листы и структуры из этой встречи.

ФОРМАТ ОТВЕТА (JSON):
{{
  "templates": [
    {{
      "template_type": "checklist|structure|document|process",
      "title": "Название шаблона",
      "description": "Описание",
      "items": ["Пункт 1", "Пункт 2", "Пункт 3"],
      "use_case": "Когда использовать этот шаблон",
      "category": "marketing|development|hr|sales|operations|other"
    }}
  ]
}}

Если шаблонов НЕТ, верни: {{"templates": []}}"""

        try:
            # Трекинг использования LLM для шаблонов
            async with UsageContext(agent_mode="templates", request_type="template_extraction"):
                response = await self.llm_router.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt
                )

                if not response:
                    return []

                # Парсим JSON
                templates = self._parse_llm_response(response, meeting_id)
                return templates

        except Exception as e:
            logger.error(f"   [ERROR] LLM extraction failed: {e}")
            return []

    def _format_items(
        self,
        items: List[Dict],
        primary_key: str,
        fallback_key: str
    ) -> str:
        """Форматирует список элементов."""
        if not items:
            return "Нет данных"

        lines = []
        for i, item in enumerate(items, 1):
            if isinstance(item, dict):
                text = item.get(primary_key, item.get(fallback_key, ""))
                if text:
                    lines.append(f"{i}. {text}")

        return "\n".join(lines) if lines else "Нет данных"

    def _parse_llm_response(
        self,
        response: str,
        meeting_id: str
    ) -> List[ExtractedTemplate]:
        """Парсит ответ LLM."""
        try:
            # Ищем JSON
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                return []

            result = json.loads(json_match.group())
            templates_raw = result.get("templates", [])

            templates = []
            for template in templates_raw:
                if not isinstance(template, dict):
                    continue

                # Валидация
                title = template.get("title", "").strip()
                items = template.get("items", [])

                if not title or len(items) < 3:
                    continue

                templates.append(ExtractedTemplate(
                    template_type=template.get("template_type", "checklist"),
                    title=title,
                    description=template.get("description", ""),
                    items=items,
                    use_case=template.get("use_case", ""),
                    category=template.get("category", "other"),
                    source_meeting_id=meeting_id
                ))

            return templates

        except json.JSONDecodeError as e:
            logger.error(f"   [ERROR] JSON parsing error: {e}")
            return []
        except Exception as e:
            logger.error(f"   [ERROR] Response processing error: {e}")
            return []

    async def _save_to_graph(
        self,
        templates: List[ExtractedTemplate],
        meeting_id: str
    ):
        """Сохраняет шаблоны в граф."""
        try:
            for template in templates:
                # Создаём узел шаблона
                node_id = template.template_id
                props = template.to_dict()
                props["node_type"] = "Template"

                await self.graph_builder.create_node(
                    node_id=node_id,
                    label="Template",
                    properties=props
                )

                # Связываем со встречей. Узел встречи в графе — `meeting_<uuid>`,
                # сюда meeting_id приходит голым uuid → линковали на
                # несуществующий узел. Нормализуем с защитой от двойного префикса.
                if meeting_id:
                    meeting_node_id = (
                        meeting_id if str(meeting_id).startswith("meeting_")
                        else f"meeting_{meeting_id}"
                    )
                    await self.graph_builder.create_relationship(
                        from_id=node_id,
                        to_id=meeting_node_id,
                        rel_type="EXTRACTED_FROM",
                        properties={"extracted_at": template.extracted_at}
                    )

                # Индексируем для поиска
                if self.vector_indexer:
                    content = f"{template.title}\n{template.description}\n" + "\n".join(template.items)
                    # Сигнатура add_document(text, metadata, ...) — раньше
                    # звали с doc_id=/content=, что роняло сохранение шаблонов
                    # в вектор. id кладём в metadata.
                    await self.vector_indexer.add_document(
                        text=content,
                        metadata={
                            "type": "template",
                            "template_type": template.template_type,
                            "title": template.title,
                            "meeting_id": meeting_id,
                            "doc_id": template.template_id,
                        },
                    )

                logger.debug(f"   💾 Saved template: {template.title}")

            # Сохраняем граф на диск
            await self.graph_builder.save()
            logger.info(f"   💾 Graph saved with {len(templates)} new templates")

        except Exception as e:
            logger.error(f"   ❌ Failed to save to graph: {e}")

    async def get_all_templates(
        self,
        template_type: Optional[str] = None,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получает все шаблоны из графа.
        """
        if not self.graph_builder:
            return []

        try:
            # Получаем все узлы типа Template
            nodes = await self.graph_builder.find_nodes_by_label("Template", limit=1000)

            # Фильтруем по параметрам
            results = []
            for node in nodes:
                props = node if isinstance(node, dict) else {}

                # Фильтр по типу шаблона
                if template_type and props.get("template_type") != template_type:
                    continue
                # Фильтр по категории
                if category and props.get("category") != category:
                    continue

                results.append(props)

            return results

        except Exception as e:
            logger.error(f"❌ Failed to get templates: {e}")
            return []

    async def search_templates(
        self,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Поиск шаблонов по запросу.
        """
        # Сначала пробуем векторный поиск
        if self.vector_indexer:
            try:
                results = await self.vector_indexer.search(
                    query=query,
                    filter={"type": "template"},
                    limit=limit
                )
                if results:
                    return results
            except Exception as e:
                logger.warning(f"Vector search failed: {e}")

        # Fallback на текстовый поиск
        all_templates = await self.get_all_templates()

        query_lower = query.lower()

        results = []
        for template in all_templates:
            title = template.get("title", "").lower()
            description = template.get("description", "").lower()
            use_case = template.get("use_case", "").lower()

            if query_lower in title or query_lower in description or query_lower in use_case:
                results.append(template)

        return results[:limit]

    async def use_template(
        self,
        template_id: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Использует шаблон (создаёт на его основе).

        Args:
            template_id: ID шаблона
            params: Параметры для заполнения

        Returns:
            Результат применения шаблона
        """
        # Получаем шаблон
        templates = await self.get_all_templates()
        template = None
        for t in templates:
            if t.get("template_id") == template_id:
                template = t
                break

        if not template:
            return {"error": f"Template {template_id} not found"}

        # Обновляем статистику
        template["usage_count"] = template.get("usage_count", 0) + 1
        template["last_used_at"] = datetime.now().isoformat()

        # Возвращаем шаблон с параметрами
        return {
            "template": template,
            "params": params,
            "created_at": datetime.now().isoformat()
        }

