# -*- coding: utf-8 -*-
"""
Session Memory for Auto/Tess — cross-query память.

Проблема: "Покажи задачи" → (ответ) → "Отправь это в тг" — система НЕ помнит предыдущий результат.
Решение: AutoSession хранит последние N результатов и обогащает новые запросы контекстом.

Usage:
    session_store = AutoSessionStore()
    session = session_store.get_or_create(session_id, user_id)

    # После выполнения инструмента:
    session.add_turn(user_message, tool_name, tool_args, observation)

    # При новом запросе:
    enriched = session.enrich_message("отправь это в тг")
    # -> "Отправь в Telegram результат предыдущего запроса (get_all_tasks): [данные]"
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SessionTurn:
    """Один шаг диалога: запрос → инструмент → результат."""
    user_message: str
    tool_name: str
    tool_args: Dict[str, Any]
    observation: str           # Результат выполнения инструмента
    pretty_result: str = ""    # Отформатированный результат (если есть)
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class AutoSession:
    """Сессия Auto/Tess с памятью между запросами."""
    session_id: str
    user_id: str
    turns: List[SessionTurn] = field(default_factory=list)
    max_turns: int = 10        # Хранить последние N шагов
    created_at: float = 0.0
    last_active: float = 0.0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.last_active:
            self.last_active = now

    def add_turn(
        self,
        user_message: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        observation: str,
        pretty_result: str = "",
    ) -> None:
        """Добавить шаг в историю."""
        turn = SessionTurn(
            user_message=user_message,
            tool_name=tool_name,
            tool_args=tool_args,
            observation=observation,
            pretty_result=pretty_result,
        )
        self.turns.append(turn)
        self.last_active = time.time()

        # Ограничиваем историю
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

    @property
    def last_turn(self) -> Optional[SessionTurn]:
        """Последний шаг."""
        return self.turns[-1] if self.turns else None

    @property
    def last_observation(self) -> str:
        """Последний результат."""
        return self.turns[-1].observation if self.turns else ""

    @property
    def last_tool(self) -> str:
        """Последний использованный инструмент."""
        return self.turns[-1].tool_name if self.turns else ""

    def enrich_message(self, message: str) -> Tuple[str, bool]:
        """
        Обогатить сообщение контекстом из предыдущих шагов.

        Возвращает (enriched_message, was_enriched).

        Детектируемые паттерны:
        - "отправь это в тг" → подставить последний результат
        - "а теперь создай задачу по этому" → взять контекст из последнего observation
        - "подробнее о первом" → взять контекст из списка
        """
        if not self.turns:
            return message, False

        msg_lower = message.lower().strip()
        last = self.last_turn

        # --- Паттерн 1: "это/эти/результат/this" — ссылка на предыдущий результат ---
        reference_patterns = [
            # Русский
            r"отправь это", r"пошли это", r"скинь это", r"кинь это",
            r"отправь результат", r"пошли результат",
            r"отправь эти", r"пошли эти",
            r"и отправь$",  # "покажи задачи и отправь"
            # English
            r"send this", r"forward this", r"share this", r"post this",
            r"send the result", r"send results", r"share results",
            r"send these", r"forward these",
            r"send it to", r"share it",
            r"and send$",  # "show tasks and send"
        ]

        for pattern in reference_patterns:
            if re.search(pattern, msg_lower):
                # Подставляем последний результат
                last_data = last.pretty_result or last.observation
                # Ограничиваем размер для отправки
                if len(last_data) > 3000:
                    last_data = last_data[:3000] + "\n... (обрезано)"

                # Обогащаем сообщение: заменяем "это" на данные
                enriched = f"{message}\n\n[Контекст из предыдущего запроса ({last.tool_name}):\n{last_data}]"
                logger.info(
                    f"[SessionMemory] Enriched '{msg_lower[:40]}...' with last observation "
                    f"from {last.tool_name} ({len(last_data)} chars)"
                )
                return enriched, True

        # --- Паттерн 2: "по этому/на основе/based on" — контекст для создания ---
        context_patterns = [
            # Русский
            r"по этому", r"на основе этого", r"из этого",
            r"по этим данным", r"из предыдущ", r"на основе предыдущ",
            # English
            r"based on this", r"from this", r"using this",
            r"based on the previous", r"from the previous", r"from above",
            r"with this data", r"using these",
        ]

        for pattern in context_patterns:
            if re.search(pattern, msg_lower):
                last_data = last.pretty_result or last.observation
                if len(last_data) > 2000:
                    last_data = last_data[:2000] + "\n..."

                enriched = f"{message}\n\n[Контекст из предыдущего ответа ({last.tool_name}):\n{last_data}]"
                logger.info(f"[SessionMemory] Added context for '{msg_lower[:40]}...'")
                return enriched, True

        # --- Паттерн 3: Короткие ответы без явного контекста ---
        # Если предыдущий запрос был, и текущий очень короткий / не самостоятельный
        short_follow_ups = [
            # Русский
            "а в тг", "в тг", "в телеграм", "и в тг", "и в телеграм",
            "анализ", "подробнее", "больше деталей", "детальнее",
            # English
            "to telegram", "to tg", "to slack", "to email",
            "in telegram", "in tg",
            "analyze", "more details", "elaborate", "more info",
            "details", "expand",
        ]

        if msg_lower in short_follow_ups or (len(msg_lower) < 20 and any(p in msg_lower for p in short_follow_ups)):
            last_data = last.pretty_result or last.observation
            if len(last_data) > 3000:
                last_data = last_data[:3000] + "\n..."

            enriched = f"{message}\n\n[Данные из предыдущего шага ({last.tool_name}):\n{last_data}]"
            logger.info(f"[SessionMemory] Short follow-up enriched: '{msg_lower}'")
            return enriched, True

        return message, False

    def symbolic_view(self, symbol_chars: int = 80) -> str:
        """Компактный СИМВОЛИЧЕСКИЙ вид истории с drill-down (аддитивно).

        Перенос идеи TencentDB-Agent-Memory: свернуть многословные observation'ы
        в одну строку-символ на шаг с node_id, чтобы длинные сессии не раздували
        контекст; к деталям — через node_id (см. symbolic_memory.expand/trace).
        Отдельный обзор — НЕ трогает enrich_message/get_context_summary."""
        from backend.core.memory.symbolic_memory import SymbolicMemory
        sm = SymbolicMemory(symbol_chars=symbol_chars)
        for i, t in enumerate(self.turns):
            raw = t.pretty_result or t.observation or ""
            label = f"{t.tool_name or 'шаг'} · {(t.user_message or '')[:48]}"
            sm.add(raw, label=label, meta={"turn": i, "tool": t.tool_name})
        return sm.render()

    def get_context_summary(self, max_chars: int = 500) -> str:
        """Краткое описание контекста сессии для system prompt."""
        if not self.turns:
            return ""

        lines = []
        for turn in self.turns[-3:]:  # Последние 3 шага
            obs_short = turn.observation[:100] + "..." if len(turn.observation) > 100 else turn.observation
            lines.append(f"- User: {turn.user_message[:80]}... → {turn.tool_name} → {obs_short}")

        return "Previous conversation:\n" + "\n".join(lines)


class AutoSessionStore:
    """
    In-memory хранилище сессий Auto/Tess.

    Thread-safe. Автоматически удаляет старые сессии (TTL: 30 минут).
    """

    TTL_SECONDS = 30 * 60  # 30 минут
    MAX_SESSIONS = 1000    # Максимум одновременных сессий

    def __init__(self):
        self._sessions: OrderedDict[str, AutoSession] = OrderedDict()
        self._lock = Lock()

    def get_or_create(self, session_id: str, user_id: str = "") -> AutoSession:
        """Получить или создать сессию."""
        with self._lock:
            self._cleanup_expired()

            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_active = time.time()
                # Move to end (LRU)
                self._sessions.move_to_end(session_id)
                return session

            session = AutoSession(session_id=session_id, user_id=user_id)
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> Optional[AutoSession]:
        """Получить сессию (None если не существует)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_active = time.time()
            return session

    def _cleanup_expired(self) -> None:
        """Удалить старые сессии."""
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_active > self.TTL_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]

        # Если всё ещё слишком много — удалить самые старые
        while len(self._sessions) > self.MAX_SESSIONS:
            self._sessions.popitem(last=False)


# === Глобальный экземпляр ===
_global_session_store: Optional[AutoSessionStore] = None


def get_auto_session_store() -> AutoSessionStore:
    """Получить глобальный session store (singleton)."""
    global _global_session_store
    if _global_session_store is None:
        _global_session_store = AutoSessionStore()
    return _global_session_store
