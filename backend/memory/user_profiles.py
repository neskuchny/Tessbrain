# -*- coding: utf-8 -*-
"""
User Profiles Service - Персонализация ответов.

Функции:
- Хранение предпочтений пользователя
- Отслеживание паттернов запросов
- Персонализация ответов
- Адаптация стиля коммуникации
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CommunicationStyle(str, Enum):
    """Стиль коммуникации."""
    STANDARD = "standard"  # Нейтральный (по умолчанию)
    FORMAL = "formal"  # Формальный
    CASUAL = "casual"  # Неформальный
    TECHNICAL = "technical"  # Технический
    EXECUTIVE = "executive"  # Для руководителей


class DetailLevel(str, Enum):
    """Уровень детализации."""
    BRIEF = "brief"  # Краткий
    STANDARD = "standard"  # Стандартный
    DETAILED = "detailed"  # Подробный


@dataclass
class UserProfile:
    """Профиль пользователя."""
    user_id: str

    # Базовая информация
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None  # admin, manager, developer, etc.
    department: Optional[str] = None

    # Предпочтения коммуникации
    communication_style: CommunicationStyle = CommunicationStyle.STANDARD
    detail_level: DetailLevel = DetailLevel.STANDARD
    preferred_language: str = "ru"

    # Экспертиза
    expertise: List[str] = field(default_factory=list)
    known_topics: List[str] = field(default_factory=list)  # Что пользователь уже знает
    blind_spots: List[str] = field(default_factory=list)  # Пробелы в знаниях

    # Контекст
    current_projects: List[str] = field(default_factory=list)
    current_focus: Optional[str] = None

    # Поведение
    typical_query_types: List[str] = field(default_factory=list)
    decision_style: Optional[str] = None  # data-driven, intuitive, consensus-seeking

    # Метаданные
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_active_at: Optional[str] = None
    query_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "department": self.department,
            "communication_style": self.communication_style.value if isinstance(self.communication_style, CommunicationStyle) else self.communication_style,
            "detail_level": self.detail_level.value if isinstance(self.detail_level, DetailLevel) else self.detail_level,
            "preferred_language": self.preferred_language,
            "expertise": self.expertise,
            "known_topics": self.known_topics,
            "blind_spots": self.blind_spots,
            "current_projects": self.current_projects,
            "current_focus": self.current_focus,
            "typical_query_types": self.typical_query_types,
            "decision_style": self.decision_style,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_active_at": self.last_active_at,
            "query_count": self.query_count
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
        # Преобразуем enum значения
        if "communication_style" in data and isinstance(data["communication_style"], str):
            try:
                data["communication_style"] = CommunicationStyle(data["communication_style"])
            except ValueError:
                data["communication_style"] = CommunicationStyle.STANDARD

        if "detail_level" in data and isinstance(data["detail_level"], str):
            try:
                data["detail_level"] = DetailLevel(data["detail_level"])
            except ValueError:
                data["detail_level"] = DetailLevel.STANDARD

        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class QueryPattern:
    """Паттерн запросов пользователя."""
    pattern_id: str
    user_id: str
    pattern_type: str  # question_type, topic, time_of_day, etc.
    pattern_value: str
    frequency: int = 1
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


class UserProfileService:
    """
    Сервис управления профилями пользователей.

    Использование:
    ```python
    service = UserProfileService()
    await service.initialize()

    # Получить профиль
    profile = await service.get_profile("user_123")

    # Обновить профиль
    await service.update_profile("user_123", {"current_focus": "Project Alpha"})

    # Записать запрос для анализа паттернов
    await service.record_query("user_123", "Какой статус проекта?", "project_status")

    # Получить персонализированный промпт
    prompt = await service.get_personalized_prompt("user_123")
    ```
    """

    def __init__(self, db_path: str = "data/user_profiles.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None
        self._initialized = False

    async def initialize(self):
        """Инициализация базы данных."""
        if self._initialized:
            return

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Создаём таблицы
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                profile_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS query_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                pattern_type TEXT NOT NULL,
                pattern_value TEXT NOT NULL,
                frequency INTEGER DEFAULT 1,
                last_seen TEXT NOT NULL,
                metadata TEXT,
                UNIQUE(user_id, pattern_type, pattern_value)
            );

            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                query TEXT NOT NULL,
                query_type TEXT,
                response_quality REAL,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_patterns_user ON query_patterns(user_id);
            CREATE INDEX IF NOT EXISTS idx_history_user ON query_history(user_id);
        """)

        self.conn.commit()
        self._initialized = True
        logger.info("✅ UserProfileService initialized")

    async def list_user_ids(self, limit: int = 5000) -> list[str]:
        """Все user_id с профилем (для рассылок/агрегаций). Best-effort."""
        if not self._initialized:
            await self.initialize()
        try:
            cur = self.conn.execute(
                "SELECT user_id FROM user_profiles LIMIT ?", (limit,))
            return [r["user_id"] for r in cur.fetchall()]
        except Exception:
            return []

    async def get_profile(self, user_id: str) -> UserProfile:
        """Получить профиль пользователя."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute(
            "SELECT profile_data FROM user_profiles WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()

        if row:
            data = json.loads(row["profile_data"])
            return UserProfile.from_dict(data)

        # Создаём новый профиль
        profile = UserProfile(user_id=user_id)
        await self.save_profile(profile)
        return profile

    async def save_profile(self, profile: UserProfile):
        """Сохранить профиль."""
        if not self._initialized:
            await self.initialize()

        profile.updated_at = datetime.now().isoformat()

        self.conn.execute("""
            INSERT OR REPLACE INTO user_profiles (user_id, profile_data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (
            profile.user_id,
            json.dumps(profile.to_dict(), ensure_ascii=False),
            profile.created_at,
            profile.updated_at
        ))
        self.conn.commit()

    async def update_profile(self, user_id: str, updates: Dict[str, Any]) -> UserProfile:
        """Обновить профиль."""
        profile = await self.get_profile(user_id)

        for key, value in updates.items():
            if hasattr(profile, key):
                setattr(profile, key, value)

        await self.save_profile(profile)
        return profile

    async def record_query(
        self,
        user_id: str,
        query: str,
        query_type: Optional[str] = None,
        response_quality: Optional[float] = None
    ):
        """Записать запрос для анализа."""
        if not self._initialized:
            await self.initialize()

        now = datetime.now().isoformat()

        # Записываем в историю
        self.conn.execute("""
            INSERT INTO query_history (user_id, query, query_type, response_quality, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, query, query_type, response_quality, now))

        # Обновляем паттерны
        if query_type:
            await self._update_pattern(user_id, "query_type", query_type)

        # Извлекаем темы из запроса
        topics = self._extract_topics(query)
        for topic in topics:
            await self._update_pattern(user_id, "topic", topic)

        # Обновляем профиль
        profile = await self.get_profile(user_id)
        profile.query_count += 1
        profile.last_active_at = now
        await self.save_profile(profile)

        self.conn.commit()

    async def _update_pattern(self, user_id: str, pattern_type: str, pattern_value: str):
        """Обновить паттерн."""
        now = datetime.now().isoformat()

        cursor = self.conn.execute("""
            SELECT frequency FROM query_patterns
            WHERE user_id = ? AND pattern_type = ? AND pattern_value = ?
        """, (user_id, pattern_type, pattern_value))

        row = cursor.fetchone()

        if row:
            self.conn.execute("""
                UPDATE query_patterns
                SET frequency = frequency + 1, last_seen = ?
                WHERE user_id = ? AND pattern_type = ? AND pattern_value = ?
            """, (now, user_id, pattern_type, pattern_value))
        else:
            self.conn.execute("""
                INSERT INTO query_patterns (user_id, pattern_type, pattern_value, frequency, last_seen)
                VALUES (?, ?, ?, 1, ?)
            """, (user_id, pattern_type, pattern_value, now))

    def _extract_topics(self, query: str) -> List[str]:
        """Извлечь темы из запроса (простая эвристика)."""
        topics = []

        # Ключевые слова для определения тем
        topic_keywords = {
            "project": ["проект", "project", "задач", "task"],
            "person": ["кто", "who", "сотрудник", "employee", "человек"],
            "meeting": ["встреч", "meeting", "совещани"],
            "status": ["статус", "status", "прогресс", "progress"],
            "decision": ["решени", "decision", "принят"],
            "risk": ["риск", "risk", "проблем", "problem"],
            "kpi": ["kpi", "метрик", "metric", "показател"],
            "timeline": ["срок", "deadline", "когда", "when", "дата"]
        }

        query_lower = query.lower()

        for topic, keywords in topic_keywords.items():
            if any(kw in query_lower for kw in keywords):
                topics.append(topic)

        return topics

    async def get_recent_queries(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Получить последние запросы пользователя.

        Используется для batch-анализа предпочтений (ночная консолидация).

        Args:
            user_id: ID пользователя
            limit: Максимальное количество запросов

        Returns:
            Список словарей с query, query_type, timestamp
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute("""
            SELECT query, query_type, response_quality, timestamp
            FROM query_history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, limit))

        results = []
        for row in cursor.fetchall():
            results.append({
                "query": row["query"],
                "query_type": row["query_type"],
                "response_quality": row["response_quality"],
                "timestamp": row["timestamp"],
            })

        return results

    async def learn_from_feedback(
        self,
        user_id: str,
        query: str,
        feedback: str,
        rating: Optional[float] = None,
    ):
        """
        Обучение на обратной связи пользователя.

        Если пользователь дал feedback (исправил, одобрил, отклонил) —
        обновляем профиль и паттерны.

        Args:
            user_id: ID пользователя
            query: Исходный запрос
            feedback: Текст обратной связи ("good", "bad", "correction: ...")
            rating: Числовая оценка (1-5)
        """
        if not self._initialized:
            await self.initialize()

        profile = await self.get_profile(user_id)

        # Обновляем response_quality для последнего запроса
        if rating is not None:
            self.conn.execute("""
                UPDATE query_history
                SET response_quality = ?
                WHERE user_id = ? AND query = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (rating, user_id, query))

        # Адаптируем detail_level на основе feedback
        feedback_lower = (feedback or "").lower()

        if any(w in feedback_lower for w in ["короче", "кратко", "слишком длинно", "too long"]):
            profile.detail_level = DetailLevel.BRIEF
        elif any(w in feedback_lower for w in ["подробнее", "больше деталей", "more details"]):
            profile.detail_level = DetailLevel.DETAILED

        await self.save_profile(profile)
        self.conn.commit()

        logger.info(f"Learned from feedback for user {user_id}: {feedback[:50]}")

    async def get_user_patterns(self, user_id: str, limit: int = 10) -> List[QueryPattern]:
        """Получить паттерны пользователя."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute("""
            SELECT * FROM query_patterns
            WHERE user_id = ?
            ORDER BY frequency DESC, last_seen DESC
            LIMIT ?
        """, (user_id, limit))

        patterns = []
        for row in cursor.fetchall():
            patterns.append(QueryPattern(
                pattern_id=str(row["id"]),
                user_id=row["user_id"],
                pattern_type=row["pattern_type"],
                pattern_value=row["pattern_value"],
                frequency=row["frequency"],
                last_seen=row["last_seen"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {}
            ))

        return patterns

    async def get_personalized_prompt(self, user_id: str) -> str:
        """
        Получить персонализированный промпт для LLM.

        Используется для адаптации ответов под пользователя.
        """
        profile = await self.get_profile(user_id)
        patterns = await self.get_user_patterns(user_id, limit=5)

        prompt_parts = []

        # Стиль коммуникации
        style_instructions = {
            CommunicationStyle.FORMAL: "Отвечай формально и профессионально.",
            CommunicationStyle.CASUAL: "Отвечай дружелюбно и неформально.",
            CommunicationStyle.TECHNICAL: "Используй технические термины и детали.",
            CommunicationStyle.EXECUTIVE: "Давай краткие, структурированные ответы для руководителей."
        }

        if profile.communication_style in style_instructions:
            prompt_parts.append(style_instructions[profile.communication_style])

        # Уровень детализации
        detail_instructions = {
            DetailLevel.BRIEF: "Будь кратким, только ключевые факты.",
            DetailLevel.STANDARD: "Давай сбалансированные ответы.",
            DetailLevel.DETAILED: "Давай подробные, развёрнутые ответы."
        }

        if profile.detail_level in detail_instructions:
            prompt_parts.append(detail_instructions[profile.detail_level])

        # Роль пользователя
        if profile.role:
            prompt_parts.append(f"Пользователь: {profile.role}.")

        # Текущий фокус
        if profile.current_focus:
            prompt_parts.append(f"Текущий фокус пользователя: {profile.current_focus}.")

        # Экспертиза
        if profile.expertise:
            prompt_parts.append(f"Пользователь разбирается в: {', '.join(profile.expertise)}.")

        # Известные темы (не нужно объяснять)
        if profile.known_topics:
            prompt_parts.append(f"Не объясняй базовые понятия: {', '.join(profile.known_topics)}.")

        # Частые темы запросов
        topic_patterns = [p for p in patterns if p.pattern_type == "topic"]
        if topic_patterns:
            frequent_topics = [p.pattern_value for p in topic_patterns[:3]]
            prompt_parts.append(f"Часто спрашивает о: {', '.join(frequent_topics)}.")

        return "\n".join(prompt_parts) if prompt_parts else ""

    async def learn_from_feedback(
        self,
        user_id: str,
        query: str,
        was_helpful: bool,
        feedback: Optional[str] = None
    ):
        """
        Обучение на основе обратной связи.

        Адаптирует профиль на основе реакции пользователя.
        """
        profile = await self.get_profile(user_id)

        if was_helpful:
            # Положительная обратная связь - запоминаем что работает
            topics = self._extract_topics(query)
            for topic in topics:
                if topic not in profile.known_topics:
                    profile.known_topics.append(topic)
        else:
            # Отрицательная - возможно нужно больше деталей
            if profile.detail_level == DetailLevel.BRIEF:
                profile.detail_level = DetailLevel.STANDARD
            elif profile.detail_level == DetailLevel.STANDARD:
                profile.detail_level = DetailLevel.DETAILED

        await self.save_profile(profile)

    async def close(self):
        """Закрыть соединение."""
        if self.conn:
            self.conn.close()
            logger.info("✅ UserProfileService closed")


# Глобальный экземпляр
_user_profile_service: Optional[UserProfileService] = None


def get_user_profile_service() -> UserProfileService:
    """Получить глобальный экземпляр сервиса."""
    global _user_profile_service
    if _user_profile_service is None:
        _user_profile_service = UserProfileService()
    return _user_profile_service


