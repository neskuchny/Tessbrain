# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - AG2 Agentic Engine v2
=====================================

Улучшенный движок на базе AG2 (AutoGen 2.0) с современными паттернами:
- Context Variables для сохранения состояния между агентами
- Handoffs для явных переходов между агентами
- Guardrails для защиты от нежелательного контента
- Structured Outputs через Pydantic модели
- ReasoningAgent для сложных аналитических задач
- RAG интеграция для обогащения контекста

Архитектура:
- DefaultPattern для контролируемого потока
- OnCondition/OnContextCondition для условных переходов
- AgentTarget/RevertToUserTarget для маршрутизации
"""

import logging
import os
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

# AG2 Core imports
from autogen import ConversableAgent, UserProxyAgent, register_function
from pydantic import BaseModel, Field

# AG2 Modern Group Chat imports (v0.9+)
try:
    from autogen.agentchat import a_initiate_group_chat, initiate_group_chat
    from autogen.agentchat.group import (
        AgentTarget,
        ContextExpression,
        ContextVariables,
        ExpressionContextCondition,
        OnCondition,
        OnContextCondition,
        ReplyResult,
        RevertToUserTarget,
        StringContextCondition,
        StringLLMCondition,
        TerminateTarget,
    )
    from autogen.agentchat.group.guardrails import LLMGuardrail, RegexGuardrail
    from autogen.agentchat.group.patterns import AutoPattern, DefaultPattern
    AG2_MODERN = True
except ImportError:
    AG2_MODERN = False
    ContextVariables = dict

# ReasoningAgent (experimental)
try:
    from autogen.agents.experimental import ReasoningAgent, ThinkNode
    REASONING_AVAILABLE = True
except ImportError:
    REASONING_AVAILABLE = False
    ReasoningAgent = None

logger = logging.getLogger(__name__)


# ==========================================
# PYDANTIC MODELS - Structured Outputs
# ==========================================

class ResearchStep(BaseModel):
    """Шаг исследования"""
    step_number: int = Field(..., description="Номер шага")
    action: str = Field(..., description="Действие (search/analyze/synthesize)")
    query: str = Field(..., description="Запрос или описание действия")
    result: Optional[str] = Field(None, description="Результат выполнения")
    sources: List[str] = Field(default_factory=list, description="Источники данных")


class AnalysisResult(BaseModel):
    """Результат анализа"""
    summary: str = Field(..., description="Краткое резюме")
    key_findings: List[str] = Field(default_factory=list, description="Ключевые находки")
    entities_found: List[str] = Field(default_factory=list, description="Найденные сущности")
    recommendations: List[str] = Field(default_factory=list, description="Рекомендации")
    confidence: float = Field(0.0, ge=0, le=1, description="Уверенность в результате (0-1)")
    sources_used: List[str] = Field(default_factory=list, description="Использованные источники")


class AgentResponse(BaseModel):
    """Структурированный ответ агента"""
    agent_name: str = Field(..., description="Имя агента")
    response_type: str = Field(..., description="Тип ответа (answer/question/handoff)")
    content: str = Field(..., description="Содержание ответа")
    next_agent: Optional[str] = Field(None, description="Следующий агент (для handoff)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Метаданные")


class DeepAnalysisReport(BaseModel):
    """Полный отчет глубокого анализа"""
    query: str = Field(..., description="Исходный запрос")
    research_steps: List[ResearchStep] = Field(default_factory=list)
    analysis: Optional[AnalysisResult] = None
    final_answer: str = Field("", description="Финальный ответ")
    processing_time_ms: int = Field(0, description="Время обработки в мс")
    agents_involved: List[str] = Field(default_factory=list, description="Задействованные агенты")
    errors: List[str] = Field(default_factory=list, description="Ошибки при обработке")


# ==========================================
# CONTEXT MANAGEMENT
# ==========================================

def create_analysis_context(
    user_id: str | None = None,
    session_id: str | None = None,
    query: str = "",
) -> ContextVariables:
    """Создать контекст для анализа"""
    if AG2_MODERN:
        return ContextVariables(data={
            # Идентификация
            "user_id": user_id,
            "session_id": session_id,
            "query": query,

            # Состояние анализа
            "analysis_stage": "initial",  # initial -> research -> analysis -> synthesis -> complete
            "research_complete": False,
            "analysis_complete": False,
            "synthesis_complete": False,

            # Накопленные данные
            "search_results": [],
            "entities_found": [],
            "relationships_found": [],
            "key_decisions": [],
            "action_items": [],

            # Результаты агентов
            "planner_output": None,
            "researcher_output": None,
            "critic_output": None,
            "analyst_output": None,

            # Ошибки и метрики
            "errors": [],
            "iteration_count": 0,
            "max_iterations": 5,

            # Флаги управления
            "needs_more_research": False,
            "ready_for_synthesis": False,
            "should_terminate": False,
        })
    else:
        return {
            "user_id": user_id,
            "session_id": session_id,
            "query": query,
            "analysis_stage": "initial",
            "search_results": [],
            "entities_found": [],
            "errors": [],
        }


# ==========================================
# AG2 AGENTIC ENGINE V2
# ==========================================

class AG2AgenticEngineV2:
    """
    Улучшенный AG2 движок для глубокого анализа.

    Особенности:
    - Context Variables для сохранения состояния
    - Явные Handoffs между агентами
    - Guardrails для защиты
    - Structured Outputs через Pydantic
    - ReasoningAgent для сложных задач
    """

    def __init__(
        self,
        tools_instance,
        llm_config: Dict[str, Any],
        use_reasoning_agent: bool = False,
        use_guardrails: bool = True,
    ):
        """
        Args:
            tools_instance: Экземпляр TessentBrainTools
            llm_config: Конфигурация для LLM
            use_reasoning_agent: Использовать ReasoningAgent для сложных задач
            use_guardrails: Включить guardrails
        """
        self.tools = tools_instance
        self.llm_config = llm_config
        self.use_reasoning_agent = use_reasoning_agent and REASONING_AVAILABLE
        self.use_guardrails = use_guardrails and AG2_MODERN

        self.agents = {}
        self.guardrails = []

        # Инициализация
        self._init_agents()
        if self.use_guardrails:
            self._init_guardrails()

        logger.info(f"✅ AG2AgenticEngineV2 initialized (modern={AG2_MODERN}, reasoning={self.use_reasoning_agent}, guardrails={self.use_guardrails})")

    def _get_company_context(self) -> str:
        """Получить контекст компании для системных сообщений"""
        return """
ВАЖНО: Ты работаешь с ВНУТРЕННЕЙ базой знаний компании Tessbrain.
Это НЕ интернет-поиск. У тебя есть доступ к:
- Транскриптам встреч компании
- Задачам и решениям
- Информации о сотрудниках и проектах
- Графу знаний компании

КРИТИЧНЫЕ ПРАВИЛА (anti-hallucination):
1. НЕЛЬЗЯ добавлять факты, которых нет в результатах инструментов
2. Если данных недостаточно — так и скажи
3. Запрещено ссылаться на внешние события/новости если их нет в базе
4. Все утверждения должны быть подтверждены данными из поиска
"""

    def _init_agents(self):
        """Инициализация агентов"""
        company_context = self._get_company_context()

        # 1. User Proxy - инициатор и исполнитель инструментов
        self.agents["user_proxy"] = UserProxyAgent(
            name="User_Proxy",
            system_message="Human admin. Execute tools when requested.",
            code_execution_config=False,
            human_input_mode="NEVER",
            is_termination_msg=lambda x: x.get("content", "").strip().endswith("TERMINATE"),
        )

        # 2. Planner - стратегический планировщик
        self.agents["planner"] = ConversableAgent(
            name="Planner",
            system_message=f"""{company_context}

Ты — Старший Аналитик и Стратегический Планировщик.

ТВОЯ РОЛЬ:
1. Анализировать запрос пользователя
2. Составлять план исследования (какую информацию искать)
3. Определять последовательность действий
4. Координировать работу других агентов

ПРАВИЛА:
- ВСЕГДА начинай с search_knowledge() для общих вопросов
- Разбивай сложные запросы на подзадачи
- Не ищи информацию сам - давай указания Researcher
- Когда информации достаточно - передай задачу Analyst

ФОРМАТ ПЛАНА:
1. Цель: [что нужно узнать]
2. Шаги: [список поисковых запросов]
3. Критерии успеха: [когда считать задачу выполненной]
""",
            llm_config=self.llm_config,
        )

        # 3. Researcher - исследователь с инструментами
        self.agents["researcher"] = ConversableAgent(
            name="Researcher",
            system_message=f"""{company_context}

Ты — Эксперт по поиску в базе знаний Tessbrain.

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
1. search_knowledge(query) — семантический поиск по всей базе
2. get_project_status(project_name) — статус проекта
3. get_person_info(person_name) — информация о сотруднике
4. get_anomalies() — список аномалий и рисков
5. get_recent_meetings(days=7) — недавние встречи

ПРАВИЛА ПОИСКА:
- Выполняй поисковые запросы по плану от Planner
- Если мало результатов - переформулируй запрос
- Сообщай о найденных фактах кратко и структурированно
- НЕ выдумывай факты - только то, что найдено инструментами
- Указывай источники (meeting_id, document_id)

ФОРМАТ ОТЧЕТА:
📊 Найдено по запросу "[запрос]":
- [факт 1] (источник: meeting_xxx)
- [факт 2] (источник: task_yyy)
""",
            llm_config=self.llm_config,
        )

        # 4. Critic - критик и валидатор
        self.agents["critic"] = ConversableAgent(
            name="Critic",
            system_message=f"""{company_context}

Ты — Критик и Валидатор качества.

ТВОЯ РОЛЬ:
1. Проверять результаты поиска Researcher
2. Оценивать достаточность информации
3. Выявлять пробелы в данных
4. Проверять на галлюцинации (факты без источников)

КРИТЕРИИ ОЦЕНКИ:
- Достаточно ли данных для ответа на исходный вопрос?
- Есть ли противоречия в найденной информации?
- Все ли факты подтверждены источниками?
- Нужны ли дополнительные поиски?

ФОРМАТ ОЦЕНКИ:
✅ ДОСТАТОЧНО / ❌ НЕДОСТАТОЧНО

Если недостаточно:
- Чего не хватает: [список]
- Рекомендуемые поиски: [запросы]

Если достаточно:
- Готово к анализу: ДА
""",
            llm_config=self.llm_config,
        )

        # 5. Analyst - финальный аналитик
        self.agents["analyst"] = ConversableAgent(
            name="Analyst",
            system_message=f"""{company_context}

Ты — Бизнес-аналитик, создающий финальные отчеты.

ТВОЯ РОЛЬ:
1. Синтезировать все найденные данные
2. Структурировать информацию
3. Выделять ключевые выводы
4. Давать рекомендации

ФОРМАТ ОТВЕТА (Markdown):
## 📋 Резюме
[краткое резюме в 2-3 предложения]

## 🔍 Ключевые находки
- [находка 1]
- [находка 2]

## 👥 Участники/Проекты
- [список релевантных сущностей]

## ⚠️ Риски/Проблемы (если есть)
- [риск 1]

## 💡 Рекомендации
- [рекомендация 1]

## 📚 Источники
- [источник 1]

ВАЖНО: В конце добавь 'TERMINATE' для завершения диалога.
""",
            llm_config=self.llm_config,
        )

        # 6. ReasoningAgent (опционально) - для сложных задач
        if self.use_reasoning_agent and REASONING_AVAILABLE:
            self.agents["reasoner"] = ReasoningAgent(
                name="Reasoner",
                llm_config=self.llm_config,
                system_message="Ты решаешь сложные аналитические задачи с пошаговым рассуждением.",
                reason_config={
                    "method": "beam_search",  # beam_search, dfs, mcts, lats
                    "beam_size": 3,
                    "max_depth": 4,
                },
            )
            logger.info("✅ ReasoningAgent initialized with beam_search")

    def _init_guardrails(self):
        """Инициализация guardrails для защиты"""
        if not AG2_MODERN:
            return

        # 1. Regex guardrail - детектор чувствительных данных
        sensitive_data_guardrail = RegexGuardrail(
            name="sensitive_data_detector",
            condition=r".*(пароль|password|api.?key|secret|token|credentials).*",
            target=AgentTarget(self.agents["user_proxy"]),
            activation_message="⚠️ Обнаружены чувствительные данные - запрос отклонен"
        )

        # 2. LLM guardrail - детектор галлюцинаций
        hallucination_guardrail = LLMGuardrail(
            name="hallucination_detector",
            condition="""Проверь: содержит ли ответ утверждения о фактах без указания источников?
            Если агент утверждает что-то конкретное (даты, имена, решения) без ссылки на источник - это галлюцинация.""",
            target=AgentTarget(self.agents["critic"]),
            llm_config=self.llm_config,
            activation_message="⚠️ Обнаружена потенциальная галлюцинация - требуется проверка"
        )

        self.guardrails = [sensitive_data_guardrail, hallucination_guardrail]

        # Регистрируем guardrails на выходе Analyst
        self.agents["analyst"].register_output_guardrail(hallucination_guardrail)

        logger.info(f"✅ Guardrails initialized: {len(self.guardrails)} rules")

    async def _register_tools(self):
        """Регистрация инструментов для агентов"""
        researcher = self.agents["researcher"]
        user_proxy = self.agents["user_proxy"]

        # Обертки для async методов tools
        async def search_knowledge(query: Annotated[str, "Поисковый запрос"]) -> str:
            """Семантический поиск по базе знаний"""
            return await self.tools.search_knowledge(query)

        async def get_project_status(project_name: Annotated[str, "Название проекта"]) -> str:
            """Получить статус проекта"""
            return await self.tools.get_project_status(project_name)

        async def get_person_info(person_name: Annotated[str, "Имя сотрудника"]) -> str:
            """Информация о сотруднике"""
            return await self.tools.get_person_info(person_name)

        async def get_anomalies() -> str:
            """Список аномалий и рисков"""
            return await self.tools.get_anomalies_and_risks()

        async def get_recent_meetings(days: Annotated[int, "Количество дней"] = 7) -> str:
            """Получить недавние встречи"""
            if hasattr(self.tools, 'get_recent_meetings'):
                return await self.tools.get_recent_meetings(days)
            return await self.tools.search_knowledge(f"встречи за последние {days} дней")

        # Регистрация инструментов
        tools_to_register = [
            (search_knowledge, "search_knowledge", "Семантический поиск по базе знаний компании"),
            (get_project_status, "get_project_status", "Получить статус проекта"),
            (get_person_info, "get_person_info", "Информация о сотруднике"),
            (get_anomalies, "get_anomalies", "Список аномалий и рисков"),
            (get_recent_meetings, "get_recent_meetings", "Получить недавние встречи"),
        ]

        for func, name, description in tools_to_register:
            register_function(
                func,
                caller=researcher,
                executor=user_proxy,
                name=name,
                description=description,
            )

        logger.info(f"✅ Registered {len(tools_to_register)} tools for Researcher")

    def _setup_handoffs(self, context: ContextVariables):
        """Настройка handoffs между агентами"""
        if not AG2_MODERN:
            return

        planner = self.agents["planner"]
        researcher = self.agents["researcher"]
        critic = self.agents["critic"]
        analyst = self.agents["analyst"]

        # Planner -> Researcher (когда план готов)
        planner.handoffs.add_llm_conditions([
            OnCondition(
                target=AgentTarget(researcher),
                condition=StringLLMCondition(
                    prompt="Когда план исследования составлен и нужно начать поиск данных"
                ),
            ),
        ])

        # Researcher -> Critic (когда поиск завершен)
        researcher.handoffs.add_llm_conditions([
            OnCondition(
                target=AgentTarget(critic),
                condition=StringLLMCondition(
                    prompt="Когда поиск данных завершен и нужна проверка результатов"
                ),
            ),
        ])

        # Critic -> Researcher (если нужно больше данных)
        critic.handoffs.add_llm_conditions([
            OnCondition(
                target=AgentTarget(researcher),
                condition=StringLLMCondition(
                    prompt="Когда данных недостаточно и нужны дополнительные поиски"
                ),
            ),
            OnCondition(
                target=AgentTarget(analyst),
                condition=StringLLMCondition(
                    prompt="Когда данных достаточно и можно переходить к анализу"
                ),
            ),
        ])

        # Analyst -> Terminate (после финального ответа)
        analyst.handoffs.set_after_work(TerminateTarget())

        logger.info("✅ Handoffs configured between agents")

    async def run(
        self,
        query: str,
        user_id: str | None = None,
        session_id: str | None = None,
        max_rounds: int | None = None,
    ) -> DeepAnalysisReport:
        """
        Запуск глубокого анализа.

        Args:
            query: Запрос пользователя
            user_id: ID пользователя
            session_id: ID сессии
            max_rounds: Максимум раундов (по умолчанию из env)

        Returns:
            DeepAnalysisReport с результатами анализа
        """
        start_time = datetime.now()
        max_rounds = max_rounds or int(os.getenv("AG2_MAX_ROUNDS", "20"))

        # НОВОЕ: Загружаем агентскую память для контекста
        agent_memory_context = await self._load_agent_memory(query)

        # Создаем контекст
        context = create_analysis_context(
            user_id=user_id,
            session_id=session_id,
            query=query,
        )

        # Добавляем агентскую память в контекст
        if agent_memory_context:
            context["agent_memory"] = agent_memory_context

        # Регистрируем инструменты
        await self._register_tools()

        # Настраиваем handoffs
        self._setup_handoffs(context)

        report = DeepAnalysisReport(
            query=query,
            agents_involved=[],
        )

        try:
            if AG2_MODERN:
                # Используем современный паттерн с initiate_group_chat
                result = await self._run_modern_pattern(query, context, max_rounds)
            else:
                # Fallback на классический GroupChat
                result = await self._run_classic_groupchat(query, max_rounds)

            # Парсим результат
            report.final_answer = result.get("answer", "Не удалось получить ответ")
            report.agents_involved = result.get("agents", [])
            report.research_steps = result.get("steps", [])

            if result.get("analysis"):
                report.analysis = AnalysisResult(**result["analysis"])

        except Exception as e:
            logger.error(f"AG2 Engine error: {e}")
            report.errors.append(str(e))
            report.final_answer = f"Ошибка при анализе: {e}"

        # Время обработки
        report.processing_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        # НОВОЕ: Сохраняем результат в агентскую память
        await self._save_to_agent_memory(query, report)

        return report

    async def _load_agent_memory(self, query: str) -> Dict[str, Any]:
        """Загрузить релевантную агентскую память."""
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return {}

            from backend.core.memory import get_memgpt_manager

            memgpt = get_memgpt_manager()

            # Вспоминаем похожие запросы
            memories = await memgpt.agent_recall(
                agent_id="ag2_agentic",
                query=query,
                memory_type="episodic",
                limit=5
            )

            if memories:
                logger.info(f"📚 Loaded {len(memories)} agent memories for context")
                return {
                    "similar_queries": [
                        {
                            "query": m.get("content", {}).get("query", ""),
                            "answer_summary": m.get("content", {}).get("answer_summary", "")[:200],
                        }
                        for m in memories
                    ]
                }

            return {}

        except Exception as e:
            logger.debug(f"Could not load agent memory: {e}")
            return {}

    async def _save_to_agent_memory(self, query: str, report: DeepAnalysisReport):
        """Сохранить результат в агентскую память."""
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return

            from backend.core.memory import get_memgpt_manager

            memgpt = get_memgpt_manager()

            # Запоминаем успешный анализ
            if report.final_answer and not report.errors:
                await memgpt.agent_remember(
                    agent_id="ag2_agentic",
                    content={
                        "query": query,
                        "answer_summary": report.final_answer[:500],
                        "agents_involved": report.agents_involved,
                        "processing_time_ms": report.processing_time_ms,
                    },
                    memory_type="episodic",
                    importance=0.7,
                    tags=["agentic_search", "deep_analysis"]
                )
                logger.debug("💾 Saved analysis to agent memory")

        except Exception as e:
            logger.debug(f"Could not save to agent memory: {e}")

    async def _run_modern_pattern(
        self,
        query: str,
        context: ContextVariables,
        max_rounds: int,
    ) -> Dict[str, Any]:
        """Запуск с современным паттерном AG2 v0.9+"""

        # Создаем паттерн
        pattern = DefaultPattern(
            initial_agent=self.agents["planner"],
            agents=[
                self.agents["planner"],
                self.agents["researcher"],
                self.agents["critic"],
                self.agents["analyst"],
            ],
            user_agent=self.agents["user_proxy"],
            context_variables=context,
            group_manager_args={"llm_config": self.llm_config},
        )

        logger.info(f"🚀 Starting AG2 analysis with DefaultPattern: {query[:100]}...")

        # Запускаем групповой чат
        result, _final_context, _last_agent = await a_initiate_group_chat(
            pattern=pattern,
            messages=query,
            max_rounds=max_rounds,
        )

        # Извлекаем результат
        answer = ""
        agents_used = set()

        if hasattr(result, 'chat_history'):
            for msg in result.chat_history:
                agent_name = msg.get("name", "")
                agents_used.add(agent_name)

                if agent_name == "Analyst":
                    content = msg.get("content", "")
                    if content and "TERMINATE" in content:
                        answer = content.replace("TERMINATE", "").strip()

        return {
            "answer": answer or "Анализ не завершен",
            "agents": list(agents_used),
            "steps": [],
            "analysis": None,
        }

    async def _run_classic_groupchat(
        self,
        query: str,
        max_rounds: int,
    ) -> Dict[str, Any]:
        """Fallback на классический GroupChat"""
        from autogen import GroupChat, GroupChatManager

        groupchat = GroupChat(
            agents=[
                self.agents["user_proxy"],
                self.agents["planner"],
                self.agents["researcher"],
                self.agents["critic"],
                self.agents["analyst"],
            ],
            messages=[],
            max_round=max_rounds,
            speaker_selection_method="auto",
        )

        manager = GroupChatManager(
            groupchat=groupchat,
            llm_config=self.llm_config,
        )

        logger.info(f"🚀 Starting AG2 classic GroupChat: {query[:100]}...")

        result = await self.agents["user_proxy"].a_initiate_chat(
            manager,
            message=query,
        )

        # Извлекаем результат
        answer = ""
        agents_used = set()

        if hasattr(result, 'chat_history'):
            for msg in reversed(result.chat_history):
                agent_name = msg.get("name", "")
                agents_used.add(agent_name)

                if agent_name == "Analyst":
                    content = msg.get("content", "")
                    if content:
                        answer = content.replace("TERMINATE", "").strip()
                        break

        return {
            "answer": answer or "Анализ не завершен",
            "agents": list(agents_used),
            "steps": [],
            "analysis": None,
        }

    async def run_with_reasoning(
        self,
        query: str,
        method: str = "beam_search",
    ) -> Dict[str, Any]:
        """
        Запуск с ReasoningAgent для сложных задач.

        Args:
            query: Запрос
            method: Метод рассуждения (beam_search, dfs, mcts, lats)

        Returns:
            Результат с деревом рассуждений
        """
        if not self.use_reasoning_agent or not REASONING_AVAILABLE:
            return await self.run(query)

        reasoner = self.agents.get("reasoner")
        if not reasoner:
            return await self.run(query)

        # Обновляем метод рассуждения
        reasoner.reason_config["method"] = method

        user_proxy = UserProxyAgent(
            name="reasoning_user",
            human_input_mode="NEVER",
            code_execution_config=False,
            is_termination_msg=lambda x: True,
        )

        logger.info(f"🧠 Starting ReasoningAgent with {method}: {query[:100]}...")

        result = await user_proxy.a_initiate_chat(
            reasoner,
            message=query,
        )

        # Извлекаем ответ
        answer = ""
        if hasattr(result, 'summary'):
            answer = result.summary
        elif hasattr(result, 'chat_history') and result.chat_history:
            answer = result.chat_history[-1].get("content", "")

        # Извлекаем дерево рассуждений
        reasoning_tree = None
        if hasattr(reasoner, '_root'):
            reasoning_tree = reasoner._root.to_dict()

        return {
            "answer": answer,
            "method": method,
            "reasoning_tree": reasoning_tree,
        }


# ==========================================
# FACTORY FUNCTION
# ==========================================

def create_ag2_engine(
    tools_instance,
    llm_config: Dict[str, Any] | None = None,
    use_reasoning: bool = False,
    use_guardrails: bool = True,
) -> AG2AgenticEngineV2:
    """
    Фабричная функция для создания AG2 Engine.

    Args:
        tools_instance: Экземпляр TessentBrainTools
        llm_config: Конфигурация LLM (если None - используется Gemini по умолчанию)
        use_reasoning: Использовать ReasoningAgent
        use_guardrails: Включить guardrails

    Returns:
        Инициализированный AG2AgenticEngineV2
    """
    if llm_config is None:
        # Конфигурация по умолчанию для Gemini
        llm_config = {
            "config_list": [{
                "model": os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
                "api_key": os.getenv("GOOGLE_API_KEY"),
                "api_type": "google",
            }],
            "temperature": 0.3,
            "timeout": 120,
        }

    return AG2AgenticEngineV2(
        tools_instance=tools_instance,
        llm_config=llm_config,
        use_reasoning_agent=use_reasoning,
        use_guardrails=use_guardrails,
    )

