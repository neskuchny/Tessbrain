# -*- coding: utf-8 -*-
"""
Agent Memory Manager - Персистентная память для агентов.

Реализует 4 типа памяти (по аналогии с человеческой):
1. Working Memory - активный контекст сессии (кратковременная)
2. Episodic Memory - конкретные события и факты
3. Semantic Memory - обобщённые знания и паттерны
4. Procedural Memory - навыки и процедуры

Использование:
```python
from backend.core.memory.agent_memory import AgentMemoryManager

memory = AgentMemoryManager(storage_path="data/agent_memories")

# Запоминание
await memory.remember(
    agent_id="search_agent",
    content={"query": "...", "result": "..."},
    memory_type="episodic"
)

# Вспоминание
memories = await memory.recall(
    agent_id="search_agent",
    query="похожий запрос",
    limit=5
)

# Консолидация (перевод из episodic в semantic)
await memory.consolidate(agent_id="search_agent")
```
"""
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryType(Enum):
    """Типы памяти."""
    WORKING = "working"      # Активный контекст (не сохраняется)
    EPISODIC = "episodic"    # Конкретные события
    SEMANTIC = "semantic"    # Обобщённые знания
    PROCEDURAL = "procedural"  # Навыки и процедуры


@dataclass
class MemoryItem:
    """Элемент памяти."""
    id: str
    agent_id: str
    memory_type: str
    content: Dict[str, Any]
    created_at: str
    last_accessed: Optional[str] = None
    access_count: int = 0
    importance: float = 1.0
    decay_rate: float = 0.01
    tags: List[str] = field(default_factory=list)
    source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryItem":
        return cls(**data)


class AgentMemoryManager:
    """
    Управление персистентной памятью агентов.

    Особенности:
    - Раздельное хранение по агентам
    - 4 типа памяти с разными характеристиками
    - Автоматическое затухание неиспользуемых воспоминаний
    - Консолидация (episodic → semantic)
    """

    def __init__(self, storage_path: str = "data/agent_memories"):
        """
        Args:
            storage_path: Путь для хранения памяти
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # Кэш памяти в RAM
        self._memories: Dict[str, List[MemoryItem]] = {}

        # Working memory (не сохраняется)
        self._working_memory: Dict[str, List[Dict[str, Any]]] = {}

        # Загружаем существующую память
        self._load_all_memories()

    def _load_all_memories(self):
        """Загрузить всю память из файлов."""
        for file in self.storage_path.glob("*.json"):
            agent_id = file.stem
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._memories[agent_id] = [
                        MemoryItem.from_dict(item) for item in data
                    ]
                logger.debug(f"Loaded {len(self._memories[agent_id])} memories for {agent_id}")
            except Exception as e:
                logger.warning(f"Failed to load memories for {agent_id}: {e}")
                self._memories[agent_id] = []

    def _save_agent_memories(self, agent_id: str):
        """Сохранить память агента в файл."""
        try:
            file_path = self.storage_path / f"{agent_id}.json"

            # Фильтруем working memory (не сохраняем)
            memories_to_save = [
                m.to_dict() for m in self._memories.get(agent_id, [])
                if m.memory_type != MemoryType.WORKING.value
            ]

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(memories_to_save, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"Failed to save memories for {agent_id}: {e}")

    def _generate_memory_id(self, agent_id: str, content: Dict[str, Any]) -> str:
        """Генерация уникального ID для памяти."""
        content_str = json.dumps(content, sort_keys=True, ensure_ascii=False)
        hash_input = f"{agent_id}:{content_str}:{datetime.now().timestamp()}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]

    async def remember(
        self,
        agent_id: str,
        content: Dict[str, Any],
        memory_type: str = "episodic",
        importance: float = 1.0,
        tags: Optional[List[str]] = None,
        source: Optional[str] = None
    ) -> str:
        """
        Запомнить информацию.

        Args:
            agent_id: ID агента
            content: Содержимое для запоминания
            memory_type: Тип памяти (episodic, semantic, procedural)
            importance: Важность (0.0 - 1.0+)
            tags: Теги для поиска
            source: Источник информации

        Returns:
            ID созданного воспоминания
        """
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return ""
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        memory_id = self._generate_memory_id(agent_id, content)

        memory = MemoryItem(
            id=memory_id,
            agent_id=agent_id,
            memory_type=memory_type,
            content=content,
            created_at=datetime.now(timezone.utc).isoformat(),
            importance=importance,
            tags=tags or [],
            source=source
        )

        # Добавляем в кэш
        if agent_id not in self._memories:
            self._memories[agent_id] = []

        self._memories[agent_id].append(memory)

        # Сохраняем (кроме working memory)
        if memory_type != MemoryType.WORKING.value:
            self._save_agent_memories(agent_id)

        logger.debug(f"Agent {agent_id} remembered: {memory_id} ({memory_type})")

        return memory_id

    async def recall(
        self,
        agent_id: str,
        query: Optional[str] = None,
        memory_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10,
        min_importance: float = 0.0
    ) -> List[Dict[str, Any]]:
        """
        Вспомнить информацию.

        Args:
            agent_id: ID агента
            query: Поисковый запрос (простой текстовый поиск)
            memory_type: Фильтр по типу памяти
            tags: Фильтр по тегам
            limit: Максимальное количество
            min_importance: Минимальная важность

        Returns:
            Список воспоминаний
        """
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return []
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        if agent_id not in self._memories:
            return []

        memories = self._memories[agent_id]

        # Фильтрация
        filtered = []
        for m in memories:
            # По типу
            if memory_type and m.memory_type != memory_type:
                continue

            # По важности
            if m.importance < min_importance:
                continue

            # По тегам
            if tags and not any(t in m.tags for t in tags):
                continue

            # По запросу (простой текстовый поиск)
            if query:
                content_str = json.dumps(m.content, ensure_ascii=False).lower()
                if query.lower() not in content_str:
                    continue

            filtered.append(m)

        # Сортируем по важности и дате
        filtered.sort(
            key=lambda x: (x.importance, x.created_at),
            reverse=True
        )

        # Обновляем access_count и last_accessed
        result = []
        for m in filtered[:limit]:
            m.access_count += 1
            m.last_accessed = datetime.now(timezone.utc).isoformat()
            result.append({
                "id": m.id,
                "content": m.content,
                "memory_type": m.memory_type,
                "importance": m.importance,
                "created_at": m.created_at,
                "access_count": m.access_count,
                "tags": m.tags,
            })

        # Сохраняем обновления
        if result:
            self._save_agent_memories(agent_id)

        return result

    async def consolidate(
        self,
        agent_id: str,
        min_access_count: int = 3,
        max_age_days: int = 7
    ) -> Dict[str, Any]:
        """
        Консолидация памяти (episodic → semantic).

        Часто используемые episodic воспоминания обобщаются в semantic.

        Args:
            agent_id: ID агента
            min_access_count: Минимум обращений для консолидации
            max_age_days: Максимальный возраст для консолидации

        Returns:
            Статистика консолидации
        """
        stats = {
            "agent_id": agent_id,
            "episodic_processed": 0,
            "semantic_created": 0,
            "episodic_removed": 0,
        }

        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return stats
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        if agent_id not in self._memories:
            return stats

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        episodic_to_consolidate = []

        for m in self._memories[agent_id]:
            if m.memory_type != MemoryType.EPISODIC.value:
                continue

            stats["episodic_processed"] += 1

            # Проверяем условия консолидации
            if m.access_count >= min_access_count:
                try:
                    created = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
                    if created < cutoff_date:
                        episodic_to_consolidate.append(m)
                except (ValueError, TypeError):
                    pass

        # Группируем похожие воспоминания и создаём semantic
        # TODO: Использовать LLM для обобщения
        if episodic_to_consolidate:
            # Простая группировка по тегам
            tag_groups: Dict[str, List[MemoryItem]] = {}
            for m in episodic_to_consolidate:
                key = ",".join(sorted(m.tags)) if m.tags else "no_tags"
                if key not in tag_groups:
                    tag_groups[key] = []
                tag_groups[key].append(m)

            # Создаём semantic память для каждой группы
            for _tag_key, group in tag_groups.items():
                if len(group) >= 2:  # Минимум 2 для обобщения
                    semantic_content = {
                        "summary": f"Обобщение {len(group)} событий",
                        "source_count": len(group),
                        "common_patterns": [m.content for m in group[:3]],
                        "tags": list(set(t for m in group for t in m.tags)),
                    }

                    await self.remember(
                        agent_id=agent_id,
                        content=semantic_content,
                        memory_type=MemoryType.SEMANTIC.value,
                        importance=max(m.importance for m in group),
                        tags=semantic_content["tags"],
                        source="consolidation"
                    )
                    stats["semantic_created"] += 1

                    # Удаляем консолидированные episodic
                    for m in group:
                        self._memories[agent_id].remove(m)
                        stats["episodic_removed"] += 1

            self._save_agent_memories(agent_id)

        logger.info(f"Consolidated agent {agent_id}: {stats}")

        return stats

    async def apply_decay(
        self,
        agent_id: Optional[str] = None,
        decay_rate: float = 0.01,
        min_importance: float = 0.1
    ) -> Dict[str, Any]:
        """
        Применить затухание к памяти.

        Args:
            agent_id: ID агента (None = все агенты)
            decay_rate: Скорость затухания
            min_importance: Минимальная важность (ниже = удаление)

        Returns:
            Статистика затухания
        """
        stats = {
            "memories_processed": 0,
            "memories_decayed": 0,
            "memories_removed": 0,
        }

        agents = [agent_id] if agent_id else list(self._memories.keys())

        for aid in agents:
            if aid not in self._memories:
                continue

            to_remove = []

            for m in self._memories[aid]:
                stats["memories_processed"] += 1

                # Применяем затухание
                new_importance = m.importance * (1 - decay_rate)

                if new_importance < min_importance:
                    to_remove.append(m)
                    stats["memories_removed"] += 1
                else:
                    m.importance = new_importance
                    stats["memories_decayed"] += 1

            # Удаляем слабые воспоминания
            for m in to_remove:
                self._memories[aid].remove(m)

            self._save_agent_memories(aid)

        logger.info(f"Memory decay applied: {stats}")

        return stats

    def get_working_memory(self, agent_id: str) -> List[Dict[str, Any]]:
        """Получить working memory агента."""
        return self._working_memory.get(agent_id, [])

    def set_working_memory(self, agent_id: str, items: List[Dict[str, Any]]):
        """Установить working memory агента."""
        self._working_memory[agent_id] = items

    def clear_working_memory(self, agent_id: str):
        """Очистить working memory агента."""
        self._working_memory[agent_id] = []

    def get_stats(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Получить статистику памяти."""
        if agent_id:
            memories = self._memories.get(agent_id, [])
            return {
                "agent_id": agent_id,
                "total_memories": len(memories),
                "by_type": {
                    mt.value: len([m for m in memories if m.memory_type == mt.value])
                    for mt in MemoryType
                },
                "working_memory_size": len(self._working_memory.get(agent_id, [])),
            }
        else:
            return {
                "total_agents": len(self._memories),
                "total_memories": sum(len(m) for m in self._memories.values()),
                "by_agent": {
                    aid: len(memories) for aid, memories in self._memories.items()
                }
            }


# Singleton
_memory_manager_instance: Optional[AgentMemoryManager] = None


def get_agent_memory_manager(storage_path: str = "data/agent_memories") -> AgentMemoryManager:
    """Получить экземпляр AgentMemoryManager."""
    global _memory_manager_instance
    if _memory_manager_instance is None:
        _memory_manager_instance = AgentMemoryManager(storage_path)
    return _memory_manager_instance


