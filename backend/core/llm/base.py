# -*- coding: utf-8 -*-
"""
Базовый класс для LLM клиентов
"""
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class BaseLLMClient(ABC):
    """
    Абстрактный базовый класс для LLM клиентов.
    Поддерживает Gemini, OpenAI и другие провайдеры.
    """

    def __init__(self, model: str, api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key
        self.enabled = False

        # Статистика использования
        self.stats = {
            "total_requests": 0,
            "cached_requests": 0,
            "total_input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0.0,
            "estimated_cost_without_cache": 0.0,
            "errors": 0
        }

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        **kwargs
    ) -> str:
        """Генерация текста"""
        pass

    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        **kwargs
    ) -> Union[Dict[str, Any], List[Any]]:
        """Генерация JSON"""
        pass

    def extract_json(self, text: str) -> Optional[Union[Dict[str, Any], List[Any]]]:
        """
        Извлечение JSON из текста.
        Поддерживает различные форматы: чистый JSON, markdown code blocks и т.д.
        """
        if not text:
            return None

        # Убираем markdown code blocks
        cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        # Ищем начало и конец JSON
        start_idx = -1
        end_idx = -1

        for i, char in enumerate(cleaned):
            if char in ['{', '['] and start_idx == -1:
                start_idx = i
            if char in ['}', ']']:
                end_idx = i

        if start_idx != -1 and end_idx != -1:
            cleaned = cleaned[start_idx:end_idx + 1]

        # Попытка 1: прямой парсинг
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.debug("suppressed exception", exc_info=True)

        # Попытка 2: поиск JSON через regex
        patterns = [
            r'\{[\s\S]*\}',  # Объект
            r'\[[\s\S]*\]',  # Массив
        ]

        for pattern in patterns:
            match = re.search(pattern, cleaned)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue

        # Попытка 3: исправление распространённых проблем
        try:
            # Заменяем одинарные кавычки на двойные
            fixed = cleaned.replace("'", '"')
            # Убираем trailing commas
            fixed = re.sub(r',(\s*[}\]])', r'\1', fixed)
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.debug("suppressed exception", exc_info=True)

        # Попытка 4: ремонт ОБРЕЗАННОГО ответа (max_tokens оборвал JSON на
        # полуслове — раньше терялось ВСЁ, включая валидные ранние элементы).
        # Сканируем баланс скобок с учётом строк: берём самый длинный
        # префикс, обрезаем незакрытый хвост до последней завершённой
        # структуры и дозакрываем скобки.
        repaired = self._repair_truncated_json(cleaned)
        if repaired is not None:
            logger.warning("extract_json: ответ был обрезан — восстановлен "
                           "валидный префикс (часть хвоста потеряна)")
            return repaired

        return None

    @staticmethod
    def _repair_truncated_json(text: str):
        """Восстановить префикс обрезанного JSON. None — не получилось."""
        start = -1
        for i, ch in enumerate(text):
            if ch in "{[":
                start = i
                break
        if start == -1:
            return None
        stack = []
        in_str = False
        esc = False
        last_complete = -1  # индекс последнего закрытия какой-либо структуры
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or stack[-1] != ch:
                    return None  # рассинхрон скобок — не рискуем
                stack.pop()
                last_complete = i  # любая закрывшаяся структура — точка отката
                if not stack:
                    # полный корень закрылся — валидный префикс целиком
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        # корень не закрылся (обрезано): режем до последней завершённой
        # структуры и дозакрываем стек
        if last_complete == -1 or not stack:
            return None
        candidate = text[start:last_complete + 1].rstrip().rstrip(",")
        # пересчитать стек для candidate (мы обрезали хвост)
        stack2 = []
        in_str = False
        esc = False
        for ch in candidate:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack2.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack2 and stack2[-1] == ch:
                    stack2.pop()
        try:
            return json.loads(candidate + "".join(reversed(stack2)))
        except json.JSONDecodeError:
            return None

    def estimate_tokens(self, text: str) -> int:
        """
        Оценка количества токенов.
        Для русского текста: ~2-3 символа на токен
        """
        return len(text) // 2

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику использования"""
        cache_hit_rate = (
            self.stats["cached_requests"] / self.stats["total_requests"] * 100
            if self.stats["total_requests"] > 0 else 0
        )

        savings = (
            self.stats["estimated_cost_without_cache"] - self.stats["estimated_cost"]
        )

        savings_percent = (
            savings / self.stats["estimated_cost_without_cache"] * 100
            if self.stats["estimated_cost_without_cache"] > 0 else 0
        )

        return {
            "model": self.model,
            "enabled": self.enabled,
            "requests": {
                "total": self.stats["total_requests"],
                "cached": self.stats["cached_requests"],
                "errors": self.stats["errors"],
                "cache_hit_rate": f"{cache_hit_rate:.1f}%"
            },
            "tokens": {
                "input": self.stats["total_input_tokens"],
                "cached": self.stats["cached_input_tokens"],
                "output": self.stats["output_tokens"],
                "total": (
                    self.stats["total_input_tokens"] +
                    self.stats["cached_input_tokens"] +
                    self.stats["output_tokens"]
                )
            },
            "cost": {
                "actual": f"${self.stats['estimated_cost']:.6f}",
                "without_cache": f"${self.stats['estimated_cost_without_cache']:.6f}",
                "savings": f"${savings:.6f}",
                "savings_percent": f"{savings_percent:.1f}%"
            }
        }

    def reset_stats(self):
        """Сброс статистики"""
        self.stats = {
            "total_requests": 0,
            "cached_requests": 0,
            "total_input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0.0,
            "estimated_cost_without_cache": 0.0,
            "errors": 0
        }



