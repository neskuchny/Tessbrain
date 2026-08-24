# -*- coding: utf-8 -*-
"""
Agent Stream Handler - Real-time agent execution visualization.

Позволяет:
- Визуализация цепочки агентов
- Real-time обновления шагов выполнения
- Отображение промежуточных результатов
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from litestar import WebSocket
from litestar.handlers import WebsocketListener

logger = logging.getLogger(__name__)


class AgentStepStatus(str, Enum):
    """Статус шага агента."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class AgentStep:
    """Шаг выполнения агента."""
    agent_id: str
    agent_name: str
    step_index: int
    status: AgentStepStatus
    action: str
    input_summary: str = ""
    output_summary: str = ""
    duration_ms: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "step_index": self.step_index,
            "status": self.status.value,
            "action": self.action,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "metadata": self.metadata,
            "timestamp": self.timestamp
        }


@dataclass
class AgentChainProgress:
    """Прогресс выполнения цепочки агентов."""
    chain_id: str
    query: str
    total_steps: int
    current_step: int
    steps: List[AgentStep]
    status: str  # running, completed, failed
    started_at: str
    completed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "query": self.query,
            "total_steps": self.total_steps,
            "current_step": self.current_step,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result
        }


class AgentStreamManager:
    """Менеджер стриминга агентов."""

    def __init__(self):
        self.active_chains: Dict[str, AgentChainProgress] = {}
        self.subscribers: Dict[str, List[WebSocket]] = {}  # chain_id -> websockets

    def create_chain(self, chain_id: str, query: str, total_steps: int) -> AgentChainProgress:
        """Создать новую цепочку."""
        chain = AgentChainProgress(
            chain_id=chain_id,
            query=query,
            total_steps=total_steps,
            current_step=0,
            steps=[],
            status="running",
            started_at=datetime.now().isoformat()
        )
        self.active_chains[chain_id] = chain
        return chain

    async def update_step(
        self,
        chain_id: str,
        step: AgentStep
    ):
        """Обновить шаг и уведомить подписчиков."""
        if chain_id not in self.active_chains:
            return

        chain = self.active_chains[chain_id]

        # Обновляем или добавляем шаг
        existing_step_idx = next(
            (i for i, s in enumerate(chain.steps) if s.agent_id == step.agent_id),
            None
        )

        if existing_step_idx is not None:
            chain.steps[existing_step_idx] = step
        else:
            chain.steps.append(step)

        # Обновляем current_step
        if step.status == AgentStepStatus.RUNNING:
            chain.current_step = step.step_index

        # Уведомляем подписчиков
        await self._notify_subscribers(chain_id, {
            "type": "step_update",
            "chain_id": chain_id,
            "step": step.to_dict(),
            "progress": {
                "current": chain.current_step,
                "total": chain.total_steps
            }
        })

    async def complete_chain(
        self,
        chain_id: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ):
        """Завершить цепочку."""
        if chain_id not in self.active_chains:
            return

        chain = self.active_chains[chain_id]
        chain.completed_at = datetime.now().isoformat()
        chain.status = "failed" if error else "completed"
        chain.result = result

        # Уведомляем подписчиков
        await self._notify_subscribers(chain_id, {
            "type": "chain_complete",
            "chain_id": chain_id,
            "status": chain.status,
            "result": result,
            "error": error,
            "total_duration_ms": self._calculate_duration(chain)
        })

        # Удаляем подписчиков
        if chain_id in self.subscribers:
            del self.subscribers[chain_id]

    def subscribe(self, chain_id: str, websocket: WebSocket):
        """Подписаться на обновления цепочки."""
        if chain_id not in self.subscribers:
            self.subscribers[chain_id] = []
        self.subscribers[chain_id].append(websocket)

    def unsubscribe(self, chain_id: str, websocket: WebSocket):
        """Отписаться от обновлений."""
        if chain_id in self.subscribers:
            self.subscribers[chain_id] = [
                ws for ws in self.subscribers[chain_id] if ws != websocket
            ]

    async def _notify_subscribers(self, chain_id: str, message: Dict[str, Any]):
        """Уведомить всех подписчиков."""
        if chain_id not in self.subscribers:
            return

        message_json = json.dumps(message, ensure_ascii=False)

        for websocket in self.subscribers[chain_id]:
            try:
                await websocket.send_text(message_json)
            except Exception as e:
                logger.warning(f"⚠️ Failed to notify subscriber: {e}")

    def _calculate_duration(self, chain: AgentChainProgress) -> int:
        """Вычислить общую длительность."""
        if not chain.started_at or not chain.completed_at:
            return 0

        start = datetime.fromisoformat(chain.started_at)
        end = datetime.fromisoformat(chain.completed_at)
        return int((end - start).total_seconds() * 1000)

    def get_chain_status(self, chain_id: str) -> Optional[Dict[str, Any]]:
        """Получить статус цепочки."""
        if chain_id in self.active_chains:
            return self.active_chains[chain_id].to_dict()
        return None


# Глобальный менеджер
agent_stream_manager = AgentStreamManager()


class AgentStreamHandler(WebsocketListener):
    """
    WebSocket handler для визуализации агентов.

    Протокол:
    1. Клиент подключается: /ws/agents/{chain_id}
    2. Сервер отправляет текущий статус
    3. Сервер стримит обновления шагов
    4. Сервер отправляет завершение
    """

    path = "/ws/agents/{chain_id:str}"

    def __init__(self):
        self.chain_id: Optional[str] = None

    async def on_accept(self, socket: WebSocket, chain_id: str) -> None:
        """При подключении."""
        await socket.accept()
        self.chain_id = chain_id

        # Подписываемся на обновления
        agent_stream_manager.subscribe(chain_id, socket)

        # Отправляем текущий статус
        current_status = agent_stream_manager.get_chain_status(chain_id)
        if current_status:
            await socket.send_text(json.dumps({
                "type": "initial_status",
                "chain": current_status
            }, ensure_ascii=False))
        else:
            await socket.send_text(json.dumps({
                "type": "error",
                "message": f"Chain {chain_id} not found"
            }, ensure_ascii=False))

        logger.info(f"🔌 Agent stream connected: {chain_id}")

    async def on_disconnect(self, socket: WebSocket) -> None:
        """При отключении."""
        if self.chain_id:
            agent_stream_manager.unsubscribe(self.chain_id, socket)
        logger.info(f"🔌 Agent stream disconnected: {self.chain_id}")

    async def on_receive(self, data: str) -> str:
        """Обработка входящих сообщений."""
        try:
            message = json.loads(data)
            msg_type = message.get("type", "unknown")

            if msg_type == "ping":
                return json.dumps({"type": "pong"})
            elif msg_type == "get_status":
                status = agent_stream_manager.get_chain_status(self.chain_id)
                return json.dumps({
                    "type": "status",
                    "chain": status
                }, ensure_ascii=False)
            else:
                return json.dumps({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}"
                })

        except Exception as e:
            return json.dumps({
                "type": "error",
                "message": str(e)
            })


# Утилиты для использования из других частей системы

async def start_agent_chain(chain_id: str, query: str, agents: List[str]) -> AgentChainProgress:
    """
    Начать отслеживание цепочки агентов.

    Args:
        chain_id: Уникальный ID цепочки
        query: Исходный запрос
        agents: Список имён агентов в цепочке
    """
    chain = agent_stream_manager.create_chain(
        chain_id=chain_id,
        query=query,
        total_steps=len(agents)
    )

    # Создаём шаги для каждого агента
    for i, agent_name in enumerate(agents):
        step = AgentStep(
            agent_id=f"{chain_id}_{agent_name}_{i}",
            agent_name=agent_name,
            step_index=i,
            status=AgentStepStatus.PENDING,
            action="waiting"
        )
        chain.steps.append(step)

    return chain


async def update_agent_step(
    chain_id: str,
    agent_name: str,
    status: AgentStepStatus,
    action: str,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int = 0,
    error: Optional[str] = None
):
    """
    Обновить статус шага агента.
    """
    chain = agent_stream_manager.active_chains.get(chain_id)
    if not chain:
        return

    # Находим шаг
    step_idx = next(
        (i for i, s in enumerate(chain.steps) if s.agent_name == agent_name),
        None
    )

    if step_idx is None:
        return

    step = AgentStep(
        agent_id=f"{chain_id}_{agent_name}_{step_idx}",
        agent_name=agent_name,
        step_index=step_idx,
        status=status,
        action=action,
        input_summary=input_summary,
        output_summary=output_summary,
        duration_ms=duration_ms,
        error=error
    )

    await agent_stream_manager.update_step(chain_id, step)


async def complete_agent_chain(
    chain_id: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None
):
    """
    Завершить цепочку агентов.
    """
    await agent_stream_manager.complete_chain(chain_id, result, error)


