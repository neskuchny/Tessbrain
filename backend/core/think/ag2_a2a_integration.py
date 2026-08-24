# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - AG2 A2A (Agent-to-Agent) Protocol Integration
==============================================================

Реализация A2A протокола для взаимодействия с внешними агентами:
- A2A Server - экспонирование агентов Tessbrain как REST сервисов
- A2A Client - подключение к внешним агентам (Mark001, MeetFlow, и др.)
- Robust Error Handling - retry логика с exponential backoff
- Agent Discovery - автоматическое обнаружение агентов

A2A позволяет:
- Взаимодействовать с агентами на других фреймворках (Pydantic AI, LangGraph)
- Распределять нагрузку между сервисами
- Масштабировать агентную систему горизонтально
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Callable, Dict, List, Optional

# AG2 Core imports
from autogen import ConversableAgent
from pydantic import BaseModel, Field

# A2A imports (AG2 v0.9+)
try:
    from autogen.a2a.client import A2aRemoteAgent
    from autogen.a2a.server import A2aAgentServer, CardSettings
    A2A_AVAILABLE = True
except ImportError:
    A2A_AVAILABLE = False
    A2aAgentServer = None
    A2aRemoteAgent = None
    CardSettings = None

logger = logging.getLogger(__name__)


# ==========================================
# PYDANTIC MODELS
# ==========================================

class AgentCardInfo(BaseModel):
    """Информация об агенте (Agent Card)"""
    name: str = Field(..., description="Имя агента")
    description: str = Field(..., description="Описание агента")
    version: str = Field("1.0.0", description="Версия")
    url: str = Field("", description="URL агента")
    capabilities: List[str] = Field(default_factory=list, description="Возможности")
    input_modes: List[str] = Field(default_factory=lambda: ["text"])
    output_modes: List[str] = Field(default_factory=lambda: ["text"])
    is_available: bool = Field(True, description="Доступен ли агент")


class A2ACallResult(BaseModel):
    """Результат вызова A2A агента"""
    success: bool = Field(..., description="Успешен ли вызов")
    agent_name: str = Field(..., description="Имя агента")
    response: str = Field("", description="Ответ агента")
    error: Optional[str] = Field(None, description="Ошибка (если есть)")
    latency_ms: int = Field(0, description="Время ответа в мс")
    retries: int = Field(0, description="Количество retry")


class A2AServerConfig(BaseModel):
    """Конфигурация A2A сервера"""
    host: str = Field("0.0.0.0", description="Host для сервера")
    port: int = Field(8000, description="Порт сервера")
    agent_name: str = Field(..., description="Имя агента")
    description: str = Field("", description="Описание")
    version: str = Field("1.0.0", description="Версия")
    enable_streaming: bool = Field(True, description="Включить streaming")
    enable_function_calling: bool = Field(True, description="Включить function calling")


# ==========================================
# A2A SERVER
# ==========================================

class TessentA2AServer:
    """
    A2A Server для экспонирования агентов Tessbrain.

    Позволяет внешним системам (Mark001, MeetFlow) взаимодействовать
    с агентами Tessbrain через стандартный A2A протокол.
    """

    def __init__(
        self,
        agent: ConversableAgent,
        config: A2AServerConfig,
        tools: List[Callable] | None = None,
    ):
        """
        Args:
            agent: ConversableAgent для экспонирования
            config: Конфигурация сервера
            tools: Список инструментов для регистрации
        """
        if not A2A_AVAILABLE:
            raise ImportError("A2A not available. Install AG2 v0.9+ with: pip install ag2[a2a]")

        self.agent = agent
        self.config = config
        self.tools = tools or []
        self._server = None
        self._app = None

        # Регистрируем инструменты
        self._register_tools()

        # Создаем A2A сервер
        self._create_server()

        logger.info(f"✅ TessentA2AServer initialized: {config.agent_name} on {config.host}:{config.port}")

    def _register_tools(self):
        """Регистрация инструментов для агента"""
        for tool in self.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            tool_desc = getattr(tool, '__doc__', '') or f"Tool: {tool_name}"

            self.agent.register_for_llm(
                name=tool_name,
                description=tool_desc,
            )(tool)

            self.agent.register_for_execution(name=tool_name)(tool)

            logger.debug(f"📎 Registered tool: {tool_name}")

    def _create_server(self):
        """Создание A2A сервера"""
        agent_card = CardSettings(
            name=self.config.agent_name,
            description=self.config.description,
            version=self.config.version,
        )

        self._server = A2aAgentServer(
            agent=self.agent,
            agent_card=agent_card,
        )

        self._app = self._server.build()

    def get_app(self):
        """Получить ASGI приложение для uvicorn"""
        return self._app

    def get_agent_card(self) -> AgentCardInfo:
        """Получить информацию об агенте"""
        return AgentCardInfo(
            name=self.config.agent_name,
            description=self.config.description,
            version=self.config.version,
            url=f"http://{self.config.host}:{self.config.port}",
            capabilities=[
                "streaming" if self.config.enable_streaming else None,
                "function_calling" if self.config.enable_function_calling else None,
            ],
            input_modes=["text"],
            output_modes=["text"],
            is_available=True,
        )


# ==========================================
# A2A CLIENT
# ==========================================

class TessentA2AClient:
    """
    A2A Client для подключения к внешним агентам.

    Поддерживает:
    - Подключение к Mark001, MeetFlow и другим агентам
    - Retry с exponential backoff
    - Автоматическое обнаружение агентов
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        """
        Args:
            timeout: Таймаут запроса в секундах
            max_retries: Максимум retry при ошибках
        """
        if not A2A_AVAILABLE:
            raise ImportError("A2A not available. Install AG2 v0.9+ with: pip install ag2[a2a]")

        self.timeout = timeout
        self.max_retries = max_retries

        # Кэш подключенных агентов
        self._agents: Dict[str, A2aRemoteAgent] = {}
        self._agent_cards: Dict[str, AgentCardInfo] = {}

        logger.info(f"✅ TessentA2AClient initialized (timeout={timeout}s, retries={max_retries})")

    def connect(
        self,
        name: str,
        url: str,
        description: str = "",
        headers: Dict[str, str] | None = None,
    ) -> A2aRemoteAgent:
        """
        Подключиться к удаленному агенту.

        Args:
            name: Имя агента
            url: URL агента
            description: Описание
            headers: Дополнительные заголовки (для авторизации)

        Returns:
            A2aRemoteAgent
        """
        if name in self._agents:
            logger.debug(f"♻️ Reusing existing connection: {name}")
            return self._agents[name]

        agent = A2aRemoteAgent(
            url=url,
            name=name,
            headers=headers,
        )

        self._agents[name] = agent
        self._agent_cards[name] = AgentCardInfo(
            name=name,
            url=url,
            description=description,
        )

        logger.info(f"🔗 Connected to remote agent: {name} at {url}")
        return agent

    def connect_mark001(self, url: str | None = None) -> A2aRemoteAgent:
        """Подключиться к Mark001"""
        url = url or os.getenv("MARK001_A2A_URL", "http://localhost:8004")
        return self.connect(
            name="Mark001",
            url=url,
            description="Маркетинговые агенты Mark001",
        )

    def connect_meetflow(self, url: str | None = None) -> A2aRemoteAgent:
        """Подключиться к MeetFlow"""
        url = url or os.getenv("MEETFLOW_A2A_URL", "http://localhost:8005")
        return self.connect(
            name="MeetFlow",
            url=url,
            description="Агенты автоматизации MeetFlow",
        )

    def connect_tessent_brain(self, url: str | None = None) -> A2aRemoteAgent:
        """Подключиться к Tessbrain (другой инстанс)"""
        url = url or os.getenv("TESSENT_BRAIN_A2A_URL", "http://localhost:8001")
        return self.connect(
            name="TessentBrain",
            url=url,
            description="Tessbrain Knowledge Agent",
        )

    async def call(
        self,
        agent_name: str,
        message: str,
        caller_agent: ConversableAgent = None,
    ) -> A2ACallResult:
        """
        Вызвать удаленного агента с retry логикой.

        Args:
            agent_name: Имя агента
            message: Сообщение
            caller_agent: Агент-инициатор (опционально)

        Returns:
            A2ACallResult
        """
        if agent_name not in self._agents:
            return A2ACallResult(
                success=False,
                agent_name=agent_name,
                error=f"Agent '{agent_name}' not connected. Call connect() first.",
            )

        remote_agent = self._agents[agent_name]
        start_time = datetime.now()
        retries = 0

        for attempt in range(self.max_retries):
            try:
                if caller_agent:
                    # Используем caller_agent для инициации чата
                    response = await caller_agent.a_initiate_chat(
                        remote_agent,
                        message=message,
                        max_turns=1,
                    )

                    # Извлекаем ответ
                    answer = ""
                    if hasattr(response, 'chat_history') and response.chat_history:
                        for msg in reversed(response.chat_history):
                            if msg.get("role") == "assistant" or msg.get("name") == agent_name:
                                answer = msg.get("content", "")
                                break

                    latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                    return A2ACallResult(
                        success=True,
                        agent_name=agent_name,
                        response=answer,
                        latency_ms=latency_ms,
                        retries=retries,
                    )
                else:
                    # Прямой вызов через run()
                    response = await remote_agent.a_run(
                        message=message,
                        max_turns=1,
                    )

                    await response.process()

                    latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                    return A2ACallResult(
                        success=True,
                        agent_name=agent_name,
                        response=response.summary or "",
                        latency_ms=latency_ms,
                        retries=retries,
                    )

            except asyncio.TimeoutError:
                retries += 1
                if attempt == self.max_retries - 1:
                    return A2ACallResult(
                        success=False,
                        agent_name=agent_name,
                        error=f"Timeout after {self.max_retries} retries",
                        retries=retries,
                    )
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

            except Exception as e:
                error_str = str(e)

                # Retry на 5xx ошибки
                if "500" in error_str or "502" in error_str or "503" in error_str:
                    retries += 1
                    if attempt == self.max_retries - 1:
                        return A2ACallResult(
                            success=False,
                            agent_name=agent_name,
                            error=f"Server error: {error_str}",
                            retries=retries,
                        )
                    await asyncio.sleep(2 ** attempt)
                else:
                    # Клиентская ошибка - не retry
                    return A2ACallResult(
                        success=False,
                        agent_name=agent_name,
                        error=error_str,
                        retries=retries,
                    )

        return A2ACallResult(
            success=False,
            agent_name=agent_name,
            error="Unknown error",
            retries=retries,
        )

    def list_agents(self) -> List[AgentCardInfo]:
        """Список подключенных агентов"""
        return list(self._agent_cards.values())

    def disconnect(self, agent_name: str):
        """Отключиться от агента"""
        if agent_name in self._agents:
            del self._agents[agent_name]
            del self._agent_cards[agent_name]
            logger.info(f"🔌 Disconnected from: {agent_name}")

    def disconnect_all(self):
        """Отключиться от всех агентов"""
        self._agents.clear()
        self._agent_cards.clear()
        logger.info("🔌 Disconnected from all agents")


# ==========================================
# A2A ORCHESTRATOR
# ==========================================

class TessentA2AOrchestrator:
    """
    Оркестратор для координации нескольких A2A агентов.

    Позволяет:
    - Маршрутизировать запросы к нужным агентам
    - Параллельно вызывать несколько агентов
    - Агрегировать результаты
    """

    def __init__(self, client: TessentA2AClient = None):
        """
        Args:
            client: A2A клиент (создается автоматически если не передан)
        """
        self.client = client or TessentA2AClient()

        # Правила маршрутизации: keyword -> agent_name
        self._routing_rules: Dict[str, str] = {}

        logger.info("✅ TessentA2AOrchestrator initialized")

    def add_routing_rule(self, keywords: List[str], agent_name: str):
        """
        Добавить правило маршрутизации.

        Args:
            keywords: Ключевые слова
            agent_name: Имя агента для маршрутизации
        """
        for keyword in keywords:
            self._routing_rules[keyword.lower()] = agent_name

        logger.debug(f"📍 Added routing rule: {keywords} -> {agent_name}")

    def route(self, message: str) -> Optional[str]:
        """
        Определить агента для обработки сообщения.

        Args:
            message: Сообщение

        Returns:
            Имя агента или None
        """
        message_lower = message.lower()

        for keyword, agent_name in self._routing_rules.items():
            if keyword in message_lower:
                return agent_name

        return None

    async def call_routed(
        self,
        message: str,
        caller_agent: ConversableAgent = None,
        default_agent: str | None = None,
    ) -> A2ACallResult:
        """
        Вызвать агента на основе маршрутизации.

        Args:
            message: Сообщение
            caller_agent: Агент-инициатор
            default_agent: Агент по умолчанию

        Returns:
            A2ACallResult
        """
        agent_name = self.route(message) or default_agent

        if not agent_name:
            return A2ACallResult(
                success=False,
                agent_name="",
                error="No agent found for this message",
            )

        return await self.client.call(agent_name, message, caller_agent)

    async def call_parallel(
        self,
        message: str,
        agent_names: List[str],
        caller_agent: ConversableAgent = None,
    ) -> List[A2ACallResult]:
        """
        Параллельный вызов нескольких агентов.

        Args:
            message: Сообщение
            agent_names: Список агентов
            caller_agent: Агент-инициатор

        Returns:
            Список результатов
        """
        tasks = [
            self.client.call(name, message, caller_agent)
            for name in agent_names
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Преобразуем исключения в A2ACallResult
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append(A2ACallResult(
                    success=False,
                    agent_name=agent_names[i],
                    error=str(result),
                ))
            else:
                processed_results.append(result)

        return processed_results

    async def call_chain(
        self,
        message: str,
        agent_chain: List[str],
        caller_agent: ConversableAgent = None,
    ) -> A2ACallResult:
        """
        Последовательный вызов цепочки агентов.

        Результат каждого агента передается следующему.

        Args:
            message: Начальное сообщение
            agent_chain: Цепочка агентов
            caller_agent: Агент-инициатор

        Returns:
            Финальный результат
        """
        current_message = message
        final_result = None

        for agent_name in agent_chain:
            result = await self.client.call(agent_name, current_message, caller_agent)

            if not result.success:
                return result

            current_message = result.response
            final_result = result

        return final_result or A2ACallResult(
            success=False,
            agent_name="",
            error="Empty agent chain",
        )


# ==========================================
# FACTORY FUNCTIONS
# ==========================================

def create_a2a_server(
    agent: ConversableAgent,
    name: str,
    description: str = "",
    host: str = "0.0.0.0",
    port: int = 8000,
    tools: List[Callable] | None = None,
) -> TessentA2AServer:
    """
    Создать A2A сервер.

    Args:
        agent: Агент для экспонирования
        name: Имя агента
        description: Описание
        host: Host
        port: Порт
        tools: Инструменты

    Returns:
        TessentA2AServer
    """
    config = A2AServerConfig(
        host=host,
        port=port,
        agent_name=name,
        description=description,
    )

    return TessentA2AServer(agent, config, tools)


def create_a2a_client(
    timeout: int = 30,
    max_retries: int = 3,
) -> TessentA2AClient:
    """
    Создать A2A клиент.

    Args:
        timeout: Таймаут
        max_retries: Макс. retry

    Returns:
        TessentA2AClient
    """
    return TessentA2AClient(timeout, max_retries)


def create_a2a_orchestrator(
    client: TessentA2AClient = None,
) -> TessentA2AOrchestrator:
    """
    Создать A2A оркестратор.

    Args:
        client: A2A клиент

    Returns:
        TessentA2AOrchestrator
    """
    return TessentA2AOrchestrator(client)


# ==========================================
# PREDEFINED AGENT CONNECTIONS
# ==========================================

async def setup_tessent_ecosystem(
    client: TessentA2AClient = None,
) -> TessentA2AOrchestrator:
    """
    Настроить подключения к экосистеме Tessbrain.

    Подключает:
    - Mark001 (маркетинг)
    - MeetFlow (автоматизация)

    Returns:
        Настроенный оркестратор
    """
    client = client or create_a2a_client()
    orchestrator = create_a2a_orchestrator(client)

    # Подключаем агентов
    try:
        client.connect_mark001()
        orchestrator.add_routing_rule(
            ["маркетинг", "marketing", "реклама", "контент", "smm", "seo", "roistat"],
            "Mark001"
        )
    except Exception as e:
        logger.warning(f"Could not connect to Mark001: {e}")

    try:
        client.connect_meetflow()
        orchestrator.add_routing_rule(
            ["встреча", "meeting", "календарь", "calendar", "telegram", "notion", "задача", "task"],
            "MeetFlow"
        )
    except Exception as e:
        logger.warning(f"Could not connect to MeetFlow: {e}")

    logger.info(f"✅ Tessbrain ecosystem setup complete: {len(client.list_agents())} agents connected")

    return orchestrator

