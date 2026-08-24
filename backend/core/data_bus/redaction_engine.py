# -*- coding: utf-8 -*-
"""
Redaction Engine — Фильтрация и маскирование данных по SharingPolicy.

Три уровня фильтрации:
1. Node Type Filter — убираем запрещённые типы узлов
2. Field Filter — оставляем только разрешённые поля внутри узлов
3. Pattern Redaction — заменяем по regex (суммы, телефоны, email)
"""
import logging
import re
from typing import Any, Dict, List, Tuple

from .models import SharingPolicy

logger = logging.getLogger(__name__)


class RedactionEngine:
    """
    Движок редактирования (маскирования) конфиденциальных данных.

    Применяет SharingPolicy к результатам поиска:
    - Убирает узлы запрещённых типов
    - Оставляет только разрешённые поля
    - Заменяет чувствительные данные по regex-паттернам
    """

    def __init__(self):
        """Инициализация движка."""
        self._compiled_patterns: Dict[str, List[Tuple[re.Pattern, str]]] = {}

    def _compile_patterns(self, policy: SharingPolicy) -> List[Tuple[re.Pattern, str]]:
        """Скомпилировать regex паттерны из политики (с кешированием)."""
        policy_id = policy.id
        if policy_id not in self._compiled_patterns:
            compiled = []
            for pat_config in policy.redaction_patterns:
                try:
                    pattern = re.compile(pat_config["pattern"], re.IGNORECASE | re.UNICODE)
                    replacement = pat_config.get("replace", "[СКРЫТО]")
                    compiled.append((pattern, replacement))
                except re.error as e:
                    logger.warning(f"Invalid regex pattern '{pat_config.get('pattern')}': {e}")
            self._compiled_patterns[policy_id] = compiled
        return self._compiled_patterns[policy_id]

    def redact_results(
        self,
        results: List[Dict[str, Any]],
        policy: SharingPolicy,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Полный pipeline фильтрации и редактирования результатов.

        Args:
            results: Список результатов поиска (узлы графа или объекты)
            policy: Политика доступа

        Returns:
            (отфильтрованные_результаты, количество_скрытых_элементов)
        """
        redacted_count = 0
        filtered_results = []
        patterns = self._compile_patterns(policy)

        for item in results:
            # 1. Фильтр по типу узла
            node_type = item.get("type") or item.get("node_type") or item.get("entity_type", "")

            if not self._is_type_allowed(node_type, policy):
                redacted_count += 1
                continue

            # 2. Фильтр по access_level
            item_access_level = item.get("access_level", 1)
            if isinstance(item_access_level, (int, float)) and item_access_level > policy.max_access_level:
                redacted_count += 1
                continue

            # 3. Фильтр полей
            filtered_item = self._filter_fields(item, node_type, policy)

            # 4. Применить regex-паттерны к текстовым полям
            redacted_item, item_redactions = self._apply_patterns(filtered_item, patterns)
            redacted_count += item_redactions

            filtered_results.append(redacted_item)

        # 5. Ограничить количество результатов
        if len(filtered_results) > policy.max_results_per_query:
            redacted_count += len(filtered_results) - policy.max_results_per_query
            filtered_results = filtered_results[:policy.max_results_per_query]

        return filtered_results, redacted_count

    def redact_text(self, text: str, policy: SharingPolicy) -> Tuple[str, int]:
        """
        Редактировать текст по паттернам политики.

        Args:
            text: Исходный текст
            policy: Политика доступа

        Returns:
            (отредактированный_текст, количество_замен)
        """
        if not text:
            return text, 0

        patterns = self._compile_patterns(policy)
        result = text
        total_replacements = 0

        for pattern, replacement in patterns:
            new_result, count = pattern.subn(replacement, result)
            total_replacements += count
            result = new_result

        return result, total_replacements

    def redact_snapshot(
        self,
        snapshot: Dict[str, Any],
        policy: SharingPolicy,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Редактировать снэпшот сущности.

        Args:
            snapshot: Снэпшот (иерархическая структура)
            policy: Политика доступа

        Returns:
            (отредактированный_снэпшот, количество_скрытых_элементов)
        """
        patterns = self._compile_patterns(policy)
        redacted, count = self._apply_patterns(snapshot, patterns)
        return redacted, count

    # ──────────────────────── Приватные методы ────────────────────────

    def _is_type_allowed(self, node_type: str, policy: SharingPolicy) -> bool:
        """Проверить, разрешён ли тип узла."""
        if not node_type:
            return True

        # Если "*" в allowed — разрешены все, кроме blocked
        if "*" in policy.allowed_node_types:
            return node_type not in policy.blocked_node_types

        # Иначе — только явно разрешённые
        if policy.allowed_node_types and node_type not in policy.allowed_node_types:
            return False

        # И не в заблокированных
        if node_type in policy.blocked_node_types:
            return False

        return True

    def _filter_fields(
        self,
        item: Dict[str, Any],
        node_type: str,
        policy: SharingPolicy,
    ) -> Dict[str, Any]:
        """Оставить только разрешённые поля для типа узла."""
        if not policy.field_filters:
            return item

        allowed_fields = policy.field_filters.get(node_type)
        if not allowed_fields:
            return item

        # Всегда оставляем служебные поля
        always_keep = {"id", "type", "node_type", "entity_type", "created_at", "updated_at"}
        allowed_set = set(allowed_fields) | always_keep

        filtered = {}
        for key, value in item.items():
            if key in allowed_set:
                filtered[key] = value

        return filtered

    def _apply_patterns(
        self,
        data: Any,
        patterns: List[Tuple[re.Pattern, str]],
    ) -> Tuple[Any, int]:
        """Рекурсивно применить regex-паттерны ко всем строковым полям."""
        if not patterns:
            return data, 0

        total_count = 0

        if isinstance(data, str):
            result = data
            for pattern, replacement in patterns:
                new_result, count = pattern.subn(replacement, result)
                total_count += count
                result = new_result
            return result, total_count

        elif isinstance(data, dict):
            result = {}
            for key, value in data.items():
                redacted_value, count = self._apply_patterns(value, patterns)
                total_count += count
                result[key] = redacted_value
            return result, total_count

        elif isinstance(data, list):
            result = []
            for item in data:
                redacted_item, count = self._apply_patterns(item, patterns)
                total_count += count
                result.append(redacted_item)
            return result, total_count

        else:
            return data, 0
