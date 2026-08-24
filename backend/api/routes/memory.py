# -*- coding: utf-8 -*-
"""
Memory API Routes - эндпоинты для работы с долгосрочной памятью.
"""

import logging
from typing import Any, Dict, List, Optional

from litestar import Controller, delete, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

from ...core.memory import MemoryManager, get_memory_manager

logger = logging.getLogger(__name__)


class AddMemoryRequest(BaseModel):
    """Запрос на добавление памяти"""
    content: str
    user_id: str
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class AddConversationRequest(BaseModel):
    """Запрос на добавление разговора"""
    messages: List[Dict[str, str]]  # [{"role": "user/assistant", "content": "..."}]
    user_id: str
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SearchMemoryRequest(BaseModel):
    """Запрос на поиск в памяти"""
    query: str
    user_id: str
    limit: int = 10
    filters: Optional[Dict[str, Any]] = None


class RecallRequest(BaseModel):
    """Запрос на recall"""
    query: str
    user_id: str
    include_graph: bool = True
    limit: int = 10


class MemoryController(Controller):
    """Контроллер для работы с памятью"""

    guards = [enforce_user_id_matches_token]
    path = "/memory"
    tags = ["Memory"]

    @staticmethod
    def _uid(authorization: Optional[str], requested: Optional[str]) -> Optional[str]:
        """Анти-IDOR для body user_id (guard роутера тело не видит): токен
        побеждает; чужой body-id при валидном токене → 401."""
        from backend.core.auth.user_guard import resolve_user_or_none
        uid = resolve_user_or_none(authorization, requested, scope="memory")
        if authorization and uid is None:
            raise HTTPException(status_code=401, detail="user_id does not match token")
        return uid

    def _get_manager(self) -> MemoryManager:
        """Получить менеджер памяти"""
        try:
            return get_memory_manager()
        except Exception as e:
            logger.error(f"Failed to get memory manager: {e}")
            raise HTTPException(
                status_code=503,
                detail="Memory service unavailable"
            )

    @post("/add")
    async def add_memory(self, data: AddMemoryRequest,
                         authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> Dict[str, Any]:
        """
        Добавить память из текста.

        Mem0 автоматически извлечёт важные факты.
        """
        data.user_id = self._uid(authorization, data.user_id)
        manager = self._get_manager()

        result = await manager.mem0.add_memory(
            content=data.content,
            user_id=data.user_id,
            session_id=data.session_id,
            metadata=data.metadata
        )

        return {
            "status": "success",
            "result": result
        }

    @post("/add-conversation")
    async def add_conversation(self, data: AddConversationRequest,
                               authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> Dict[str, Any]:
        """
        Добавить разговор в память.

        Mem0 автоматически извлечёт важные факты из диалога.
        """
        data.user_id = self._uid(authorization, data.user_id)
        manager = self._get_manager()

        result = await manager.remember_conversation(
            session_id=data.session_id or "default",
            user_id=data.user_id,
            messages=data.messages,
            extract_entities=True
        )

        return {
            "status": "success",
            "result": result
        }

    @post("/search")
    async def search_memory(self, data: SearchMemoryRequest,
                            authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> Dict[str, Any]:
        """
        Семантический поиск по памяти.
        """
        data.user_id = self._uid(authorization, data.user_id)
        manager = self._get_manager()

        memories = await manager.mem0.search_memory(
            query=data.query,
            user_id=data.user_id,
            limit=data.limit,
            filters=data.filters
        )

        return {
            "status": "success",
            "count": len(memories),
            "memories": memories
        }

    @post("/recall")
    async def recall(self, data: RecallRequest,
                     authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> Dict[str, Any]:
        """
        Вспомнить релевантную информацию.

        Объединяет поиск по mem0 и графу знаний.
        """
        data.user_id = self._uid(authorization, data.user_id)
        manager = self._get_manager()

        result = await manager.recall(
            query=data.query,
            user_id=data.user_id,
            include_graph=data.include_graph,
            limit=data.limit
        )

        return {
            "status": "success",
            "memories_count": len(result.get("memories", [])),
            "graph_context_count": len(result.get("graph_context", [])),
            "combined_context": result.get("combined_context", ""),
            "memories": result.get("memories", []),
            "graph_context": result.get("graph_context", [])
        }

    @get("/footprint/{user_id:str}")
    async def memory_footprint(self, user_id: str) -> Dict[str, Any]:
        """Объём и наклон памяти: сколько узлов/связей/версий, как растёт.

        Долгоживущую память разоряет не размер, а наклон роста — здесь он
        виден: текущий объём, рост за 7 и 30 дней, лидеры роста по типам.
        Пока снимков мало, отчёт честно говорит «истории ещё нет», а не
        рисует нули, которые читаются как «не растёт».
        """
        from backend.core.store.memory_footprint import growth_report
        return {"status": "success", "user_id": user_id,
                **growth_report(user_id)}

    @get("/user/{user_id:str}")
    async def get_user_memories(
        self,
        user_id: str,
        limit: int = Parameter(default=50, ge=1, le=200)
    ) -> Dict[str, Any]:
        """
        Получить все воспоминания пользователя.
        """
        manager = self._get_manager()

        memories = await manager.mem0.get_all_memories(
            user_id=user_id,
            limit=limit
        )

        return {
            "status": "success",
            "user_id": user_id,
            "count": len(memories),
            "memories": memories
        }

    @get("/user/{user_id:str}/profile")
    async def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """
        Получить профиль пользователя на основе памяти.

        Возвращает предпочтения, факты и интересы.
        """
        manager = self._get_manager()

        profile = await manager.get_user_profile(user_id)

        return {
            "status": "success",
            "profile": profile
        }

    @get("/{memory_id:str}")
    async def get_memory(self, memory_id: str) -> Dict[str, Any]:
        """
        Получить конкретное воспоминание по ID.
        """
        manager = self._get_manager()

        memory = await manager.mem0.get_memory_by_id(memory_id)

        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")

        return {
            "status": "success",
            "memory": memory
        }

    @get("/{memory_id:str}/history")
    async def get_memory_history(self, memory_id: str) -> Dict[str, Any]:
        """
        Получить историю изменений воспоминания.
        """
        manager = self._get_manager()

        history = await manager.mem0.get_memory_history(memory_id)

        return {
            "status": "success",
            "memory_id": memory_id,
            "history": history
        }

    @delete("/{memory_id:str}", status_code=200)
    async def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        """
        Удалить воспоминание.
        """
        manager = self._get_manager()

        success = await manager.mem0.delete_memory(memory_id)

        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete memory")

        return {
            "status": "success",
            "deleted": memory_id
        }

    @delete("/user/{user_id:str}/all", status_code=200)
    async def delete_all_user_memories(self, user_id: str) -> Dict[str, Any]:
        """
        Удалить все воспоминания пользователя.

        ⚠️ Это действие необратимо!
        """
        manager = self._get_manager()

        success = await manager.mem0.delete_all_memories(user_id)

        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete memories")

        return {
            "status": "success",
            "message": f"All memories for user {user_id} deleted"
        }


# Router для подключения к app
router = MemoryController

