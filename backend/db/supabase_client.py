# -*- coding: utf-8 -*-
"""
Supabase клиент для Tessbrain.
Адаптировано из alfa_asynk_meetflow/automation_tools_async.py

Обеспечивает:
- Получение встреч и транскриптов
- Управление проектами и папками
- Создание и обновление задач
- Работа с пользователями и организациями
"""
import contextvars
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Context vars для пользовательского контекста
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_current_user_id", default=None)
_current_org_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_current_org_id", default=None)


def set_user_context(user_id: Optional[str] = None, org_id: Optional[str] = None):
    """Установить контекст пользователя для запросов"""
    _current_user_id.set(user_id)
    _current_org_id.set(org_id)


def get_current_user_id() -> Optional[str]:
    """Получить текущий user_id"""
    return _current_user_id.get()


def get_current_org_id() -> Optional[str]:
    """Получить текущий org_id"""
    return _current_org_id.get()


class SupabaseClient:
    """
    Асинхронный клиент для работы с Supabase.

    Поддерживает:
    - REST API для CRUD операций
    - Контекст пользователя для RLS
    - Retry с exponential backoff
    - Типизированные методы для всех сущностей

    Пример использования:
    ```python
    client = SupabaseClient()

    # Установка контекста пользователя
    set_user_context(user_id="user_123", org_id="org_456")

    # Получение встреч
    meetings = await client.get_meetings(project_id="proj_789")

    # Получение деталей встречи с транскриптом
    meeting = await client.get_meeting_details(meeting_id="meet_123")
    ```
    """

    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
        timeout: float = 12.0,
        retry_attempts: int = 3
    ):
        """
        Args:
            url: URL Supabase проекта
            key: API ключ Supabase (anon или service_role)
            timeout: Таймаут HTTP запросов
            retry_attempts: Количество попыток при ошибке
        """
        raw_url = url or os.environ.get("SUPABASE_URL", "")
        self.url = raw_url.rstrip("/")
        self.key = key or os.environ.get("SUPABASE_KEY", "")
        self.timeout = timeout
        self.retry_attempts = retry_attempts

        # Проверка и нормализация URL
        if self.url and not self.url.startswith(("http://", "https://")):
            logger.warning(f"⚠️ SUPABASE_URL должен начинаться с http:// или https://. Получено: {self.url[:50]}")
            self.url = f"https://{self.url}"

        if not self.url or not self.key:
            logger.warning("⚠️ Database connection not configured. Check environment variables.")
        else:
            # Маскируем URL для безопасности
            masked_url = self.url[:20] + "..." if len(self.url) > 20 else self.url
            logger.info(f"✅ SupabaseClient initialized: {masked_url}")

    def _headers(self) -> Dict[str, str]:
        """Заголовки для запросов"""
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

    async def debug_list_meetings(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Диагностический метод: получить первые N встреч без фильтров.
        Используется для отладки проблем с доступом.
        """
        try:
            params = {"select": "*", "limit": str(limit)}
            meetings = await self._request("GET", "/rest/v1/meetings", params=params)
            logger.info(f"🔍 Debug: Found {len(meetings)} meetings in DB")
            if meetings:
                # Выводим структуру первой встречи (без sensitive data)
                first = meetings[0]
                logger.info(f"🔍 Debug: Sample meeting keys: {list(first.keys())}")
                logger.info(f"🔍 Debug: Sample meeting_id: {first.get('meeting_id', 'N/A')[:20]}...")
            return meetings
        except Exception as e:
            logger.error(f"❌ Debug: Failed to list meetings: {e}")
            return []

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        **kwargs
    ) -> Any:
        """Выполнить HTTP запрос с retry"""
        if not self.url or not self.key:
            raise RuntimeError("Database connection not configured")

        url = f"{self.url}{path}"

        # Merge headers from kwargs if present
        headers = self._headers()
        if "headers" in kwargs:
            headers.update(kwargs["headers"])

        last_error = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.retry_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException))
        ):
            with attempt:
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.request(
                            method=method,
                            url=url,
                            headers=headers,
                            params=params,
                            json=json_data
                        )
                        response.raise_for_status()

                        if response.status_code == 204:
                            return None

                        return response.json()
                except httpx.ConnectError as e:
                    last_error = e
                    logger.warning(f"⚠️ Connection error (attempt {attempt.retry_state.attempt_number}): {e}")
                    if attempt.retry_state.attempt_number >= self.retry_attempts:
                        raise RuntimeError(
                            f"Не удалось подключиться к Supabase. "
                            f"Проверьте SUPABASE_URL: {self.url[:50]}... "
                            f"Ошибка: {e}"
                        ) from e
                    raise
                except httpx.HTTPStatusError as e:
                    # HTTP ошибки не ретраим
                    logger.error(f"❌ HTTP error {e.response.status_code}: {e.response.text[:200]}")
                    raise
                except Exception as e:
                    last_error = e
                    logger.error(f"❌ Unexpected error: {e}")
                    raise

        # Если дошли сюда, значит все попытки исчерпаны
        raise RuntimeError(f"Не удалось выполнить запрос после {self.retry_attempts} попыток: {last_error}")

    # ==================== MEETINGS ====================

    async def get_meetings(
        self,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at.desc"
    ) -> List[Dict[str, Any]]:
        """
        Получить список встреч.

        Args:
            user_id: ID пользователя (если не указан, берется из контекста)
            project_id: Фильтр по проекту
            folder_id: Фильтр по папке
            status: Статус встречи (active, archived, deleted)
            limit: Максимум записей
            offset: Смещение для пагинации
            order_by: Сортировка
        """
        target_user = user_id or get_current_user_id()
        if not target_user:
            raise ValueError("user_id is required")

        params = {
            "user_id": f"eq.{target_user}",
            "status": f"eq.{status}",
            "select": "id,meeting_id,title,created_at,transcription_status,meeting_type,project_id,folder_id",
            "order": order_by,
            "limit": str(limit),
            "offset": str(offset)
        }

        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if folder_id:
            params["folder_id"] = f"eq.{folder_id}"

        return await self._request("GET", "/rest/v1/meetings", params=params)

    async def get_meeting_details(
        self,
        meeting_id: str,
        include_transcript: bool = True,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Получить детали встречи с транскриптом.

        Args:
            meeting_id: ID встречи (UUID, short ID suffix, или title)
            include_transcript: Включить полный транскрипт
            user_id: владелец — фильтр тенанта. Клиент ходит service-ключом
                (RLS обходится), поэтому передавайте user_id везде, где id
                встречи приходит извне (tool-args агента, API): без него
                поиск шёл по ВСЕМ тенантам, а фолбэк ilike-по-title отдавал
                чужую встречу по подстроке.
        """
        import uuid

        def is_uuid(val: str) -> bool:
            try:
                uuid.UUID(val)
                return True
            except Exception:
                return False

        def _scope(params: Dict[str, str]) -> Dict[str, str]:
            if user_id:
                params["user_id"] = f"eq.{user_id}"
            return params

        meetings = None

        # ПРИОРИТЕТ 1: Если это полный UUID, ищем по 'id'
        if is_uuid(meeting_id):
            params = _scope({"id": f"eq.{meeting_id}", "select": "*"})
            meetings = await self._request("GET", "/rest/v1/meetings", params=params)

        # ПРИОРИТЕТ 2: Если это короткий ID (последние 8 символов UUID), ищем по частичному совпадению
        # Используем Supabase text search: id.cs.*{suffix} означает "contains suffix"
        # Но для UUID нужен другой подход - ищем по meeting_id, который может быть строкой
        if not meetings and not is_uuid(meeting_id):
            # Пробуем найти по meeting_id (если это строка, а не UUID)
            params = _scope({"meeting_id": f"eq.{meeting_id}", "select": "*"})
            meetings = await self._request("GET", "/rest/v1/meetings", params=params)

        # ПРИОРИТЕТ 3: Ищем по title
        if not meetings:
            params = _scope({"title": f"eq.{meeting_id}", "select": "*"})
            meetings = await self._request("GET", "/rest/v1/meetings", params=params)

        # ПРИОРИТЕТ 4: Частичное совпадение по title — ТОЛЬКО внутри тенанта.
        # Глобальный ilike по всем встречам всех пользователей — утечка.
        if not meetings and user_id:
            params = _scope({"title": f"ilike.%{meeting_id}%", "select": "*"})
            meetings = await self._request("GET", "/rest/v1/meetings", params=params)

        if not meetings:
            return None

        return meetings[0]

    async def get_meeting_transcript(self, meeting_id: str,
                                     user_id: Optional[str] = None) -> Optional[str]:
        """Получить только транскрипт встречи"""
        meeting = await self.get_meeting_details(meeting_id, user_id=user_id)
        if meeting:
            # Поле транскрипта может называться по-разному
            return (
                meeting.get("transcription_text") or  # Новая схема
                meeting.get("transcription") or       # Альтернативное название
                meeting.get("transcript") or          # Ещё одно возможное название
                ""
            )
        return None

    async def get_meetings_with_transcripts(
        self,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Получить встречи сразу с транскриптами (одним запросом).
        Оптимизировано для генерации документов.
        """
        target_user = user_id or get_current_user_id()
        if not target_user:
            raise ValueError("user_id is required")

        params = {
            "user_id": f"eq.{target_user}",
            "status": "eq.active",
            # Берём все поля чтобы не гадать с названиями колонок
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
            "offset": str(offset)
        }

        return await self._request("GET", "/rest/v1/meetings", params=params)

    async def get_meetings_by_period(
        self,
        days_back: int = 7,
        days_ahead: int = 0,
        user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Получить встречи за период"""
        target_user = user_id or get_current_user_id()
        if not target_user:
            raise ValueError("user_id is required")

        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days_back)).isoformat()
        end = (now + timedelta(days=days_ahead)).isoformat()

        params = {
            "user_id": f"eq.{target_user}",
            "status": "eq.active",
            "created_at": f"gte.{start}",
            "and": f"(created_at.lte.{end})",
            "select": "id,meeting_id,title,created_at,transcription_status,meeting_type",
            "order": "created_at.desc"
        }

        return await self._request("GET", "/rest/v1/meetings", params=params)

    # ==================== PROJECTS ====================

    async def get_projects(
        self,
        user_id: Optional[str] = None,
        status: str = "active"
    ) -> List[Dict[str, Any]]:
        """Получить проекты пользователя"""
        target_user = user_id or get_current_user_id()
        if not target_user:
            raise ValueError("user_id is required")

        params = {
            "user_id": f"eq.{target_user}",
            "status": f"eq.{status}",
            "select": "project_id,name,description,created_at,status",
            "order": "created_at.desc"
        }

        return await self._request("GET", "/rest/v1/projects", params=params)

    async def get_all_projects(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Получить все проекты для синхронизации с графом (игнорируя user_id)"""
        params = {
            "status": "eq.active",
            "select": "project_id,name,description,created_at,status",
            "order": "created_at.desc",
            "limit": str(limit)
        }
        return await self._request("GET", "/rest/v1/projects", params=params)

    async def get_project_by_id(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Получить проект по ID"""
        params = {"project_id": f"eq.{project_id}", "select": "*"}
        projects = await self._request("GET", "/rest/v1/projects", params=params)
        return projects[0] if projects else None

    # ==================== FOLDERS ====================

    async def get_folders(
        self,
        project_id: str,
        user_id: Optional[str] = None,
        status: str = "active"
    ) -> List[Dict[str, Any]]:
        """Получить папки проекта"""
        target_user = user_id or get_current_user_id()
        if not target_user:
            raise ValueError("user_id is required")

        params = {
            "project_id": f"eq.{project_id}",
            "user_id": f"eq.{target_user}",
            "status": f"eq.{status}",
            "select": "id,name,parent_folder_id,created_at",
            "order": "created_at.desc"
        }

        return await self._request("GET", "/rest/v1/folders", params=params)

    async def get_all_folders(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Получить все папки для синхронизации с графом"""
        params = {
            "status": "eq.active",
            "select": "id,name,project_id,parent_folder_id,created_at",
            "order": "created_at.desc",
            "limit": str(limit)
        }
        return await self._request("GET", "/rest/v1/folders", params=params)

    # ==================== TASKS ====================

    async def get_meeting_tasks(self, meeting_id: str,
                                user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Получить задачи встречи. user_id — тенант-фильтр (см.
        get_meeting_details): резолв по title без него ищет по всем тенантам."""
        import uuid

        def is_uuid(val: str) -> bool:
            try:
                uuid.UUID(val)
                return True
            except Exception:
                return False

        # Пробуем по meeting_id
        params = {"meeting_id": f"eq.{meeting_id}", "select": "*"}
        tasks = await self._request("GET", "/rest/v1/meeting_tasks", params=params)

        if tasks:
            return tasks

        # Если не UUID, ищем meeting_id по title
        if not is_uuid(meeting_id):
            meeting = await self.get_meeting_details(meeting_id, user_id=user_id)
            if meeting:
                actual_meeting_id = meeting.get("meeting_id") or meeting.get("id")
                params = {"meeting_id": f"eq.{actual_meeting_id}", "select": "*"}
                tasks = await self._request("GET", "/rest/v1/meeting_tasks", params=params)

        return tasks or []

    async def create_task(
        self,
        meeting_id: str,
        title: str,
        description: str = "",
        assignee: Optional[str] = None,
        deadline: Optional[str] = None,
        priority: str = "medium",
        status: str = "pending"
    ) -> Dict[str, Any]:
        """Создать задачу для встречи"""
        task_data = {
            "meeting_id": meeting_id,
            "title": title,
            "description": description,
            "assignee": assignee,
            "deadline": deadline,
            "priority": priority,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        result = await self._request("POST", "/rest/v1/meeting_tasks", json_data=task_data)
        return result[0] if result else task_data

    async def update_task(
        self,
        task_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Обновить задачу"""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        result = await self._request(
            "PATCH",
            f"/rest/v1/meeting_tasks?id=eq.{task_id}",
            json_data=updates
        )
        return result[0] if result else updates

    # ==================== USERS ====================

    async def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить пользователя по ID"""
        params = {"id": f"eq.{user_id}", "select": "*"}
        users = await self._request("GET", "/rest/v1/users", params=params)
        return users[0] if users else None

    async def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Получить пользователя по email"""
        params = {"email": f"eq.{email}", "select": "*"}
        users = await self._request("GET", "/rest/v1/users", params=params)
        return users[0] if users else None

    # ==================== ORGANIZATIONS ====================

    async def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        """Получить организацию по ID"""
        params = {"id": f"eq.{org_id}", "select": "*"}
        orgs = await self._request("GET", "/rest/v1/organizations", params=params)
        return orgs[0] if orgs else None

    async def get_user_organizations(self, user_id: str) -> List[Dict[str, Any]]:
        """Получить организации пользователя"""
        params = {
            "user_id": f"eq.{user_id}",
            "select": "organization_id,role,organizations(id,name,slug)"
        }
        return await self._request("GET", "/rest/v1/organization_members", params=params)

    # ==================== ENTITIES (для Knowledge Graph) ====================

    async def save_entities(
        self,
        meeting_id: str,
        entities: List[Dict[str, Any]],
        entity_type: str
    ) -> List[Dict[str, Any]]:
        """Сохранить извлеченные сущности"""
        records = []
        for entity in entities:
            record = {
                "meeting_id": meeting_id,
                "entity_type": entity_type,
                "entity_id": entity.get(f"{entity_type}_id") or entity.get("id"),
                "data": json.dumps(entity, ensure_ascii=False),
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            records.append(record)

        if records:
            return await self._request("POST", "/rest/v1/extracted_entities", json_data=records)
        return []

    async def get_entities(
        self,
        meeting_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Получить сохраненные сущности"""
        params = {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit)
        }

        if meeting_id:
            params["meeting_id"] = f"eq.{meeting_id}"
        if entity_type:
            params["entity_type"] = f"eq.{entity_type}"

        return await self._request("GET", "/rest/v1/extracted_entities", params=params)


# Singleton
_global_client: Optional[SupabaseClient] = None


def get_supabase_client() -> SupabaseClient:
    """Получить глобальный экземпляр SupabaseClient"""
    global _global_client
    if _global_client is None:
        _global_client = SupabaseClient()
    return _global_client


def reset_global_client():
    """Сбросить глобальный клиент"""
    global _global_client
    _global_client = None
