# -*- coding: utf-8 -*-
"""
Regulation Extractor Agent - Извлечение регламентов, правил и практик из встреч.

Расширенные типы извлекаемых данных:
- Регламенты (regulations) - формальные правила и процедуры
- Требования (requirements) - обязательные условия и стандарты
- Политики (policies) - корпоративные правила и ограничения
- Процедуры (procedures) - установленные порядки действий
- **Кейсы (cases)** - примеры из практики, истории успеха/неудачи
- **Методологии (methodologies)** - подходы и методы работы
- **Лучшие практики (best_practices)** - проверенные способы решения задач
- **Руководства (guidelines)** - рекомендации и инструкции
- **Плейбуки (playbooks)** - сценарии действий в определённых ситуациях
- **Рабочие процессы (workflows)** - последовательности действий для автоматизации
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
class ExtractedRegulation:
    """
    Извлечённый регламент или практика.

    Поддерживаемые типы (regulation_type):
    - regulation: Формальные правила и процедуры
    - requirement: Обязательные условия и стандарты
    - policy: Корпоративные правила и ограничения
    - procedure: Установленные порядки действий
    - case: Примеры из практики, истории успеха/неудачи
    - methodology: Подходы и методы работы
    - best_practice: Проверенные способы решения задач
    - guideline: Рекомендации и инструкции
    - playbook: Сценарии действий в определённых ситуациях
    - workflow: Последовательности действий для автоматизации
    """
    regulation_id: str = field(default_factory=lambda: str(uuid4()))

    # Тип (расширенный список)
    regulation_type: str = "regulation"

    # Содержание
    title: str = ""
    description: str = ""

    # Область применения
    scope: str = ""  # Кому/чему применяется
    department: Optional[str] = None  # Отдел

    # Обязательность
    mandatory: bool = True  # Обязательный или рекомендательный
    enforcement: str = ""  # Как контролируется

    # Исключения
    exceptions: str = ""

    # Для кейсов
    outcome: str = ""  # success, failure, mixed
    lessons_learned: str = ""  # Уроки из кейса

    # Для workflows/playbooks
    steps: List[str] = field(default_factory=list)  # Шаги процесса
    triggers: List[str] = field(default_factory=list)  # Триггеры запуска
    automation_potential: str = ""  # Потенциал автоматизации

    # Источник
    source_meeting_id: Optional[str] = None
    source_quote: str = ""  # Цитата из встречи

    # Статус
    status: str = "active"  # active, draft, deprecated
    effective_date: Optional[str] = None

    # Связи
    related_regulations: List[str] = field(default_factory=list)
    related_projects: List[str] = field(default_factory=list)

    # Метаданные
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    confidence: float = 0.8

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "regulation_id": self.regulation_id,
            "regulation_type": self.regulation_type,
            "title": self.title,
            "description": self.description,
            "scope": self.scope,
            "department": self.department,
            "mandatory": self.mandatory,
            "enforcement": self.enforcement,
            "exceptions": self.exceptions,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "status": self.status,
            "effective_date": self.effective_date,
            "related_regulations": self.related_regulations,
            "related_projects": self.related_projects,
            "extracted_at": self.extracted_at,
            "confidence": self.confidence
        }

        # Добавляем поля для кейсов
        if self.regulation_type == "case":
            result["outcome"] = self.outcome
            result["lessons_learned"] = self.lessons_learned

        # Добавляем поля для workflows/playbooks
        if self.regulation_type in ("workflow", "playbook", "procedure"):
            result["steps"] = self.steps
            result["triggers"] = self.triggers
            result["automation_potential"] = self.automation_potential

        return result


class RegulationExtractorAgent:
    """
    Агент для извлечения регламентов и правил из встреч.

    Использование:
    ```python
    agent = RegulationExtractorAgent(llm_router=router)
    regulations = await agent.extract_regulations(meeting_data)
    ```
    """

    # Ключевые слова для определения потенциала регламентов и практик
    REGULATION_KEYWORDS = [
        # Регламенты и правила
        "регламент", "правило", "требование", "обязан",
        "должен", "необходимо", "запрещено", "политика",
        "стандарт", "процедура", "порядок", "инструкция",
        "обязательно", "нельзя", "всегда", "никогда",
        "согласовать", "утвердить", "compliance",
        # Кейсы и практики
        "кейс", "пример", "случай", "история", "опыт",
        "сработало", "не сработало", "урок", "вывод",
        # Методологии
        "методология", "подход", "метод", "фреймворк",
        "принцип", "практика", "способ",
        # Процессы
        "процесс", "workflow", "алгоритм", "последовательность",
        "шаг", "этап", "автоматизация", "сценарий"
    ]

    def __init__(self, llm_router=None, graph_builder=None):
        self.llm_router = llm_router
        self.graph_builder = graph_builder
        logger.info("✅ RegulationExtractorAgent initialized")

    async def extract_regulations(
        self,
        meeting_data: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> List[ExtractedRegulation]:
        """
        Извлекает регламенты из данных встречи.

        Args:
            meeting_data: Данные встречи (transcript_raw, decisions, tasks, etc.)
            context: Дополнительный контекст

        Returns:
            Список извлечённых регламентов
        """
        try:
            # Проверяем потенциал
            if not self._has_regulation_potential(meeting_data):
                logger.info("   ⏭️ Нет индикаторов регламентов - пропускаем")
                return []

            # Подготовка данных
            transcript = meeting_data.get("transcript_raw", "") or meeting_data.get("transcript", "")
            decisions = meeting_data.get("decisions", [])
            tasks = meeting_data.get("tasks", [])
            meeting_id = meeting_data.get("meeting_id", "")

            # Извлекаем через LLM
            regulations = await self._extract_with_llm(
                transcript=transcript,
                decisions=decisions,
                tasks=tasks,
                meeting_id=meeting_id,
                context=context
            )

            logger.info(f"   📋 Извлечено {len(regulations)} регламентов")

            # Сохраняем в граф если есть builder
            if self.graph_builder and regulations:
                await self._save_to_graph(regulations, meeting_id)

            return regulations

        except Exception as e:
            logger.error(f"   ❌ Ошибка извлечения регламентов: {e}", exc_info=True)
            return []

    def _has_regulation_potential(self, meeting_data: Dict[str, Any]) -> bool:
        """Проверяет наличие индикаторов регламентов."""
        transcript = (
            meeting_data.get("transcript_raw", "") or
            meeting_data.get("transcript", "")
        ).lower()

        decisions = meeting_data.get("decisions", [])

        # Для длинных транскриптов всегда проверяем через LLM
        if len(transcript) > 5000:
            return True

        # Есть решения с формальными требованиями
        if decisions and len(decisions) > 1:
            return True

        # Есть ключевые слова (снижен порог с 3 до 2)
        keyword_count = sum(1 for kw in self.REGULATION_KEYWORDS if kw in transcript)
        if keyword_count >= 2:
            return True

        return False

    async def _extract_with_llm(
        self,
        transcript: str,
        decisions: List[Dict],
        tasks: List[Dict],
        meeting_id: str,
        context: Optional[Dict] = None
    ) -> List[ExtractedRegulation]:
        """Извлекает регламенты через LLM."""

        if not self.llm_router:
            logger.warning("   ⚠️ LLM Router not available")
            return []

        # Форматируем данные
        decisions_text = "\n".join([
            f"- {d.get('decision', d.get('decision_summary', ''))}"
            for d in decisions[:15]
        ]) if decisions else "Нет решений"

        tasks_text = "\n".join([
            f"- {t.get('task', t.get('task_summary', ''))}"
            for t in tasks[:15]
        ]) if tasks else "Нет задач"

        prompt = f"""Проанализируй данные встречи и извлеки все регламенты, правила, практики и процессы.

ТРАНСКРИПТ:
{transcript[:6000]}

РЕШЕНИЯ:
{decisions_text}

ЗАДАЧИ:
{tasks_text}

Найди ВСЕ типы знаний:

1. **regulation** - Формальные правила и процедуры
2. **requirement** - Обязательные условия и стандарты
3. **policy** - Корпоративные правила и ограничения
4. **procedure** - Установленные порядки действий
5. **case** - Примеры из практики, истории успеха/неудачи (ВАЖНО!)
6. **methodology** - Подходы и методы работы
7. **best_practice** - Проверенные способы решения задач
8. **guideline** - Рекомендации и инструкции
9. **playbook** - Сценарии действий в определённых ситуациях
10. **workflow** - Последовательности действий (для автоматизации)

ВАЖНО:
- Извлекай ТОЛЬКО явно озвученные правила, практики и процессы
- Для кейсов указывай outcome (success/failure/mixed) и lessons_learned
- Для workflows/playbooks указывай steps и triggers
- НЕ придумывай то, чего нет в тексте
- Если ничего нет - верни пустой массив

Верни ТОЛЬКО валидный JSON массив (без markdown):
[
  {{
    "regulation_type": "regulation|requirement|policy|procedure|case|methodology|best_practice|guideline|playbook|workflow",
    "title": "Краткое название",
    "description": "Полное описание",
    "scope": "Область применения (кому/чему)",
    "department": "Отдел (если указан)",
    "mandatory": true/false,
    "enforcement": "Как контролируется выполнение",
    "exceptions": "Исключения (если есть)",
    "source_quote": "Точная цитата из встречи",
    "outcome": "success|failure|mixed (только для case)",
    "lessons_learned": "Уроки из кейса (только для case)",
    "steps": ["Шаг 1", "Шаг 2"] (только для workflow/playbook/procedure),
    "triggers": ["Триггер 1"] (только для workflow/playbook),
    "automation_potential": "high|medium|low|none (только для workflow)"
  }}
]

Если ничего НЕТ - верни пустой массив []"""

        system_prompt = """Ты - эксперт по анализу корпоративных знаний: регламентов, практик и процессов.

Твоя задача:
1. Находить формальные правила и требования
2. Выявлять кейсы и примеры из практики (особенно уроки из ошибок!)
3. Фиксировать методологии и лучшие практики
4. Описывать рабочие процессы для будущей автоматизации
5. Различать обязательные и рекомендательные нормы
6. НЕ извлекать если в встрече нет релевантных данных

Возвращай ТОЛЬКО валидный JSON массив."""

        try:
            # Трекинг использования LLM для регламентов
            async with UsageContext(agent_mode="templates", request_type="regulation_extraction"):
                response = await self.llm_router.generate(
                    prompt=prompt,
                    system_prompt=system_prompt
                )

                if not response:
                    return []

                # Парсим ответ
                regulations = self._parse_llm_response(response, meeting_id)
                return regulations

        except Exception as e:
            logger.error(f"   ❌ LLM extraction failed: {e}")
            return []

    def _parse_llm_response(
        self,
        response: str,
        meeting_id: str
    ) -> List[ExtractedRegulation]:
        """Парсит ответ LLM."""
        try:
            # Убираем markdown если есть
            response = response.strip()
            if response.startswith("```"):
                lines = response.split("\n")
                # Убираем первую и последнюю строки с ```
                response = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            # Ищем JSON массив
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if not json_match:
                return []

            regulations_raw = json.loads(json_match.group())

            if not isinstance(regulations_raw, list):
                return []

            # Преобразуем в объекты
            regulations = []
            for reg in regulations_raw:
                if isinstance(reg, dict) and reg.get("title") and reg.get("description"):
                    extracted = ExtractedRegulation(
                        regulation_type=reg.get("regulation_type", "regulation"),
                        title=reg.get("title", ""),
                        description=reg.get("description", ""),
                        scope=reg.get("scope", ""),
                        department=reg.get("department"),
                        mandatory=reg.get("mandatory", True),
                        enforcement=reg.get("enforcement", ""),
                        exceptions=reg.get("exceptions", ""),
                        source_meeting_id=meeting_id,
                        source_quote=reg.get("source_quote", "")
                    )

                    # Дополнительные поля для кейсов
                    if reg.get("regulation_type") == "case":
                        extracted.outcome = reg.get("outcome", "")
                        extracted.lessons_learned = reg.get("lessons_learned", "")

                    # Дополнительные поля для workflows/playbooks
                    if reg.get("regulation_type") in ("workflow", "playbook", "procedure"):
                        extracted.steps = reg.get("steps", [])
                        extracted.triggers = reg.get("triggers", [])
                        extracted.automation_potential = reg.get("automation_potential", "")

                    regulations.append(extracted)

            return regulations

        except json.JSONDecodeError as e:
            logger.error(f"   ❌ JSON parsing error: {e}")
            return []
        except Exception as e:
            logger.error(f"   ❌ Response processing error: {e}")
            return []

    async def _save_to_graph(
        self,
        regulations: List[ExtractedRegulation],
        meeting_id: str
    ):
        """Сохраняет регламенты в граф."""
        try:
            for reg in regulations:
                # Создаём узел регламента
                node_id = reg.regulation_id
                props = reg.to_dict()
                props["node_type"] = "Regulation"

                await self.graph_builder.create_node(
                    node_id=node_id,
                    label="Regulation",
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
                        properties={"extracted_at": reg.extracted_at}
                    )

                logger.info(f"   💾 Saved regulation: {reg.title}")

            # Сохраняем граф на диск
            await self.graph_builder.save()
            logger.info(f"   💾 Graph saved with {len(regulations)} new regulations")

        except Exception as e:
            logger.error(f"   ❌ Failed to save to graph: {e}")

    async def get_all_regulations(
        self,
        regulation_type: Optional[str] = None,
        department: Optional[str] = None,
        mandatory_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Получает все регламенты из графа.
        """
        if not self.graph_builder:
            return []

        try:
            # Формируем запрос

            # Получаем все узлы типа Regulation
            nodes = await self.graph_builder.find_nodes_by_label("Regulation", limit=1000)

            # Фильтруем по параметрам
            results = []
            for node in nodes:
                props = node if isinstance(node, dict) else {}

                # Фильтр по типу регламента
                if regulation_type and props.get("regulation_type") != regulation_type:
                    continue
                # Фильтр по отделу
                if department and props.get("department") != department:
                    continue
                # Фильтр по обязательности
                if mandatory_only and not props.get("mandatory", True):
                    continue

                results.append(props)

            return results

        except Exception as e:
            logger.error(f"❌ Failed to get regulations: {e}")
            return []

    async def search_regulations(
        self,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Поиск регламентов по запросу.
        """
        all_regs = await self.get_all_regulations()

        query_lower = query.lower()

        # Простой поиск по тексту
        results = []
        for reg in all_regs:
            title = reg.get("title", "").lower()
            description = reg.get("description", "").lower()
            scope = reg.get("scope", "").lower()

            if query_lower in title or query_lower in description or query_lower in scope:
                results.append(reg)

        return results[:limit]

