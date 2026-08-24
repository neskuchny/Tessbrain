# -*- coding: utf-8 -*-
"""
Security Filter - Фильтрация конфиденциальных данных.

Автоматически обнаруживает и маскирует чувствительную информацию
перед передачей внешним подрядчикам.
"""
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DataClassification(Enum):
    """Уровни классификации данных."""
    PUBLIC = "public"           # Публичная информация
    GENERAL = "general"         # Общая информация
    INTERNAL = "internal"       # Внутренняя информация
    CONFIDENTIAL = "confidential"  # Конфиденциальная
    SECRET = "secret"           # Секретная
    RESTRICTED = "restricted"   # Ограниченная


class ContractorType(Enum):
    """Типы подрядчиков."""
    INVESTOR = "investor"
    MARKETING = "marketing"
    AUDITOR = "auditor"
    HR_PARTNER = "hr_partner"
    LEGAL = "legal"
    CONSULTANT = "consultant"


@dataclass
class FilterResult:
    """Результат фильтрации."""
    original_text: str
    filtered_text: str
    masked_items: List[Dict[str, Any]] = field(default_factory=list)
    blocked_categories: List[str] = field(default_factory=list)
    classification: DataClassification = DataClassification.GENERAL


class SecurityFilter:
    """
    Фильтр безопасности для внешнего доступа.

    Функции:
    1. Обнаружение чувствительных данных по паттернам
    2. Маскирование персональных данных
    3. Блокировка запрещённых категорий
    4. Классификация уровня секретности
    """

    # Паттерны для обнаружения чувствительных данных
    SENSITIVE_PATTERNS = {
        "salary": [
            r"\b(зарплат|оклад|ставк|премия|бонус|компенсаци)\w*\b",
            r"\b(salary|wage|bonus|compensation)\w*\b",
            r"\d+[\s,]*\d*\s*(руб|₽|rub|\$|usd|eur|€)",
        ],
        "personal": [
            r"\b(паспорт|инн|снилс|адрес проживания)\b",
            r"\b(passport|ssn|social security)\b",
            r"\b\d{3}[-\s]?\d{3}[-\s]?\d{4}\b",  # Телефон
        ],
        "financial": [
            r"\b(выручк|прибыл|убыт|баланс|счёт|бюджет)\w*\s*[:=]?\s*\d+",
            r"\b(revenue|profit|loss|balance|budget)\w*\s*[:=]?\s*\d+",
            r"(расход|доход)\w*\s*[:=]?\s*\d+",
        ],
        "contract": [
            r"\b(договор|контракт)\s*№?\s*[\w-]+",
            r"\b(contract|agreement)\s*#?\s*[\w-]+",
            r"NDA|соглашение о неразглашении",
        ],
        "credentials": [
            r"\b(пароль|password|api.?key|token|secret)\b",
            r"[a-zA-Z0-9]{32,}",  # Длинные ключи
            r"Bearer\s+[a-zA-Z0-9._-]+",
        ],
        "internal_conflict": [
            r"\b(конфликт|увольнен|уволить|жалоба|выговор)\w*\b",
            r"\b(conflict|fired|complaint|warning)\w*\b",
        ],
    }

    # Маски для замены
    MASKS = {
        "phone": "[ТЕЛЕФОН СКРЫТ]",
        "email": "[EMAIL СКРЫТ]",
        "amount": "[СУММА СКРЫТА]",
        "name": "[ИМЯ СКРЫТО]",
        "contract": "[КОНТРАКТ СКРЫТ]",
        "credential": "[ДАННЫЕ СКРЫТЫ]",
        "personal": "[ПЕРСОНАЛЬНЫЕ ДАННЫЕ СКРЫТЫ]",
    }

    # Разрешённые категории по типам подрядчиков
    ALLOWED_CATEGORIES = {
        ContractorType.INVESTOR: {
            "company_overview", "project_progress", "team_overview",
            "risks_overview", "achievements", "roadmap", "metrics"
        },
        ContractorType.MARKETING: {
            "product_description", "unique_selling_points", "target_audience",
            "competitor_analysis", "pricing_public", "brand_guidelines",
            "success_stories", "testimonials"
        },
        ContractorType.AUDITOR: {
            "business_processes", "decisions", "regulations",
            "team_structure", "compliance", "metrics"
        },
        ContractorType.HR_PARTNER: {
            "employee_role", "employee_projects", "employee_skills",
            "employee_achievements"  # Только с согласия
        },
        ContractorType.LEGAL: {
            "contracts_overview", "regulations", "compliance",
            "risks_legal", "decisions"
        },
        ContractorType.CONSULTANT: {
            "company_overview", "processes", "team_structure"
        },
    }

    # Запрещённые ключевые слова по типам
    BLOCKED_KEYWORDS = {
        ContractorType.INVESTOR: [
            "зарплата", "оклад", "конфликт", "увольнение", "жалоба",
            "пароль", "токен", "ключ доступа"
        ],
        ContractorType.MARKETING: [
            "прибыль", "убыток", "выручка", "бюджет", "зарплата",
            "конфликт", "увольнение", "код", "архитектура"
        ],
        ContractorType.AUDITOR: [
            "зарплата", "личные данные", "паспорт", "инн"
        ],
        ContractorType.HR_PARTNER: [
            "зарплата", "медицинский", "конфликт", "жалоба"
        ],
    }

    def __init__(self):
        """Инициализация фильтра."""
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}
        self._compile_patterns()

    def _compile_patterns(self):
        """Компиляция регулярных выражений."""
        for category, patterns in self.SENSITIVE_PATTERNS.items():
            self._compiled_patterns[category] = [
                re.compile(p, re.IGNORECASE | re.UNICODE)
                for p in patterns
            ]

    def filter_data(
        self,
        data: Dict[str, Any],
        contractor_type: ContractorType,
        consent_given: bool = False
    ) -> Dict[str, Any]:
        """
        Фильтрация данных для подрядчика.

        Args:
            data: Исходные данные
            contractor_type: Тип подрядчика
            consent_given: Есть ли согласие на передачу персональных данных

        Returns:
            Отфильтрованные данные
        """
        filtered = {}

        for key, value in data.items():
            # Проверяем категорию
            if not self._is_category_allowed(key, contractor_type):
                logger.info(f"Blocked category: {key} for {contractor_type.value}")
                continue

            # Фильтруем значение
            if isinstance(value, str):
                filtered[key] = self._filter_text(
                    value, contractor_type, consent_given
                )
            elif isinstance(value, dict):
                filtered[key] = self.filter_data(
                    value, contractor_type, consent_given
                )
            elif isinstance(value, list):
                filtered[key] = [
                    self._filter_item(item, contractor_type, consent_given)
                    for item in value
                ]
            else:
                filtered[key] = value

        return filtered

    def filter_text(
        self,
        text: str,
        contractor_type: ContractorType,
        consent_given: bool = False
    ) -> str:
        """Публичная маскировка готового текста.

        Нужна там, где наружу уходит не структура, а связный ответ (например,
        ответ мозга подрядчику): модель видит только уже отфильтрованные
        данные, но текст всё равно прогоняется через те же паттерны — чтобы
        пересказ не вернул то, что фильтр вырезал."""
        return self._filter_text(text or "", contractor_type, consent_given)

    def _filter_text(
        self,
        text: str,
        contractor_type: ContractorType,
        consent_given: bool
    ) -> str:
        """Фильтрация текста."""
        result = text

        # Проверяем запрещённые ключевые слова
        blocked = self.BLOCKED_KEYWORDS.get(contractor_type, [])
        for keyword in blocked:
            if keyword.lower() in result.lower():
                # Маскируем предложение с ключевым словом
                sentences = result.split('.')
                result = '. '.join([
                    s if keyword.lower() not in s.lower()
                    else "[ИНФОРМАЦИЯ СКРЫТА]"
                    for s in sentences
                ])

        # Применяем паттерны
        for category, patterns in self._compiled_patterns.items():
            # Пропускаем персональные данные если есть согласие
            if category == "personal" and consent_given:
                continue

            for pattern in patterns:
                result = pattern.sub(self._get_mask(category), result)

        return result

    def _filter_item(
        self,
        item: Any,
        contractor_type: ContractorType,
        consent_given: bool
    ) -> Any:
        """Фильтрация элемента списка."""
        if isinstance(item, str):
            return self._filter_text(item, contractor_type, consent_given)
        elif isinstance(item, dict):
            return self.filter_data(item, contractor_type, consent_given)
        return item

    def _is_category_allowed(
        self,
        category: str,
        contractor_type: ContractorType
    ) -> bool:
        """Проверка разрешена ли категория."""
        allowed = self.ALLOWED_CATEGORIES.get(contractor_type, set())

        # Если категория явно в списке - разрешено
        if category in allowed:
            return True

        # Проверяем частичное совпадение
        for allowed_cat in allowed:
            if allowed_cat in category or category in allowed_cat:
                return True

        # По умолчанию разрешаем общие категории
        general_categories = {
            "name", "title", "description", "summary", "overview",
            "status", "created_at", "updated_at"
        }
        return category in general_categories

    def _get_mask(self, category: str) -> str:
        """Получить маску для категории."""
        mask_map = {
            "salary": self.MASKS["amount"],
            "personal": self.MASKS["personal"],
            "financial": self.MASKS["amount"],
            "contract": self.MASKS["contract"],
            "credentials": self.MASKS["credential"],
            "internal_conflict": "[ИНФОРМАЦИЯ СКРЫТА]",
        }
        return mask_map.get(category, "[СКРЫТО]")

    def classify_data(self, text: str) -> DataClassification:
        """
        Классификация уровня секретности текста.

        Args:
            text: Текст для классификации

        Returns:
            Уровень классификации
        """
        text_lower = text.lower()

        # Проверяем на секретные данные
        secret_keywords = ["пароль", "password", "api key", "token", "secret"]
        if any(kw in text_lower for kw in secret_keywords):
            return DataClassification.SECRET

        # Проверяем на конфиденциальные
        confidential_keywords = ["зарплата", "прибыль", "убыток", "бюджет", "контракт"]
        if any(kw in text_lower for kw in confidential_keywords):
            return DataClassification.CONFIDENTIAL

        # Проверяем на внутренние
        internal_keywords = ["внутренний", "internal", "процесс", "решение"]
        if any(kw in text_lower for kw in internal_keywords):
            return DataClassification.INTERNAL

        # Проверяем на общие
        general_keywords = ["продукт", "команда", "проект", "описание"]
        if any(kw in text_lower for kw in general_keywords):
            return DataClassification.GENERAL

        return DataClassification.PUBLIC

    def get_filter_stats(
        self,
        original: Dict[str, Any],
        filtered: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Получить статистику фильтрации."""
        def count_items(d: Dict) -> int:
            count = 0
            for v in d.values():
                if isinstance(v, dict):
                    count += count_items(v)
                elif isinstance(v, list):
                    count += len(v)
                else:
                    count += 1
            return count

        original_count = count_items(original)
        filtered_count = count_items(filtered)

        return {
            "original_items": original_count,
            "filtered_items": filtered_count,
            "removed_items": original_count - filtered_count,
            "filter_rate": round((original_count - filtered_count) / max(original_count, 1) * 100, 2)
        }


# Singleton
_security_filter: Optional[SecurityFilter] = None


def get_security_filter() -> SecurityFilter:
    """Получить экземпляр SecurityFilter."""
    global _security_filter
    if _security_filter is None:
        _security_filter = SecurityFilter()
    return _security_filter


