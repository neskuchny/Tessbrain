# -*- coding: utf-8 -*-
"""
Chat WebSocket Handler - Real-time chat streaming.

Позволяет:
- Streaming ответов от LLM
- Real-time обновления статуса
- Уведомления о прогрессе обработки
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from litestar import WebSocket
from litestar.handlers import WebsocketListener

logger = logging.getLogger(__name__)


@dataclass
class WSMessage:
    """WebSocket сообщение."""
    type: str  # chat, status, error, thinking, answer_chunk
    content: Any
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class ConnectionManager:
    """Менеджер WebSocket соединений."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_sessions: Dict[str, str] = {}  # user_id -> connection_id

    async def connect(self, websocket: WebSocket, connection_id: str, user_id: Optional[str] = None):
        """Подключить клиента."""
        await websocket.accept()
        self.active_connections[connection_id] = websocket

        if user_id:
            self.user_sessions[user_id] = connection_id

        logger.info(f"🔌 WebSocket connected: {connection_id} (user={user_id})")

    def disconnect(self, connection_id: str):
        """Отключить клиента."""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]

        # Удаляем из user_sessions
        for user_id, conn_id in list(self.user_sessions.items()):
            if conn_id == connection_id:
                del self.user_sessions[user_id]

        logger.info(f"🔌 WebSocket disconnected: {connection_id}")

    async def send_to_connection(self, connection_id: str, message: WSMessage):
        """Отправить сообщение конкретному соединению."""
        if connection_id in self.active_connections:
            websocket = self.active_connections[connection_id]
            try:
                await websocket.send_text(message.to_json())
            except Exception as e:
                logger.warning(f"⚠️ Failed to send to {connection_id}: {e}")
                self.disconnect(connection_id)

    async def send_to_user(self, user_id: str, message: WSMessage):
        """Отправить сообщение пользователю."""
        if user_id in self.user_sessions:
            connection_id = self.user_sessions[user_id]
            await self.send_to_connection(connection_id, message)

    async def broadcast(self, message: WSMessage):
        """Отправить сообщение всем."""
        for connection_id in list(self.active_connections.keys()):
            await self.send_to_connection(connection_id, message)


# Глобальный менеджер соединений
connection_manager = ConnectionManager()


class ChatWebSocketHandler(WebsocketListener):
    """
    WebSocket handler для чата.

    Протокол:
    1. Клиент подключается и отправляет: {"type": "auth", "user_id": "...", "session_id": "..."}
    2. Клиент отправляет запрос: {"type": "query", "content": "...", "strategy": "standard"}
    3. Сервер отправляет статус: {"type": "status", "content": "thinking"}
    4. Сервер стримит ответ: {"type": "answer_chunk", "content": "..."}
    5. Сервер завершает: {"type": "answer_complete", "content": {...}}
    """

    path = "/ws/chat"

    def __init__(self):
        self.reasoning_engine = None
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.connection_id: Optional[str] = None

    async def on_accept(self, socket: WebSocket) -> None:
        """При подключении."""
        import uuid
        self.connection_id = str(uuid.uuid4())
        await connection_manager.connect(socket, self.connection_id)

        # Отправляем приветствие
        await socket.send_text(WSMessage(
            type="connected",
            content={"connection_id": self.connection_id}
        ).to_json())

    async def on_disconnect(self, socket: WebSocket) -> None:
        """При отключении."""
        if self.connection_id:
            connection_manager.disconnect(self.connection_id)

    async def on_receive(self, data: str) -> str:
        """Обработка входящих сообщений."""
        try:
            message = json.loads(data)
            msg_type = message.get("type", "unknown")

            if msg_type == "auth":
                return await self._handle_auth(message)
            elif msg_type == "query":
                return await self._handle_query(message)
            elif msg_type == "ping":
                return WSMessage(type="pong", content={}).to_json()
            else:
                return WSMessage(
                    type="error",
                    content=f"Unknown message type: {msg_type}"
                ).to_json()

        except json.JSONDecodeError:
            return WSMessage(
                type="error",
                content="Invalid JSON"
            ).to_json()
        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}")
            return WSMessage(
                type="error",
                content=str(e)
            ).to_json()

    async def _handle_auth(self, message: Dict[str, Any]) -> str:
        """Обработка аутентификации."""
        self.user_id = message.get("user_id")
        self.session_id = message.get("session_id")

        if self.user_id and self.connection_id:
            connection_manager.user_sessions[self.user_id] = self.connection_id

        logger.info(f"🔐 WebSocket auth: user={self.user_id}, session={self.session_id}")

        return WSMessage(
            type="auth_success",
            content={
                "user_id": self.user_id,
                "session_id": self.session_id
            }
        ).to_json()

    async def _handle_query(self, message: Dict[str, Any]) -> str:
        """Обработка запроса с streaming."""
        query = message.get("content", "")
        strategy = message.get("strategy", "standard")
        context = message.get("context", {})

        if not query:
            return WSMessage(
                type="error",
                content="Empty query"
            ).to_json()

        # Инициализируем ReasoningEngine если нужно
        if not self.reasoning_engine:
            await self._init_reasoning_engine()

        # Отправляем статус "thinking"
        await self._send_status("thinking", {"query": query, "strategy": strategy})

        try:
            # Выполняем запрос
            result = await self.reasoning_engine.reason(
                query=query,
                context={
                    **context,
                    "user_id": self.user_id,
                    "session_id": self.session_id
                },
                strategy=self._get_strategy(strategy)
            )

            # Отправляем статус "generating"
            await self._send_status("generating", {})

            # Стримим ответ по частям (симуляция для длинных ответов)
            answer = result.answer
            chunk_size = 50  # символов

            for i in range(0, len(answer), chunk_size):
                chunk = answer[i:i+chunk_size]
                await self._send_chunk(chunk, i == 0)
                await asyncio.sleep(0.02)  # Небольшая задержка для эффекта

            # Отправляем завершение
            return WSMessage(
                type="answer_complete",
                content={
                    "answer": answer,
                    "data": result.data,
                    "sources": result.sources,
                    "confidence": result.confidence,
                    "processing_time_ms": result.processing_time_ms
                }
            ).to_json()

        except Exception as e:
            logger.error(f"❌ Query error: {e}")
            return WSMessage(
                type="error",
                content=str(e)
            ).to_json()

    async def _init_reasoning_engine(self):
        """Инициализация ReasoningEngine."""
        from ...core.llm.router import get_llm_router
        from ...core.store.graph_builder import GraphBuilder
        from ...core.store.vector_indexer import VectorIndexer
        from ...core.think.reasoning_engine import ReasoningEngine

        graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
        vector_indexer = VectorIndexer()  # модель из config.py
        llm = get_llm_router()

        self.reasoning_engine = ReasoningEngine(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer,
            llm_router=llm
        )

    def _get_strategy(self, strategy_name: str):
        """Получить стратегию поиска."""
        from ...core.think.logical_planner import SearchStrategy

        mapping = {
            "quick": SearchStrategy.QUICK,
            "standard": SearchStrategy.STANDARD,
            "deep": SearchStrategy.DEEP,
            "agentic": SearchStrategy.DEEP
        }
        return mapping.get(strategy_name.lower(), SearchStrategy.STANDARD)

    async def _send_status(self, status: str, data: Dict[str, Any]):
        """Отправить статус."""
        if self.connection_id:
            await connection_manager.send_to_connection(
                self.connection_id,
                WSMessage(type="status", content={"status": status, **data})
            )

    async def _send_chunk(self, chunk: str, is_first: bool = False):
        """Отправить чанк ответа."""
        if self.connection_id:
            await connection_manager.send_to_connection(
                self.connection_id,
                WSMessage(
                    type="answer_chunk",
                    content=chunk,
                    metadata={"is_first": is_first}
                )
            )


# Утилиты для отправки уведомлений из других частей системы

async def notify_user(user_id: str, notification_type: str, content: Any):
    """
    Отправить уведомление пользователю.

    Args:
        user_id: ID пользователя
        notification_type: Тип уведомления (processing_complete, new_meeting, etc.)
        content: Содержимое уведомления
    """
    await connection_manager.send_to_user(
        user_id,
        WSMessage(type=notification_type, content=content)
    )


async def broadcast_notification(notification_type: str, content: Any):
    """
    Отправить уведомление всем подключённым клиентам.
    """
    await connection_manager.broadcast(
        WSMessage(type=notification_type, content=content)
    )


