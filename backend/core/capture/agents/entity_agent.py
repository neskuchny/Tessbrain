# -*- coding: utf-8 -*-
"""
Универсальный агент для извлечения различных сущностей.
Объединяет извлечение проектов, организаций, технологий, документов и событий.

ВАЖНО: Каждая сущность ОБЯЗАТЕЛЬНО получает entity_type — это ключевое поле
для построения графа знаний. Без типа сущность бесполезна.
"""
import logging
import uuid
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent

logger = logging.getLogger(__name__)

# Валидные типы сущностей и их алиасы для нормализации
VALID_ENTITY_TYPES = [
    "project", "product", "person", "team", "department",
    "organization", "technology", "feature", "document",
    "event", "idea", "concept", "company",
]

# Маппинг алиасов → каноничный тип
ENTITY_TYPE_ALIASES = {
    "tool": "technology",
    "framework": "technology",
    "platform": "technology",
    "software": "technology",
    "language": "technology",
    "library": "technology",
    "service": "product",
    "app": "product",
    "application": "product",
    "startup": "company",
    "firm": "company",
    "corp": "company",
    "corporation": "company",
    "partner": "organization",
    "client": "organization",
    "customer": "organization",
    "competitor": "organization",
    "vendor": "organization",
    "employee": "person",
    "founder": "person",
    "ceo": "person",
    "manager": "person",
    "lead": "person",
    "developer": "person",
    "designer": "person",
    "squad": "team",
    "group": "team",
    "unit": "department",
    "division": "department",
    "module": "feature",
    "functionality": "feature",
    "capability": "feature",
    "milestone": "event",
    "deadline": "event",
    "release": "event",
    "launch": "event",
    "report": "document",
    "spec": "document",
    "specification": "document",
    "contract": "document",
    "presentation": "document",
    "proposal": "idea",
    "suggestion": "idea",
    "initiative": "project",
    "program": "project",
    "workstream": "project",
}


class EntityAgent(BaseExtractionAgent):
    """
    Универсальный агент извлечения сущностей.

    Извлекает:
    - Проекты и продукты
    - Людей и команды
    - Организации и компании
    - Технологии и инструменты
    - Фичи и концепции
    - Документы и артефакты
    - События и даты
    - Идеи и предложения
    """

    def get_entity_type(self) -> str:
        return "entity"

    def get_instruction(self) -> str:
        return '''Ты — агент извлечения и классификации сущностей для базы знаний компании.

Твоя задача — извлечь ВСЕ значимые сущности из транскрипта и классифицировать каждую.

═══════════════════════════════════════════════════
ГЛАВНОЕ ПРАВИЛО — ТОЛЬКО ФАКТЫ ИЗ ТРАНСКРИПТА:
- Извлекай ТОЛЬКО то, что реально упомянуто в транскрипте. Ничего не выдумывай.
- name — дословно как в тексте (не переименовывай, не «улучшай», не переводи).
- Заполняй атрибут ТОЛЬКО если его значение прямо есть в тексте. Нет данных —
  пропусти поле, не ставь заглушку и не угадывай (никаких "TBD"/"—"/выдуманных
  цифр). Пустой атрибут хуже отсутствующего.
- Никакие примеры из этой инструкции НЕ являются данными транскрипта. Не
  переноси их в вывод.
- confidence — честная оценка: 0.9+ сущность названа прямо и однозначно; 0.6–0.8
  упомянута, но тип/детали выводятся косвенно; <0.6 предположение из контекста.

КАЖДАЯ сущность ОБЯЗАНА иметь поле "entity_type" (не пустое, не «дефолтное»).
Если не уверен в типе — выбери БЛИЖАЙШИЙ из списка ниже (но сам факт сущности
должен быть в тексте).
═══════════════════════════════════════════════════

ТИПЫ СУЩНОСТЕЙ (выбирай СТРОГО из этого списка):

1. **project** — Проект, инициатива, рабочий поток (workstream)
   Примеры: "Проект Альфа", "Редизайн сайта", "Миграция на AWS"
   Признаки: имеет сроки, команду, этапы, бюджет, цели

2. **product** — Продукт, сервис, приложение
   Примеры: "CRM-система", "Мобильное приложение", "API платежей"
   Признаки: используется клиентами, имеет версию, функции, метрики

3. **person** — Конкретный человек, упомянутый по имени
   Примеры: "Иван Петров", "Маша", "CEO компании"
   Признаки: упоминается по имени или уникальной должности

4. **team** — Команда, группа людей
   Примеры: "Frontend-команда", "Отдел продаж", "Squad Alpha"
   Признаки: группа людей, работающих вместе

5. **department** — Департамент, подразделение
   Примеры: "Маркетинг", "HR", "R&D", "Финансовый отдел"
   Признаки: формальная структурная единица организации

6. **organization** — Внешняя организация (партнёр, клиент, конкурент)
   Примеры: "Google", "Банк Х", "ООО Ромашка"

7. **company** — Собственная компания, стартап (когда говорят "мы", "наша компания")
   Примеры: "Наша компания", "Tessbrain", "Synlabs"

8. **technology** — Технология, инструмент, фреймворк, платформа
   Примеры: "React", "PostgreSQL", "Kubernetes", "Figma"

9. **feature** — Функция продукта, модуль, возможность
   Примеры: "Авторизация через Google", "Экспорт в PDF", "Push-уведомления"
   Признаки: часть продукта, конкретная функциональность

10. **document** — Документ, отчёт, спецификация
    Примеры: "ТЗ на редизайн", "Квартальный отчёт", "Контракт с X"

11. **event** — Событие, дедлайн, релиз
    Примеры: "Релиз 2.0", "Конференция в марте", "Дедлайн Q1"

12. **idea** — Идея, предложение, гипотеза
    Примеры: "Идея с подпиской", "Предложение по оптимизации"

13. **concept** — Абстрактное понятие, принцип, методология
    Примеры: "Agile", "Unit-экономика", "Product-market fit"
    ИСПОЛЬЗУЙ ТОЛЬКО если ни один другой тип не подходит!

ФОРМАТ ВЫВОДА (JSON массив):
```json
[
  {
    "entity_id": "уникальный_snake_case_id",
    "entity_type": "ОБЯЗАТЕЛЬНО один из: project|product|person|team|department|organization|company|technology|feature|document|event|idea|concept",
    "name": "Название сущности",
    "description": "Краткое описание (1-2 предложения)",
    "attributes": {
      "key": "value"
    },
    "relationships": [
      {
        "target_type": "тип связанной сущности",
        "target_name": "название",
        "relation_type": "uses|belongs_to|related_to|depends_on|works_on|manages|owns"
      }
    ],
    "importance": "high|medium|low",
    "confidence": 0.0-1.0
  }
]
```

ПРАВИЛА КЛАССИФИКАЦИИ:
- Если упоминают что-то с дедлайнами/этапами/командой → **project**
- Если упоминают продукт для клиентов → **product**
- Если упоминают конкретного человека → **person**
- Если упоминают группу людей → **team**
- Если упоминают отдел/департамент → **department**
- Если упоминают внешнюю компанию → **organization**
- Если упоминают "мы"/"наша компания" → **company**
- Если упоминают инструмент/язык/фреймворк → **technology**
- Если упоминают функцию продукта → **feature**
- Если упоминают документ → **document**
- Если упоминают событие/дату → **event**
- Если это идея/предложение → **idea**
- ТОЛЬКО если ничего не подходит → **concept**

АТРИБУТЫ ПО ТИПАМ:
**project**: status, phase, team_size, budget, deadline, owner
**product**: version, users_count, revenue, features_list
**person**: role, department, email, skills
**team**: members_count, lead, focus_area
**department**: head, budget, headcount
**organization**: type (partner/client/competitor), industry, country
**company**: industry, size, stage (startup/scaleup/enterprise)
**technology**: category (frontend/backend/infra/design/analytics), version, license
**feature**: status (planned/in_progress/done), product_name, priority
**document**: doc_type, author, date, status (draft/final)
**event**: date, location, participants_count
**idea**: author, feasibility (high/medium/low), impact (high/medium/low)
**concept**: domain, related_concepts

ВАЖНО:
- Извлекай ВСЕ упомянутые сущности — но каждая должна реально быть в тексте.
- entity_type ОБЯЗАТЕЛЕН — НЕ оставляй пустым и НЕ ставь "concept" по умолчанию.
- description — из того, что сказано о сущности в транскрипте (конкретно, не общо).
  Если о сущности сказано только имя — краткое честное описание, без домыслов.
- importance оценивай по её роли в обсуждении, а не по «звучности» названия.
- НЕ дублируй участников встречи — они извлекаются отдельным агентом.

Если значимых сущностей нет — верни пустой массив `[]`. Пустой массив лучше
выдуманной сущности.'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация извлеченной сущности с ОБЯЗАТЕЛЬНОЙ типизацией"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        name = item.get("name") or item.get("entity_name")
        entity_type = (
            item.get("entity_type")
            or item.get("type")
            or item.get("category")
        )

        if not name:
            return None

        # Нормализуем тип: приводим к нижнему регистру, убираем пробелы
        if entity_type:
            entity_type = entity_type.strip().lower()

        # Если тип пустой или невалидный — пробуем определить из алиасов
        if not entity_type or entity_type not in VALID_ENTITY_TYPES:
            # Попытка маппинга через алиасы
            alias_type = ENTITY_TYPE_ALIASES.get(entity_type, None) if entity_type else None
            if alias_type:
                entity_type = alias_type
            else:
                # Последняя попытка: эвристика по названию/описанию
                entity_type = self._guess_type_from_content(name, item.get("description", ""), item)
                logger.debug(f"Entity '{name}' had no valid type, guessed: {entity_type}")

        # Генерируем ID если нет
        entity_id = item.get("entity_id") or item.get("id") or str(uuid.uuid4())

        # Нормализуем importance
        importance = item.get("importance") or "medium"
        valid_importance = ["high", "medium", "low"]
        if importance not in valid_importance:
            importance = "medium"

        normalized = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "name": name,
            "description": item.get("description") or "",
            "attributes": item.get("attributes") or {},
            "relationships": item.get("relationships") or [],
            "mentions": item.get("mentions") or [],
            "importance": importance,
            "confidence": float(item.get("confidence") or item.get("confidence_score") or 0.8)
        }

        return super().normalize(normalized)

    @staticmethod
    def _guess_type_from_content(name: str, description: str, item: dict) -> str:
        """
        Эвристическая классификация по содержимому, когда LLM не указал тип.
        Используется как fallback — основной путь: LLM должен указать тип.
        """
        text = f"{name} {description}".lower()
        attrs = item.get("attributes", {})

        # Проверяем атрибуты-маркеры
        if any(k in attrs for k in ("status", "phase", "deadline", "budget", "team_size")):
            return "project"
        if any(k in attrs for k in ("version", "users", "revenue", "features")):
            return "product"
        if any(k in attrs for k in ("role", "email", "skills")):
            return "person"
        if any(k in attrs for k in ("members_count", "lead", "focus_area")):
            return "team"
        if any(k in attrs for k in ("headcount", "head")):
            return "department"

        # Проверяем текстовые маркеры
        project_markers = ("проект", "project", "инициатива", "initiative", "workstream", "программа")
        product_markers = ("продукт", "product", "приложение", "app", "сервис", "service", "платформа")
        person_markers = ("директор", "менеджер", "разработчик", "дизайнер", "аналитик", "CEO", "CTO", "PM")
        team_markers = ("команда", "team", "squad", "группа")
        dept_markers = ("отдел", "департамент", "department", "подразделение", "division")
        tech_markers = ("react", "python", "java", "node", "docker", "kubernetes", "aws", "gcp", "azure",
                        "postgres", "mysql", "redis", "kafka", "figma", "jira", "slack", "git")
        feature_markers = ("функция", "feature", "модуль", "module", "интеграция", "integration")
        org_markers = ("ООО", "ОАО", "Inc", "LLC", "GmbH", "Ltd", "Corp")

        for marker in tech_markers:
            if marker in text:
                return "technology"
        for marker in project_markers:
            if marker in text:
                return "project"
        for marker in product_markers:
            if marker in text:
                return "product"
        for marker in person_markers:
            if marker in text:
                return "person"
        for marker in team_markers:
            if marker in text:
                return "team"
        for marker in dept_markers:
            if marker in text:
                return "department"
        for marker in feature_markers:
            if marker in text:
                return "feature"
        for marker in org_markers:
            if marker in text:
                return "organization"

        # Если ничего не подошло
        return "concept"


# Singleton
entity_agent = EntityAgent()



