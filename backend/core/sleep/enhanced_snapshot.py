# -*- coding: utf-8 -*-
"""
Enhanced Snapshot Generator - Расширенные снапшоты компании.

Реализует полноценные снапшоты согласно концепции:
- Компактные (1-2 страницы)
- Самообновляемые
- Фиксированного размера
- Полные и самодостаточные

Типы снапшотов:
1. CompanySnapshot - полное состояние компании
2. PersonSnapshot - профиль сотрудника
3. ProjectSnapshot - состояние проекта
4. DepartmentSnapshot - состояние отдела

Использование:
```python
from backend.core.sleep.enhanced_snapshot import EnhancedSnapshotGenerator

generator = EnhancedSnapshotGenerator(graph_builder)

# Получить снапшот компании
company = await generator.get_company_snapshot()

# Получить снапшот человека
person = await generator.get_person_snapshot("person_123")
```
"""
import json
import logging
import os
import re as _re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Generic-метки и роли, которые прилетают в Person-узлы как «люди», но людьми не
# являются (обрывки экстракции, собирательные понятия, роли без имени). Их надо
# прятать из списков сотрудников/команды — иначе «Все участники», «Партнеры»,
# «маркдиры», «Фаундер» висят среди реальных людей.
_PERSON_JUNK_EXACT = {
    "все участники", "участники", "все", "команда", "партнеры", "партнёры",
    "партнер", "партнёр", "сотрудник", "сотрудники", "профессионал",
    "организатор", "организатор программы", "женщина", "мужчина", "фаундер",
    "ресерчер", "акционер", "акционеры", "инвестор", "инвестора", "инвесторы",
    "контекст встречи", "снепшот сотрудника", "снимок сотрудника", "маркдиры",
    "эксперт", "эксперты", "спикер", "докладчик", "кандидат", "клиент",
    "собеседник", "аудитория", "гость", "гости", "модератор", "оратор",
    "неизвестный участник", "неизвестный эксперт", "неизвестный",
}


_MANAGEMENT_MARKERS = (
    "ceo", "сео", "cto", "cfo", "coo", "chief", "vp", "вице-президент",
    "генеральный директор", "гендиректор", "директор", "руковод",
    "founder", "основатель", "сооснователь", "co-founder", "управляющ",
    "head of", "owner", "владелец бизнеса", "президент",
)


def _is_management_role(role: str) -> bool:
    """Высший/средний менеджмент по тексту роли — для группировки списка людей."""
    r = (role or "").lower()
    return bool(r) and any(m in r for m in _MANAGEMENT_MARKERS)


def _is_person_junk(name: str) -> bool:
    """True, если имя Person-узла — мусор/составной узел, не реальный человек.

    Ловит: пустышки, generic-роли, составные узлы («Александр и Екатерина»,
    «Катя, Максим, Александр», «Максим/Катя»), диктора («Speaker 1») и
    тайм-фрагменты диаризации («Катя 1:11:57» — тот же человек, что базовый
    узел «Катя/Екатерина», отдельно светить не надо)."""
    s = (name or "").strip().lower()
    if not s:
        return True
    # тайм-фрагмент диаризации («Катя 1:11:57», «Наталья 2:45») — по ПОЛНОЙ
    # строке, т.к. в таймкоде есть ':' (иначе split по ':' его разрежет).
    if _re.search(r"\d{1,2}[:.]\d{2}", s):
        return True
    # чистый диктор: «speaker 1», «спикер 2», «speaker 1 (0:01)»
    if _re.match(r"^(speaker|спикер)\s*\d", s):
        return True
    # имя без роли: часть до первого ':' («Александр: Организатор/Тренер» → имя
    # «александр», роль «организатор/тренер» в структурных проверках не участвует).
    head = s.split(":", 1)[0].strip()
    bare = _re.sub(r"\([^)]*\)", "", head).strip()  # без «(инвестор)»-суффикса
    if s in _PERSON_JUNK_EXACT or head in _PERSON_JUNK_EXACT or bare in _PERSON_JUNK_EXACT:
        return True
    # составной узел из нескольких людей: «Максим/Катя», «Шитов / Кустова»
    if _re.search(r"[а-яёa-z]\s*/\s*[а-яёa-z]", head):
        return True
    words = head.split()
    if len(words) >= 3:
        if "," in head:                    # «Катя, Максим, Александр»
            return True
        if " и " in f" {head} ":           # «Александр и Екатерина»
            return True
    return False


# ============================================================================
# СТРУКТУРЫ СНАПШОТОВ
# ============================================================================

@dataclass
class CompanySnapshot:
    """
    Полный снапшот компании.

    Содержит 12 ключевых разделов для полного понимания состояния компании.
    Формат NoCap-style: структурированный бизнес-профиль.
    """
    # 1. Базовая информация
    name: str = ""
    mission: str = ""
    description: str = ""
    industry: str = ""
    founded: str = ""
    size: str = ""  # small/medium/large
    website: str = ""
    location: str = ""

    # 2. Основатель / CEO
    founder: Dict[str, Any] = field(default_factory=dict)
    # {name, role, background, linkedin}

    # 3. Продукты и бизнес-модель
    products: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, description, status, target_audience}]

    business_model: str = ""
    revenue_model: str = ""
    target_market: str = ""
    competitors: List[str] = field(default_factory=list)

    # 4. Структура
    departments: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, head, employees_count, description}]

    teams: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, lead, members_count, focus}]

    key_people: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, role, responsibilities}] — ТОЛЬКО сотрудники компании
    related_people: List[Dict[str, Any]] = field(default_factory=list)
    # Не-сотрудники, отделённые от key_people: партнёры/внешние эксперты/
    # кандидаты/клиенты/контакты. [{name, role, category}]

    # 5. Текущее состояние
    current_status: str = ""  # growth/stable/restructuring
    current_priorities: List[str] = field(default_factory=list)
    current_challenges: List[str] = field(default_factory=list)
    stage: str = ""  # idea/mvp/growth/scale/mature

    # 6. Проекты
    active_projects: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, status, progress, lead, deadline, description}]

    completed_projects_count: int = 0
    planned_projects: List[str] = field(default_factory=list)

    # 7. Цели и стратегия
    strategic_goals: List[Dict[str, Any]] = field(default_factory=list)
    # [{goal, timeframe, progress, status}]

    okrs: List[Dict[str, Any]] = field(default_factory=list)
    # [{objective, key_results, progress}]

    # 8. Сильные и слабые стороны
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    opportunities: List[str] = field(default_factory=list)
    threats: List[str] = field(default_factory=list)

    # 9. Достижения и проблемы
    recent_achievements: List[Dict[str, Any]] = field(default_factory=list)
    # [{achievement, date, impact}]

    current_problems: List[Dict[str, Any]] = field(default_factory=list)
    # [{problem, severity, responsible, status}]

    # 10. Технологии и инструменты
    tech_stack: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    methodologies: List[str] = field(default_factory=list)

    # 11. Метрики
    kpis: List[Dict[str, Any]] = field(default_factory=list)
    # Реальные финансовые ряды из онтологии цифр (KPI-узлы с series),
    # а не счётчики сущностей: [{name, value, period, series{YYYY-MM: v}}]
    financial_kpis: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, current_value, target, trend}]

    health_score: float = 0.0  # 0-100
    productivity_score: float = 0.0  # 0-100

    # 12. Тренды и ресурсы
    trends: List[Dict[str, Any]] = field(default_factory=list)
    # [{area, direction, description}]

    resources: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, url, type}]  — ссылки, документы, каналы

    # Метаданные
    last_updated: str = ""
    update_source: str = ""  # meeting_id или manual
    version: int = 1

    # ═══ Step 3: Hierarchy + Snippet + Delta ═══
    snapshot_id: str = ""
    snapshot_type: str = "company"
    hierarchy_level: int = 0          # 0 = company (top level)
    parent_snapshot_id: str = ""      # "" для company
    children_snapshot_ids: List[str] = field(default_factory=list)

    snippet: str = ""                 # 1-3 предложения (50-100 токенов) для быстрой навигации
    delta_summary: str = ""           # "Что изменилось с прошлого обновления"
    delta_details: Dict[str, Any] = field(default_factory=dict)
    previous_version_id: str = ""     # Для истории версий

    # Access control
    access_level: int = 2             # INTERNAL
    access_groups: List[str] = field(default_factory=list)
    tenant_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def generate_snippet(self) -> str:
        """Генерирует короткий snippet (1-3 предложения) для быстрой навигации."""
        parts = []
        if self.name:
            parts.append(self.name)
        if self.industry:
            parts.append(self.industry)
        elif self.description:
            parts.append(self.description[:80])
        if self.products:
            product_names = [p.get("name", "") for p in self.products[:3] if p.get("name")]
            if product_names:
                parts.append(f"Продукты: {', '.join(product_names)}")
        if self.active_projects:
            parts.append(f"{len(self.active_projects)} проектов")
        if self.key_people:
            parts.append(f"{len(self.key_people)} сотрудников")
        if self.current_status:
            parts.append(f"Статус: {self.current_status}")
        self.snippet = ". ".join(parts[:4]) if parts else "Компания"
        return self.snippet

    def merge(self, other: 'CompanySnapshot') -> None:
        """
        Объединить с другим (более новым) снепшотом.
        Приоритет у 'other', если там есть данные.
        """
        if not other:
            return

        # Снимок СТАРЫХ значений ДО перезаписи — иначе delta считался после
        # `self.x = other.x` и всегда выходил пустым («Минорные обновления»,
        # «изменений нет» в Сравнить). Теперь сравниваем old vs other честно.
        _old = {
            "name": self.name,
            "current_status": self.current_status,
            "size": self.size,
            "health_score": getattr(self, "health_score", None),
            "active_projects": list(self.active_projects or []),
            "key_people": list(self.key_people or []),
            "kpis": self.kpis,
        }

        # Обновляем простые поля, если они есть в новом. Имя НЕ закрепляем
        # кодом: единственный источник истины для «правильного» имени —
        # ручная правка пользователя (_company_overrides.name накладывается
        # ПОВЕРХ merge при каждой генерации и каждом чтении).
        for field_name in [
            "name", "mission", "description", "industry", "founded", "size",
            "current_status", "website", "location", "business_model",
            "revenue_model", "target_market", "stage",
        ]:
            new_val = getattr(other, field_name, "")
            if new_val:
                setattr(self, field_name, new_val)

        # Dict поля (founder)
        if other.founder and other.founder.get("name"):
            self.founder = other.founder

        # Списки обновляем, если в новом они не пустые
        # (предполагаем, что новый граф актуальнее)
        if other.products:
            self.products = other.products
        if other.active_projects:
            self.active_projects = other.active_projects
        if other.key_people:
            self.key_people = other.key_people
        if other.related_people:
            self.related_people = other.related_people
        if other.departments:
            self.departments = other.departments
        if other.competitors:
            self.competitors = other.competitors
        if other.resources:
            self.resources = other.resources

        # Стратегические вещи (LLM) - если LLM отвалился, в новом будет пусто, оставляем старое
        if other.strengths: self.strengths = other.strengths
        if other.weaknesses: self.weaknesses = other.weaknesses
        if other.opportunities: self.opportunities = other.opportunities
        if other.threats: self.threats = other.threats

        # Метрики обновляем всегда, если они есть
        if other.kpis:
            self.kpis = other.kpis
        if other.financial_kpis:
            self.financial_kpis = other.financial_kpis

        # ═══ Step 3: delta — old (снимок ДО перезаписи) vs ПРИМЕНЁННОЕ ═══
        # Сравниваем с self (после merge), а не с other: отклонённое
        # переименование не должно попадать в дельту как «изменение».
        delta_changes = {}
        for field_name in ["name", "current_status", "size", "health_score"]:
            old_val = _old.get(field_name)
            new_val = getattr(self, field_name, None)
            if new_val and new_val != old_val:
                delta_changes[field_name] = {"old": old_val, "new": new_val}

        # Сравниваем списки
        if other.active_projects and other.active_projects != _old["active_projects"]:
            old_names = {p.get("name", "") for p in _old["active_projects"]}
            new_names = {p.get("name", "") for p in other.active_projects}
            added = new_names - old_names
            removed = old_names - new_names
            if added or removed:
                delta_changes["active_projects"] = {"added": list(added), "removed": list(removed)}

        if other.key_people and other.key_people != _old["key_people"]:
            old_names = {p.get("name", "") for p in _old["key_people"]}
            new_names = {p.get("name", "") for p in other.key_people}
            added = new_names - old_names
            if added:
                delta_changes["key_people"] = {"added": list(added)}

        if other.kpis and other.kpis != _old["kpis"]:
            delta_changes["kpis_updated"] = True

        self.delta_details = delta_changes

        # Генерируем delta_summary из изменений
        delta_parts = []
        if "active_projects" in delta_changes:
            if delta_changes["active_projects"].get("added"):
                delta_parts.append(f"Новые проекты: {', '.join(delta_changes['active_projects']['added'])}")
        if "key_people" in delta_changes:
            if delta_changes["key_people"].get("added"):
                delta_parts.append(f"Новые сотрудники: {', '.join(delta_changes['key_people']['added'])}")
        if "current_status" in delta_changes:
            delta_parts.append(f"Статус: {delta_changes['current_status']['old']} → {delta_changes['current_status']['new']}")
        if "kpis_updated" in delta_changes:
            delta_parts.append("KPI обновлены")
        self.delta_summary = ". ".join(delta_parts) if delta_parts else "Минорные обновления"

        # Анти-спам версий: если материальных изменений нет (delta_parts пуст),
        # НЕ создаём новую версию — иначе за день копятся v274…v285, и «Сравнить»
        # между двумя такими версиями показывает «изменений нет». Обновляем поля
        # и метаданные, версия остаётся прежней.
        self.last_updated = other.last_updated or datetime.now(timezone.utc).isoformat()
        self.generate_snippet()
        if not delta_parts:
            return

        # Сохраняем ссылку на предыдущую версию
        self.previous_version_id = f"company_v{self.version}"
        self.version += 1
        self.generate_snippet()

    def to_text(self, max_length: int = 5000) -> str:
        """Генерация текстового представления для LLM (NoCap-style)."""
        sections = []

        # Заголовок
        sections.append(f"# СНАПШОТ КОМПАНИИ: {self.name}")
        if self.industry:
            sections.append(f"Отрасль: {self.industry}")
        sections.append(f"Обновлено: {self.last_updated}")
        sections.append("")

        # О компании
        if self.description or self.mission:
            sections.append("## О компании")
            if self.description:
                sections.append(self.description)
            if self.mission and self.mission != self.description:
                sections.append(f"Миссия: {self.mission}")
            if self.founded:
                sections.append(f"Основана: {self.founded}")
            if self.location:
                sections.append(f"Локация: {self.location}")
            if self.stage:
                sections.append(f"Стадия: {self.stage}")
            sections.append("")

        # Основатель
        if self.founder and self.founder.get("name"):
            sections.append("## Основатель / CEO")
            f = self.founder
            sections.append(f"Имя: {f.get('name', 'N/A')}")
            if f.get("role"):
                sections.append(f"Роль: {f['role']}")
            if f.get("background"):
                sections.append(f"Бэкграунд: {f['background']}")
            sections.append("")

        # Продукты и бизнес
        if self.products:
            sections.append("## Продукты")
            for p in self.products[:8]:
                desc = f" — {p.get('description', '')[:100]}" if p.get('description') else ""
                status = f" [{p.get('status', 'active')}]" if p.get('status') else ""
                sections.append(f"- {p.get('name', 'N/A')}{status}{desc}")
            sections.append("")

        if self.business_model or self.target_market:
            sections.append("## Бизнес-модель")
            if self.business_model:
                sections.append(self.business_model)
            if self.target_market:
                sections.append(f"Целевой рынок: {self.target_market}")
            if self.revenue_model:
                sections.append(f"Модель монетизации: {self.revenue_model}")
            sections.append("")

        # Текущее состояние
        sections.append("## Текущее состояние")
        sections.append(f"Статус: {self.current_status or 'не определён'}")
        if self.current_priorities:
            sections.append(f"Приоритеты: {', '.join(self.current_priorities[:5])}")
        if self.current_challenges:
            sections.append(f"Вызовы: {', '.join(self.current_challenges[:5])}")
        sections.append("")

        # Ключевые люди
        if self.key_people:
            sections.append("## Команда")
            for p in self.key_people[:10]:
                role = f" — {p.get('role', '')}" if p.get('role') else ""
                sections.append(f"- {p.get('name', 'N/A')}{role}")
            sections.append("")

        # Структура
        if self.departments:
            sections.append("## Структура")
            for dept in self.departments[:5]:
                sections.append(f"- {dept.get('name', 'N/A')}: {dept.get('employees_count', '?')} чел.")
            sections.append("")

        # Проекты
        if self.active_projects:
            sections.append("## Активные проекты")
            for proj in self.active_projects[:8]:
                status = proj.get('status', 'active')
                desc = f" — {proj.get('description', '')[:80]}" if proj.get('description') else ""
                sections.append(f"- {proj.get('name', 'N/A')} [{status}]{desc}")
            sections.append("")

        # Цели
        if self.strategic_goals:
            sections.append("## Стратегические цели")
            for goal in self.strategic_goals[:5]:
                sections.append(f"- {goal.get('goal', 'N/A')} ({goal.get('progress', 0)}%)")
            sections.append("")

        # SWOT
        if any([self.strengths, self.weaknesses, self.opportunities, self.threats]):
            sections.append("## SWOT-анализ")
            if self.strengths:
                sections.append(f"Сильные: {', '.join(self.strengths[:4])}")
            if self.weaknesses:
                sections.append(f"Слабые: {', '.join(self.weaknesses[:4])}")
            if self.opportunities:
                sections.append(f"Возможности: {', '.join(self.opportunities[:4])}")
            if self.threats:
                sections.append(f"Угрозы: {', '.join(self.threats[:4])}")
            sections.append("")

        # Технологии
        if self.tech_stack:
            sections.append("## Технологии")
            sections.append(", ".join(self.tech_stack[:15]))
            sections.append("")

        # Метрики
        if self.kpis:
            sections.append("## Ключевые метрики")
            for kpi in self.kpis[:5]:
                trend = kpi.get('trend', '→')
                sections.append(f"- {kpi.get('name', 'N/A')}: {kpi.get('current_value', 'N/A')} {trend}")
            sections.append("")

        # Ресурсы
        if self.resources:
            sections.append("## Ресурсы")
            for r in self.resources[:5]:
                url = f" ({r.get('url', '')})" if r.get('url') else ""
                sections.append(f"- {r.get('name', 'N/A')}{url}")
            sections.append("")

        result = "\n".join(sections)

        # Обрезаем если слишком длинный
        if len(result) > max_length:
            result = result[:max_length - 100] + "\n\n[...обрезано...]"

        return result


@dataclass
class PersonSnapshot:
    """
    Полный снапшот сотрудника.

    Содержит 8 ключевых разделов для понимания роли и состояния человека.
    """
    # 1. Базовая информация
    person_id: str = ""
    name: str = ""
    role: str = ""
    department: str = ""
    team: str = ""

    # 2. Иерархия
    manager: str = ""
    direct_reports: List[str] = field(default_factory=list)
    colleagues: List[str] = field(default_factory=list)

    # 3. Компетенции
    skills: List[Dict[str, Any]] = field(default_factory=list)
    # [{skill, level, last_used}]

    expertise_areas: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)

    # 4. Текущая работа
    current_projects: List[Dict[str, Any]] = field(default_factory=list)
    # [{project, role, status}]

    current_tasks: List[Dict[str, Any]] = field(default_factory=list)
    # [{task, status, priority, deadline}]

    responsibilities: List[str] = field(default_factory=list)

    # 5. Взаимодействие
    interaction_frequency: Dict[str, int] = field(default_factory=dict)
    # {person_name: meetings_count}

    communication_style: str = ""  # active/moderate/minimal
    collaboration_score: float = 0.0  # 0-100

    # 6. Производительность
    tasks_completed_week: int = 0
    tasks_in_progress: int = 0
    meetings_participated: int = 0
    decisions_made: int = 0

    performance_trend: str = ""  # improving/stable/declining

    # 7. Сильные и слабые стороны
    strengths: List[str] = field(default_factory=list)
    areas_for_improvement: List[str] = field(default_factory=list)

    # 8. Достижения и проблемы
    recent_achievements: List[str] = field(default_factory=list)
    current_challenges: List[str] = field(default_factory=list)

    # 9. Недавние митинги (до 10 последних)
    recent_meetings: List[Dict[str, Any]] = field(default_factory=list)
    # [{meeting_id, title, date, role_in_meeting}]

    # 9b. Атрибутированный вклад из графа (что человек РЕАЛЬНО делал) —
    # раньше считалось числом, но не показывалось содержимое.
    decisions: List[Dict[str, Any]] = field(default_factory=list)      # [{summary, category}]
    ideas: List[str] = field(default_factory=list)                    # предложенные идеи
    opinions: List[Dict[str, Any]] = field(default_factory=list)      # [{summary, sentiment}]
    contradictions: List[str] = field(default_factory=list)           # вовлечён в противоречия
    psychological: Dict[str, Any] = field(default_factory=dict)       # психопрофиль (сводка)

    # 10. LLM-саммари текущей активности
    activity_summary: str = ""
    # "Антон сейчас фокусируется на запуске проекта X, активно участвует
    #  в ежедневных стендапах и принял 3 ключевых решения за эту неделю."

    # 11. Delta-tracking (что изменилось с прошлой версии)
    delta_summary: str = ""
    delta_details: Dict[str, Any] = field(default_factory=dict)
    previous_version_id: str = ""

    # Метаданные
    last_active: str = ""
    last_updated: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: 'PersonSnapshot') -> None:
        """Объединить с новым снепшотом, отслеживая изменения (delta)."""
        if not other: return

        # Собираем дельту
        changes = []
        delta = {}

        # Отслеживаем изменения текстовых полей
        for f in ["name", "role", "department", "team", "manager", "communication_style", "performance_trend"]:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if new_val and new_val != old_val:
                setattr(self, f, new_val)
                if old_val:
                    changes.append(f"{f}: {old_val} → {new_val}")
                    delta[f] = {"old": old_val, "new": new_val}

        # Отслеживаем изменения списков
        if other.current_projects:
            old_names = {p.get("project", "") for p in self.current_projects}
            new_names = {p.get("project", "") for p in other.current_projects}
            added = new_names - old_names
            removed = old_names - new_names
            if added:
                changes.append(f"Новые проекты: {', '.join(added)}")
                delta["projects_added"] = list(added)
            if removed:
                changes.append(f"Завершённые проекты: {', '.join(removed)}")
                delta["projects_removed"] = list(removed)
            self.current_projects = other.current_projects

        if other.current_tasks: self.current_tasks = other.current_tasks
        if other.skills: self.skills = other.skills
        if other.recent_meetings: self.recent_meetings = other.recent_meetings
        if other.activity_summary: self.activity_summary = other.activity_summary

        # Числовые метрики
        for metric in ["tasks_completed_week", "tasks_in_progress", "meetings_participated", "decisions_made"]:
            old_v = getattr(self, metric)
            new_v = getattr(other, metric)
            if new_v and new_v != old_v:
                delta[metric] = {"old": old_v, "new": new_v}
                setattr(self, metric, new_v)

        # LLM поля
        if other.strengths: self.strengths = other.strengths
        if other.areas_for_improvement: self.areas_for_improvement = other.areas_for_improvement
        if other.recent_achievements: self.recent_achievements = other.recent_achievements
        if other.current_challenges: self.current_challenges = other.current_challenges

        # Атрибутированный вклад из графа (берём свежее, не теряем при merge)
        if other.decisions: self.decisions = other.decisions
        if other.ideas: self.ideas = other.ideas
        if other.opinions: self.opinions = other.opinions
        if other.contradictions: self.contradictions = other.contradictions
        if other.psychological: self.psychological = other.psychological

        # Формируем delta (анти-спам версий: без изменений — не бампим версию)
        self.delta_details = delta
        self.delta_summary = "; ".join(changes) if changes else "Без существенных изменений"
        self.last_updated = other.last_updated
        if not changes:
            return
        self.previous_version_id = f"person_{self.person_id}_v{self.version}"
        self.version += 1

    def to_text(self, max_length: int = 2000) -> str:
        """Генерация текстового представления для LLM."""
        sections = []

        # Заголовок
        sections.append(f"# ПРОФИЛЬ: {self.name}")
        sections.append(f"Роль: {self.role}")
        sections.append(f"Отдел: {self.department}")
        sections.append("")

        # LLM-саммари текущей активности
        if self.activity_summary:
            sections.append("## Текущая активность")
            sections.append(self.activity_summary)
            sections.append("")

        # Delta
        if self.delta_summary and self.delta_summary != "Без существенных изменений":
            sections.append(f"## Изменения (v{self.version})")
            sections.append(self.delta_summary)
            sections.append("")

        # Иерархия
        if self.manager or self.colleagues:
            sections.append("## Команда")
            if self.manager:
                sections.append(f"Руководитель: {self.manager}")
            if self.colleagues:
                sections.append(f"Коллеги: {', '.join(self.colleagues[:5])}")
            sections.append("")

        # Навыки
        if self.skills:
            sections.append("## Навыки")
            for skill in self.skills[:5]:
                sections.append(f"- {skill.get('skill', 'N/A')} ({skill.get('level', 'N/A')})")
            sections.append("")

        # Текущие проекты
        if self.current_projects:
            sections.append("## Текущие проекты")
            for proj in self.current_projects[:3]:
                sections.append(f"- {proj.get('project', 'N/A')} [{proj.get('role', 'участник')}]")
            sections.append("")

        # Задачи
        if self.current_tasks:
            sections.append("## Текущие задачи")
            for task in self.current_tasks[:5]:
                priority = task.get('priority', 'normal')
                sections.append(f"- [{priority}] {task.get('task', 'N/A')}")
            sections.append("")

        # Недавние митинги
        if self.recent_meetings:
            sections.append("## Недавние встречи")
            for mtg in self.recent_meetings[:5]:
                date = mtg.get("date", "")
                title = mtg.get("title", "N/A")
                sections.append(f"- {date}: {title}")
            sections.append("")

        # Производительность
        sections.append("## Активность (метрики)")
        sections.append(f"Задач выполнено (неделя): {self.tasks_completed_week}")
        sections.append(f"Задач в работе: {self.tasks_in_progress}")
        sections.append(f"Участие во встречах: {self.meetings_participated}")
        # Тренд печатаем ТОЛЬКО если он посчитан. Раньше здесь стоял
        # fallback 'стабильно', а поле performance_trend не заполняется
        # никаким кодом — то есть в контекст чата и в слепок человека
        # уходило утверждение о его динамике, которого никто не измерял.
        # Отсутствие строки честнее выдуманной стабильности.
        if self.performance_trend:
            sections.append(f"Тренд: {self.performance_trend}")

        result = "\n".join(sections)

        if len(result) > max_length:
            result = result[:max_length - 100] + "\n\n[...обрезано...]"

        return result


@dataclass
class ProjectSnapshot:
    """Полный снапшот проекта."""
    project_id: str = ""
    name: str = ""
    description: str = ""
    status: str = ""  # planning/active/on_hold/completed

    # Команда
    lead: str = ""
    team_members: List[Dict[str, Any]] = field(default_factory=list)

    # Прогресс
    progress: float = 0.0
    milestones: List[Dict[str, Any]] = field(default_factory=list)

    # Задачи
    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_in_progress: int = 0
    tasks_blocked: int = 0

    # Риски
    risks: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    # Решения
    recent_decisions: List[Dict[str, Any]] = field(default_factory=list)

    # Метрики
    health_score: float = 0.0
    velocity: float = 0.0

    # LLM-выжимка: «на каком этапе, что хорошо/плохо, перспективы» — вердикт
    # для карточки, а не сырой список фактов. Генерится ночью (1 вызов/проект).
    ai_summary: str = ""

    # Метаданные
    start_date: str = ""
    deadline: str = ""
    last_updated: str = ""
    version: int = 1

    # Delta-tracking
    delta_summary: str = ""
    delta_details: Dict[str, Any] = field(default_factory=dict)
    previous_version_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: 'ProjectSnapshot') -> None:
        """Объединить с новым снепшотом, отслеживая изменения (delta)."""
        if not other: return

        # Собираем дельту
        changes = []
        delta = {}

        # Отслеживаем изменения скалярных полей
        for f in ["name", "description", "status", "lead", "start_date", "deadline", "ai_summary"]:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if new_val and new_val != old_val:
                setattr(self, f, new_val)
                if old_val:
                    changes.append(f"{f}: {old_val} → {new_val}")
                    delta[f] = {"old": old_val, "new": new_val}

        # Метрики — отслеживаем числовые изменения
        for metric in ["progress", "tasks_total", "tasks_completed", "tasks_in_progress", "tasks_blocked", "health_score", "velocity"]:
            old_v = getattr(self, metric)
            new_v = getattr(other, metric)
            if new_v and new_v != old_v:
                delta[metric] = {"old": old_v, "new": new_v}
                if metric == "progress":
                    changes.append(f"Прогресс: {old_v}% → {new_v}%")
                elif metric == "status":
                    changes.append(f"Статус: {old_v} → {new_v}")
                setattr(self, metric, new_v)

        # Списки
        if other.team_members:
            old_names = {m.get("name", "") for m in self.team_members}
            new_names = {m.get("name", "") for m in other.team_members}
            added = new_names - old_names
            removed = old_names - new_names
            if added:
                changes.append(f"В команду вошли: {', '.join(added)}")
                delta["team_added"] = list(added)
            if removed:
                changes.append(f"Покинули команду: {', '.join(removed)}")
                delta["team_removed"] = list(removed)
            self.team_members = other.team_members

        if other.risks: self.risks = other.risks
        if other.blockers:
            new_blockers = set(other.blockers) - set(self.blockers)
            if new_blockers:
                changes.append(f"Новые блокеры: {', '.join(new_blockers)}")
                delta["new_blockers"] = list(new_blockers)
            self.blockers = other.blockers

        if other.recent_decisions: self.recent_decisions = other.recent_decisions

        # Формируем delta (анти-спам версий: без изменений — не бампим версию)
        self.delta_details = delta
        self.delta_summary = "; ".join(changes) if changes else "Без существенных изменений"
        self.last_updated = other.last_updated
        if not changes:
            return
        self.previous_version_id = f"project_{self.project_id}_v{self.version}"
        self.version += 1

    def to_text(self, max_length: int = 2000) -> str:
        """Генерация текстового представления."""
        sections = []

        sections.append(f"# ПРОЕКТ: {self.name}")
        sections.append(f"Статус: {self.status}")
        sections.append(f"Прогресс: {self.progress}%")
        if self.ai_summary:
            sections.append(f"\n## Состояние\n{self.ai_summary}")
        sections.append("")

        if self.lead:
            sections.append(f"Руководитель: {self.lead}")

        sections.append(f"Задачи: {self.tasks_completed}/{self.tasks_total} выполнено")
        if self.tasks_blocked:
            sections.append(f"Заблокировано: {self.tasks_blocked}")

        if self.blockers:
            sections.append("")
            sections.append("## Блокеры")
            for b in self.blockers[:3]:
                sections.append(f"- {b}")

        if self.risks:
            sections.append("")
            sections.append("## Риски")
            for r in self.risks[:3]:
                sections.append(f"- {r.get('description', r.get('name', str(r)))}")

        if self.recent_decisions:
            sections.append("")
            sections.append("## Последние решения")
            for d in self.recent_decisions[:3]:
                sections.append(f"- {d.get('summary', d.get('description', str(d)))}")

        # Delta
        if self.delta_summary and self.delta_summary != "Без существенных изменений":
            sections.append("")
            sections.append(f"## Изменения (v{self.version})")
            sections.append(self.delta_summary)

        result = "\n".join(sections)

        if len(result) > max_length:
            result = result[:max_length - 100] + "\n\n[...обрезано...]"

        return result


# ============================================================================
# ДОПОЛНИТЕЛЬНЫЕ СНАПШОТЫ: Product, Department, Team
# ============================================================================

@dataclass
class ProductSnapshot:
    """Снапшот продукта/сервиса компании."""
    product_id: str = ""
    name: str = ""
    description: str = ""
    product_type: str = ""  # saas/service/physical/internal_tool
    status: str = ""  # idea/development/launched/mature/sunset

    # Целевая аудитория
    target_audience: str = ""

    # Метрики
    users_count: int = 0
    revenue: float = 0.0
    revenue_unit: str = "руб"

    # Фичи и стек
    key_features: List[str] = field(default_factory=list)
    tech_stack: List[str] = field(default_factory=list)

    # Команда
    owner: str = ""
    team_members: List[str] = field(default_factory=list)

    # Связанные проекты
    related_projects: List[str] = field(default_factory=list)

    # KPI
    kpis: List[Dict[str, Any]] = field(default_factory=list)

    # Риски
    risks: List[str] = field(default_factory=list)

    # Delta
    delta_summary: str = ""
    delta_details: Dict[str, Any] = field(default_factory=dict)
    last_updated: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: 'ProductSnapshot') -> None:
        """Объединить с новыми данными, отслеживая изменения."""
        if not other: return
        changes = []
        delta = {}

        for f in ["name", "description", "product_type", "status", "target_audience", "owner"]:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if new_val and new_val != old_val:
                if old_val:
                    changes.append(f"{f}: {old_val} → {new_val}")
                    delta[f] = {"old": old_val, "new": new_val}
                setattr(self, f, new_val)

        for metric in ["users_count", "revenue"]:
            old_v = getattr(self, metric)
            new_v = getattr(other, metric)
            if new_v and new_v != old_v:
                delta[metric] = {"old": old_v, "new": new_v}
                setattr(self, metric, new_v)

        if other.key_features: self.key_features = other.key_features
        if other.tech_stack: self.tech_stack = other.tech_stack
        if other.team_members: self.team_members = other.team_members
        if other.related_projects: self.related_projects = other.related_projects
        if other.kpis: self.kpis = other.kpis
        if other.risks: self.risks = other.risks

        self.delta_details = delta
        self.delta_summary = "; ".join(changes) if changes else "Без существенных изменений"
        self.last_updated = other.last_updated
        self.version += 1

    def to_text(self, max_length: int = 2000) -> str:
        sections = [f"# ПРОДУКТ: {self.name}"]
        if self.description:
            sections.append(self.description)
        sections.append(f"Тип: {self.product_type or 'не указан'} | Статус: {self.status or 'не указан'}")
        if self.target_audience:
            sections.append(f"Аудитория: {self.target_audience}")
        if self.owner:
            sections.append(f"Владелец: {self.owner}")

        if self.key_features:
            sections.append("\n## Ключевые фичи")
            for f in self.key_features[:5]:
                sections.append(f"- {f}")

        if self.kpis:
            sections.append("\n## KPI")
            for kpi in self.kpis[:5]:
                sections.append(f"- {kpi.get('name', 'N/A')}: {kpi.get('value', 'N/A')} {kpi.get('unit', '')}")

        if self.risks:
            sections.append("\n## Риски")
            for r in self.risks[:3]:
                sections.append(f"- {r}")

        if self.delta_summary and self.delta_summary != "Без существенных изменений":
            sections.append(f"\n## Изменения (v{self.version})")
            sections.append(self.delta_summary)

        result = "\n".join(sections)
        return result[:max_length] if len(result) > max_length else result


@dataclass
class DepartmentSnapshot:
    """Снапшот отдела/департамента."""
    department_id: str = ""
    name: str = ""
    description: str = ""

    # Руководство
    head: str = ""

    # Состав
    teams: List[str] = field(default_factory=list)
    members_count: int = 0
    key_people: List[Dict[str, Any]] = field(default_factory=list)  # [{name, role}]

    # Проекты отдела
    active_projects: List[str] = field(default_factory=list)

    # Метрики
    kpis: List[Dict[str, Any]] = field(default_factory=list)
    budget: float = 0.0
    budget_unit: str = "руб"

    # Задачи
    tasks_total: int = 0
    tasks_completed: int = 0

    # Проблемы
    risks: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    # LLM-выжимка состояния отдела для карточки (этап/хорошо/плохо/перспективы)
    ai_summary: str = ""

    # Delta
    delta_summary: str = ""
    delta_details: Dict[str, Any] = field(default_factory=dict)
    last_updated: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: 'DepartmentSnapshot') -> None:
        if not other: return
        changes = []
        delta = {}

        for f in ["name", "description", "head", "ai_summary"]:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if new_val and new_val != old_val:
                if old_val:
                    changes.append(f"{f}: {old_val} → {new_val}")
                    delta[f] = {"old": old_val, "new": new_val}
                setattr(self, f, new_val)

        if other.members_count and other.members_count != self.members_count:
            delta["members_count"] = {"old": self.members_count, "new": other.members_count}
            self.members_count = other.members_count

        if other.teams: self.teams = other.teams
        if other.key_people: self.key_people = other.key_people
        if other.active_projects: self.active_projects = other.active_projects
        if other.kpis: self.kpis = other.kpis
        if other.risks: self.risks = other.risks
        if other.blockers: self.blockers = other.blockers

        self.delta_details = delta
        self.delta_summary = "; ".join(changes) if changes else "Без существенных изменений"
        self.last_updated = other.last_updated
        self.version += 1

    def to_text(self, max_length: int = 2000) -> str:
        sections = [f"# ОТДЕЛ: {self.name}"]
        if self.ai_summary:
            sections.append(f"## Состояние\n{self.ai_summary}")
        if self.description:
            sections.append(self.description)
        if self.head:
            sections.append(f"Руководитель: {self.head}")
        sections.append(f"Сотрудников: {self.members_count}")

        if self.teams:
            sections.append(f"\nКоманды: {', '.join(self.teams)}")

        if self.key_people:
            sections.append("\n## Ключевые люди")
            for p in self.key_people[:5]:
                role = f" — {p.get('role', '')}" if p.get('role') else ""
                sections.append(f"- {p.get('name', 'N/A')}{role}")

        if self.active_projects:
            sections.append(f"\n## Проекты ({len(self.active_projects)})")
            for p in self.active_projects[:5]:
                sections.append(f"- {p}")

        if self.kpis:
            sections.append("\n## KPI")
            for kpi in self.kpis[:5]:
                sections.append(f"- {kpi.get('name', 'N/A')}: {kpi.get('value', 'N/A')}")

        if self.delta_summary and self.delta_summary != "Без существенных изменений":
            sections.append(f"\n## Изменения (v{self.version})")
            sections.append(self.delta_summary)

        result = "\n".join(sections)
        return result[:max_length] if len(result) > max_length else result


@dataclass
class TeamSnapshot:
    """Снапшот команды."""
    team_id: str = ""
    name: str = ""
    description: str = ""
    department: str = ""

    # Состав
    lead: str = ""
    members: List[Dict[str, Any]] = field(default_factory=list)  # [{name, role}]
    members_count: int = 0

    # Фокус
    focus_area: str = ""
    current_projects: List[str] = field(default_factory=list)

    # Задачи
    tasks_total: int = 0
    tasks_completed: int = 0

    # Метрики
    velocity: float = 0.0
    health_score: float = 0.0

    # Delta
    delta_summary: str = ""
    delta_details: Dict[str, Any] = field(default_factory=dict)
    last_updated: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: 'TeamSnapshot') -> None:
        if not other: return
        changes = []
        delta = {}

        for f in ["name", "description", "department", "lead", "focus_area"]:
            old_val = getattr(self, f)
            new_val = getattr(other, f)
            if new_val and new_val != old_val:
                if old_val:
                    changes.append(f"{f}: {old_val} → {new_val}")
                    delta[f] = {"old": old_val, "new": new_val}
                setattr(self, f, new_val)

        if other.members:
            old_names = {m.get("name", "") for m in self.members}
            new_names = {m.get("name", "") for m in other.members}
            added = new_names - old_names
            if added:
                changes.append(f"Новые участники: {', '.join(added)}")
                delta["members_added"] = list(added)
            self.members = other.members
            self.members_count = len(other.members)

        if other.current_projects: self.current_projects = other.current_projects

        for metric in ["velocity", "health_score", "tasks_total", "tasks_completed"]:
            old_v = getattr(self, metric)
            new_v = getattr(other, metric)
            if new_v and new_v != old_v:
                delta[metric] = {"old": old_v, "new": new_v}
                setattr(self, metric, new_v)

        self.delta_details = delta
        self.delta_summary = "; ".join(changes) if changes else "Без существенных изменений"
        self.last_updated = other.last_updated
        self.version += 1

    def to_text(self, max_length: int = 1500) -> str:
        sections = [f"# КОМАНДА: {self.name}"]
        if self.department:
            sections.append(f"Отдел: {self.department}")
        if self.lead:
            sections.append(f"Лид: {self.lead}")
        sections.append(f"Участников: {self.members_count}")

        if self.focus_area:
            sections.append(f"Фокус: {self.focus_area}")

        if self.members:
            sections.append("\n## Состав")
            for m in self.members[:8]:
                role = f" — {m.get('role', '')}" if m.get('role') else ""
                sections.append(f"- {m.get('name', 'N/A')}{role}")

        if self.current_projects:
            sections.append("\n## Проекты")
            for p in self.current_projects[:5]:
                sections.append(f"- {p}")

        sections.append(f"\nЗадачи: {self.tasks_completed}/{self.tasks_total}")

        if self.delta_summary and self.delta_summary != "Без существенных изменений":
            sections.append(f"\n## Изменения (v{self.version})")
            sections.append(self.delta_summary)

        result = "\n".join(sections)
        return result[:max_length] if len(result) > max_length else result


# ============================================================================
# ГЕНЕРАТОР СНАПШОТОВ
# ============================================================================

class EnhancedSnapshotGenerator:
    """
    Генератор расширенных снапшотов.

    Особенности:
    - Умное обновление (merge, не перезапись)
    - Фиксированный размер (оптимизация)
    - Автоматическое обновление при новых данных
    """

    def __init__(
        self,
        graph_builder=None,
        llm_router=None,  # Добавили LLM Router
        storage_path: str = "data/snapshots"
    ):
        self.graph = graph_builder
        self.llm = llm_router
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.user_id = ""  # Set by caller (knowledge_sync)

        # Кэш снапшотов
        self._company_snapshot: Optional[CompanySnapshot] = None
        self._person_snapshots: Dict[str, PersonSnapshot] = {}
        self._project_snapshots: Dict[str, ProjectSnapshot] = {}
        self._product_snapshots: Dict[str, 'ProductSnapshot'] = {}
        self._department_snapshots: Dict[str, 'DepartmentSnapshot'] = {}
        self._team_snapshots: Dict[str, 'TeamSnapshot'] = {}
        # «Грязный» флаг: данные на диске устарели (после sync) → каждую
        # сущность перегенерировать ОДИН раз при следующем доступе.
        # См. invalidate_snapshot_cache + персистентный маркер ниже.
        self._dirty = False
        # Что уже перегенерировано с момента последней инвалидации (чтобы при
        # _dirty=True каждая сущность регенерилась один раз, а не на каждый read).
        self._regen_done: set = set()

        # Ручные правки (overrides), переживающие ре-генерацию. Накладываются
        # ПОВЕРХ сгенерированного снапшота. company: {field: value};
        # person: {person_id: {field: value}}.
        self._company_overrides: Dict[str, Any] = {}
        self._person_overrides: Dict[str, Dict[str, Any]] = {}
        # Ручная классификация людей (employee/partner/external/candidate/client),
        # переживает ре-генерацию. Ключ — person_id ИЛИ нормализованное имя.
        self._person_class_overrides: Dict[str, str] = {}

        # Загружаем существующие снапшоты
        self._load_snapshots()
        self._load_overrides()

        # Персистентный маркер «нужна регенерация» переживает рестарт: после
        # sync invalidate_snapshot_cache() пишет его на диск, и первый read
        # после рестарта перегенерирует снапшот (иначе с диска отдавался
        # устаревший — симптом «снапшот не поменялся после рестарта бэка»).
        try:
            if (self.storage_path / ".needs_regen").exists():
                self._dirty = True
        except Exception:
            logger.debug("regen marker check failed", exc_info=True)

    def set_storage_path(self, path) -> None:
        """Переключить хранилище на per-user путь И перечитать оверрайды.

        Раньше вызывающие присваивали storage_path напрямую, а оверрайды
        грузились ровно один раз в __init__ из дефолтного data/snapshots.
        Итог: ручная правка (напр. «Основатель/CEO») писалась в per-user файл,
        который после рестарта/в worker-процессе НИКОГДА не читался обратно —
        ночная регенерация «возвращала» LLM-догадку. Плюс межтенантная утечка:
        оверрайды юзера A оставались в памяти и накладывались на снапшот B.
        """
        p = Path(path)
        if p == self.storage_path:
            return
        self.storage_path = p
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("storage_path mkdir failed", exc_info=True)
        # Оверрайды принадлежат тенанту: сбросить чужие, загрузить свои.
        self._company_overrides = {}
        self._person_overrides = {}
        self._person_class_overrides = {}
        self._load_overrides()
        try:
            if (p / ".needs_regen").exists():
                self._dirty = True
        except Exception:
            logger.debug("regen marker check failed", exc_info=True)

    def _reset_caches(self):
        """Очистить in-memory кэш снапшотов (после sync или смены user_id).

        Не трогаем файлы на диске — только память. Следующий get_*_snapshot
        перечитает их с диска (_load_snapshots) или перегенерирует.
        """
        self._company_snapshot = None
        self._person_snapshots = {}
        self._project_snapshots = {}
        self._product_snapshots = {}
        self._department_snapshots = {}
        self._team_snapshots = {}
        # Диск тоже считаем устаревшим → каждая сущность регенерится один раз.
        self._dirty = True
        self._regen_done = set()

    def _load_snapshots(self):
        """Загрузить снапшоты из файлов."""
        # Company snapshot
        company_file = self.storage_path / "company_snapshot.json"
        if company_file.exists():
            try:
                with open(company_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._company_snapshot = CompanySnapshot(**data)
                logger.debug("Loaded company snapshot")
            except Exception as e:
                logger.warning(f"Failed to load company snapshot: {e}")

        # Person snapshots
        persons_dir = self.storage_path / "persons"
        if persons_dir.exists():
            for file in persons_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        person_id = file.stem
                        self._person_snapshots[person_id] = PersonSnapshot(**data)
                except Exception as e:
                    logger.warning(f"Failed to load person snapshot {file}: {e}")

        # Product snapshots
        products_dir = self.storage_path / "products"
        if products_dir.exists():
            for file in products_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self._product_snapshots[file.stem] = ProductSnapshot(**data)
                except Exception as e:
                    logger.warning(f"Failed to load product snapshot {file}: {e}")

        # Department snapshots
        departments_dir = self.storage_path / "departments"
        if departments_dir.exists():
            for file in departments_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self._department_snapshots[file.stem] = DepartmentSnapshot(**data)
                except Exception as e:
                    logger.warning(f"Failed to load department snapshot {file}: {e}")

        # Team snapshots
        teams_dir = self.storage_path / "teams"
        if teams_dir.exists():
            for file in teams_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self._team_snapshots[file.stem] = TeamSnapshot(**data)
                except Exception as e:
                    logger.warning(f"Failed to load team snapshot {file}: {e}")

    # ───────────────────────── Overrides (ручные правки) ─────────────────────
    def _load_overrides(self):
        """Загрузить ручные правки с диска (best-effort)."""
        try:
            cf = self.storage_path / "company_overrides.json"
            if cf.exists():
                with open(cf, "r", encoding="utf-8") as f:
                    self._company_overrides = json.load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load company overrides: {e}")
        try:
            pf = self.storage_path / "person_overrides.json"
            if pf.exists():
                with open(pf, "r", encoding="utf-8") as f:
                    self._person_overrides = json.load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load person overrides: {e}")
        try:
            cf2 = self.storage_path / "person_class_overrides.json"
            if cf2.exists():
                with open(cf2, "r", encoding="utf-8") as f:
                    self._person_class_overrides = json.load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load person class overrides: {e}")

    def _save_overrides(self):
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path / "company_overrides.json", "w", encoding="utf-8") as f:
                json.dump(self._company_overrides, f, ensure_ascii=False, indent=2)
            with open(self.storage_path / "person_overrides.json", "w", encoding="utf-8") as f:
                json.dump(self._person_overrides, f, ensure_ascii=False, indent=2)
            with open(self.storage_path / "person_class_overrides.json", "w", encoding="utf-8") as f:
                json.dump(self._person_class_overrides, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save overrides: {e}")

    # ── Регенерация после sync: каждая сущность обновляется один раз ──
    def _should_regen(self, key: str, force: bool) -> bool:
        """True если сущность нужно перегенерировать: явный force ИЛИ диск
        устарел (_dirty) и эту сущность ещё не обновляли с момента инвалидации."""
        if force:
            return True
        # Маркер проверяем НА КАЖДЫЙ read, а не только в __init__/set_storage_path:
        # воркер (отдельный процесс!) после инжеста пишет .needs_regen, а
        # API-процесс с горячим кэшем иначе не узнал бы об этом до рестарта —
        # симптом «снапшот обновился только после кнопки Пересобрать».
        if not getattr(self, "_dirty", False):
            try:
                if (self.storage_path / ".needs_regen").exists():
                    self._dirty = True
                    self._regen_done = set()
            except Exception:
                logger.debug("regen marker check failed", exc_info=True)
        if getattr(self, "_dirty", False) and key not in self._regen_done:
            return True
        return False

    def _ttl_stale(self, key: str, last_updated: str) -> bool:
        """TTL-подстраховка: снапшот старше SNAPSHOT_TTL_HOURS (по умолчанию 12,
        0 = выключено) авто-регенерируется при чтении даже без инвалидации.
        Повторные попытки для одного key — не чаще раза в час (если генерация
        падает/деградирует, guard сохраняет старый снапшот со старым
        last_updated — без троттлинга каждый read жёг бы LLM-вызов)."""
        try:
            ttl_h = float(os.getenv("SNAPSHOT_TTL_HOURS", "12") or 0)
            if ttl_h <= 0 or not last_updated:
                return False
            ts = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if (now - ts).total_seconds() < ttl_h * 3600:
                return False
            tries = getattr(self, "_ttl_last_try", None)
            if tries is None:
                tries = {}
                self._ttl_last_try = tries
            last_try = tries.get(key)
            if last_try and (now - last_try).total_seconds() < 3600:
                return False
            tries[key] = now
            logger.info(f"♻️ Снапшот {key} старше TTL ({ttl_h}ч) — авто-регенерация")
            return True
        except Exception:
            logger.debug("ttl staleness check failed", exc_info=True)
            return False

    _TOMBSTONE_LABELS = ("Person", "Entity", "Product", "Team", "Department")

    def _is_tombstoned_name(self, name: Optional[str]) -> bool:
        """Имя удалено пользователем (tombstone)? Never-raise.

        Проверяем по всем меткам, которыми оперирует удаление из снапшота:
        is_tombstoned внутри также матчит tombstone без метки."""
        if not (self.user_id and name):
            return False
        try:
            from backend.core.store.entity_tombstones import is_tombstoned
            return any(is_tombstoned(self.user_id, name, lb)
                       for lb in self._TOMBSTONE_LABELS)
        except Exception:
            logger.debug("tombstone check skipped", exc_info=True)
            return False

    def _strip_tombstoned(self, snapshot: Optional['CompanySnapshot']) -> None:
        """Вычистить удалённые пользователем сущности из готового снапшота.

        Merge/degraded-guard могут протащить их из СТАРОГО сохранённого
        снапшота (когда новая генерация пустая), а LLM — переизобрести из
        текста встреч. Финальный фильтр гарантирует: удалённое не всплывает."""
        if snapshot is None:
            return
        try:
            for attr in ("key_people", "related_people", "active_projects",
                         "products"):
                items = getattr(snapshot, attr, None)
                if not items:
                    continue
                kept = [it for it in items
                        if not self._is_tombstoned_name(
                            (it or {}).get("name") if isinstance(it, dict)
                            else str(it))]
                if len(kept) != len(items):
                    setattr(snapshot, attr, kept)
        except Exception:
            logger.debug("tombstone strip skipped", exc_info=True)

    def _mark_regen_done(self, key: str) -> None:
        try:
            self._regen_done.add(key)
        except Exception:
            logger.debug("mark regen done failed", exc_info=True)

    def _clear_regen_marker(self) -> None:
        """Снять персистентный маркер — свежие данные подхвачены."""
        try:
            marker = self.storage_path / ".needs_regen"
            if marker.exists():
                marker.unlink()
        except Exception:
            logger.debug("clear regen marker failed", exc_info=True)

    def set_person_classification(self, person_id_or_name: str, classification: str) -> None:
        """Ручная классификация человека (employee/partner/external/candidate/
        client), переживает ре-генерацию. Позволяет, напр., перевести человека
        из «внешних» в «сотрудники». Ключуем И по id, И по имени для надёжности."""
        key = (person_id_or_name or "").strip()
        if not key:
            return
        self._person_class_overrides[key] = classification
        # дублируем по нижнему регистру имени — split-loop матчит и так, и так
        self._person_class_overrides[key.lower()] = classification
        self._save_overrides()
        # компанию пересоберём при следующем read (классификация влияет на состав)
        self._company_snapshot = None

    def _classification_override_for(self, person_id: str, name: str) -> Optional[str]:
        """Найти ручную классификацию по id или имени (если задана)."""
        ov = self._person_class_overrides or {}
        for k in (person_id, (person_id or "").lower(), name, (name or "").strip().lower()):
            if k and k in ov:
                return ov[k]
        return None

    _FOREIGN_ORG_MARKERS = (
        "компани", "банк", "холдинг", "корпорац", " ооо", "ооо ",
        " зао", " пао", " ао ", "llc", "inc.", "gmbh", "corporation",
    )

    def _categorize_person(self, pid: str, name: str, role: str,
                           description: str = "", department: str = "",
                           engagement: Optional[int] = None) -> str:
        """ЕДИНЫЙ каскад категоризации человека (management/employee/partner/
        client/candidate/external) — им пользуются и списки людей, и иерархия.
        Раньше иерархия считала категорию своей урезанной копией (без
        оргструктуры и фильтра чужих компаний) — списки расходились.

        Порядок сигналов: ручной override > оргструктура компании >
        текст роли; титул чужой компании и упоминание сторонней организации
        в роли/отделе уводят во внешние."""
        manual = self._classification_override_for(pid, name)
        if manual:
            return manual
        category = self._classify_person(role, description)
        if category == "employee" and _is_management_role(role):
            category = "management"
        _org = self._org_membership(name)
        if _org == "head":
            return "management"
        if _org == "member":
            return category if category == "management" else "employee"
        # Титул без вовлечённости = гость одной встречи (CEO чужой компании).
        if category == "management" and engagement is not None and engagement < 3:
            category = "external"
        # Роль/отдел указывают на ЧУЖУЮ организацию («ВП HR Трубной
        # металлургической компании», «глава B2C банка») — гость с титулом.
        if category in ("management", "employee"):
            blob = f"{department} {role}".lower()
            if any(m in blob for m in self._FOREIGN_ORG_MARKERS):
                category = "external"
        return category

    def set_company_override(self, field: str, value: Any) -> None:
        """Установить ручную правку поля компании (переживёт ре-генерацию)."""
        self._company_overrides[field] = value
        self._save_overrides()
        # Применяем сразу к закэшированному снапшоту, если он есть.
        if self._company_snapshot is not None:
            self._apply_company_overrides(self._company_snapshot)

    def set_person_override(self, person_id: str, field: str, value: Any) -> None:
        self._person_overrides.setdefault(person_id, {})[field] = value
        self._save_overrides()
        if person_id in self._person_snapshots:
            self._apply_person_overrides(person_id, self._person_snapshots[person_id])

    def clear_company_override(self, field: str) -> None:
        if field in self._company_overrides:
            del self._company_overrides[field]
            self._save_overrides()

    def _apply_company_overrides(self, snapshot: "CompanySnapshot") -> None:
        """Наложить ручные правки поверх сгенерированного снапшота компании."""
        for field_name, value in (self._company_overrides or {}).items():
            try:
                # founder — это dict {name,...}; если правку сохранили строкой
                # (пользователь вписал только имя), нормализуем, иначе .get("name")
                # ломается и карточка показывает пусто/мусор.
                if field_name == "founder" and isinstance(value, str):
                    value = {"name": value}
                if hasattr(snapshot, field_name):
                    setattr(snapshot, field_name, value)
            except Exception:
                logger.debug("override apply skipped for %s", field_name, exc_info=True)

    def _apply_person_overrides(self, person_id: str, snapshot: "PersonSnapshot") -> None:
        for field_name, value in (self._person_overrides.get(person_id) or {}).items():
            try:
                if hasattr(snapshot, field_name):
                    setattr(snapshot, field_name, value)
            except Exception:
                logger.debug("person override apply skipped for %s", field_name, exc_info=True)

    def _save_company_snapshot(self):
        """Сохранить снапшот компании с историей версий и retention policy."""
        if not self._company_snapshot:
            return

        file_path = self.storage_path / "company_snapshot.json"

        # ═══ Step 3: Сохраняем предыдущую версию перед перезаписью ═══
        try:
            if file_path.exists():
                versions_dir = self.storage_path / "versions" / "company"
                versions_dir.mkdir(parents=True, exist_ok=True)
                version_num = self._company_snapshot.version - 1  # Предыдущая версия
                if version_num > 0:
                    version_file = versions_dir / f"v{version_num}.json"
                    if not version_file.exists():
                        # Копируем текущий (до merge) как предыдущую версию
                        import shutil
                        shutil.copy2(file_path, version_file)
                        logger.debug(f"📸 Saved company snapshot version {version_num}")

                # Retention policy: оставляем только последние 10 полных версий
                self._apply_retention_policy(versions_dir, max_versions=10)
        except Exception as e:
            logger.warning(f"Failed to save version history: {e}")

        # Сохраняем текущую версию
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self._company_snapshot.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save company snapshot: {e}")

    def _reload_person_snapshot(self, person_id: str) -> bool:
        """Догрузить ОДИН person-снапшот с диска в кэш (файл появился после
        создания этого генератора). True — загружен."""
        try:
            file_path = self.storage_path / "persons" / f"{person_id}.json"
            if not file_path.exists():
                return False
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._person_snapshots[person_id] = PersonSnapshot(**data)
            logger.info("person snapshot «%s» догружен с диска", person_id)
            return True
        except Exception as e:
            logger.debug("reload person snapshot %s failed: %s", person_id, e)
            return False

    def _save_person_snapshot(self, person_id: str):
        """Сохранить снапшот человека с историей версий."""
        if person_id not in self._person_snapshots:
            return

        persons_dir = self.storage_path / "persons"
        persons_dir.mkdir(exist_ok=True)

        file_path = persons_dir / f"{person_id}.json"

        # ═══ Step 3: Сохраняем предыдущую версию ═══
        try:
            snapshot = self._person_snapshots[person_id]
            if file_path.exists() and hasattr(snapshot, 'version') and snapshot.version > 1:
                versions_dir = self.storage_path / "versions" / "persons" / person_id
                versions_dir.mkdir(parents=True, exist_ok=True)
                version_file = versions_dir / f"v{snapshot.version - 1}.json"
                if not version_file.exists():
                    import shutil
                    shutil.copy2(file_path, version_file)

                # Retention policy: последние 10 версий
                self._apply_retention_policy(versions_dir, max_versions=10)
        except Exception as e:
            logger.warning(f"Failed to save person version history: {e}")

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self._person_snapshots[person_id].to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save person snapshot: {e}")

    def _apply_retention_policy(self, versions_dir: Path, max_versions: int = 10):
        """
        Retention policy для версий снэпшотов.

        Оставляет только последние max_versions файлов.
        Более старые — удаляются. Предотвращает бесконечный рост диска.

        Args:
            versions_dir: Директория с версиями (v1.json, v2.json, ...)
            max_versions: Максимальное количество хранимых версий
        """
        try:
            version_files = sorted(
                versions_dir.glob("v*.json"),
                key=lambda p: self._extract_version_number(p.name),
                reverse=True
            )

            if len(version_files) > max_versions:
                to_delete = version_files[max_versions:]
                for old_file in to_delete:
                    old_file.unlink(missing_ok=True)
                logger.debug(
                    f"Retention: deleted {len(to_delete)} old versions in {versions_dir.name}, "
                    f"kept {max_versions}"
                )
        except Exception as e:
            logger.debug(f"Retention policy error: {e}")

    @staticmethod
    def _extract_version_number(filename: str) -> int:
        """Извлечь номер версии из имени файла (v1.json → 1)."""
        import re
        match = re.search(r"v(\d+)", filename)
        return int(match.group(1)) if match else 0

    def is_enabled(self) -> bool:
        """Проверить включены ли расширенные снапшоты."""
        try:
            from backend.core.config import flags
            return flags.enable_enhanced_snapshots
        except ImportError:
            return False

    _FALLBACK_COMPANY_NAMES = {"", "компания", "наша компания", "company"}

    @classmethod
    def _snapshot_degraded(cls, new: "CompanySnapshot", old: "CompanySnapshot",
                           strict: bool = False) -> bool:
        """True, если новый company-снапшот — деградация относительно старого.

        Признаки: у старого осмысленное имя, а у нового — fallback-заглушка;
        либо у нового пропали и люди, и продукты, которые у старого были
        (типичный след пустого графа или упавшего LLM-обогащения).

        strict=True (АВТО-регенерация: TTL/маркер, без явной воли юзера):
        потеря КОМАНДЫ — уже деградация. Реальный случай: TTL сработал в
        первые секунды после старта, граф ещё не прогрет → генерация увидела
        0 людей, LLM по пустому контексту заново «угадал» имя компании и
        затёр хороший снапшот («Meflow» у Пустоваловой). Пустая команда при
        непустой старой = граф в этот момент недоступен → выбросить новый
        снапшот целиком. Ручная «Пересобрать» проходит без strict."""
        try:
            old_name = (old.name or "").strip().lower()
            new_name = (new.name or "").strip().lower()
            old_has_name = old_name not in cls._FALLBACK_COMPANY_NAMES
            if old_has_name and new_name in cls._FALLBACK_COMPANY_NAMES:
                return True
            if (old.key_people or old.products) and not (new.key_people or new.products):
                return True
            if strict and old.key_people and not new.key_people:
                return True
            return False
        except Exception:
            return False

    async def get_company_snapshot(self, force_regenerate: bool = False) -> CompanySnapshot:
        """
        Получить снапшот компании.

        Если снапшота нет или он устарел, генерирует новый.
        Args:
            force_regenerate: Принудительно пересоздать (объединить с текущим)
        """
        if not self._company_snapshot:
            # Если в памяти нет, пробуем загрузить
            self._load_snapshots()

        # _dirty выставляется invalidate_snapshot_cache() / маркером после sync:
        # на диске устаревший снапшот, перегенерировать ОДИН раз (иначе страница
        # «Компания» показывала старое даже после рестарта процесса).
        effective_force = self._should_regen("__company__", force_regenerate)
        # TTL-подстраховка: даже без инвалидации снапшот не должен «застыть»
        # навсегда (жалоба «пересобрался только после кнопки Пересобрать»).
        # Без графа TTL-реген не запускаем: генерация гарантированно увидит
        # пустоту и только сожжёт LLM-вызов (guard всё равно её выбросит).
        if (not effective_force and self._company_snapshot is not None
                and self.graph is not None):
            effective_force = self._ttl_stale(
                "__company__", getattr(self._company_snapshot, "last_updated", ""))

        if not self._company_snapshot or effective_force:
            # Метка расходов для on-demand регенерации (вне generate_all):
            # без неё вызов из страницы «Компания» падает в «unknown».
            from backend.core.llm.usage_tracker import UsageContext
            async with UsageContext(agent_mode="snapshots",
                                    request_type="company_snapshot",
                                    user_id=self.user_id or None):
                new_snapshot = await self._generate_company_snapshot()

            # Guard «не перезаписывать хорошее худшим»: если LLM-провайдер упал
            # (403/квота) генерация возвращает fallback-заглушку (name=«Компания»
            # / «Наша компания», пустая команда) — а merge затянул бы её поверх
            # осмысленного снапшота. Деградированный новый при живом старом
            # игнорируем: старый остаётся до следующего успешного прогона.
            # АВТО-реген (TTL/маркер, не ручной) — строже: потеря команды при
            # непрогретом графе тоже деградация (см. _snapshot_degraded).
            if self._company_snapshot and self._snapshot_degraded(
                    new_snapshot, self._company_snapshot,
                    strict=not force_regenerate):
                logger.warning(
                    "⚠️ Новый company-снапшот деградирован (пустой граф/LLM "
                    "fallback?) — сохраняю прежний, перезапись пропущена")
            elif self._company_snapshot:
                # Если уже был снепшот, объединяем с новым (Smart Merge)
                # Это защищает от потери описания, если LLM не сработал
                self._company_snapshot.merge(new_snapshot)
            else:
                self._company_snapshot = new_snapshot

            # Наложить ручные правки ДО сохранения — иначе сохранённая версия
            # (и вся история/таймлайн) хранит LLM-догадку CEO/состава, а не правку
            # пользователя, и «неверный основатель» возвращается при каждом регене.
            self._apply_company_overrides(self._company_snapshot)
            # Удалённые сущности не должны пережить merge со старым снапшотом
            self._strip_tombstoned(self._company_snapshot)
            self._save_company_snapshot()
            self._mark_regen_done("__company__")
            # свежие данные подхвачены → снимаем персистентный маркер
            self._clear_regen_marker()

        # Ручные правки накладываются ПОВЕРХ генерации (источник истины —
        # _company_overrides), поэтому правка CEO/состава не перетирается.
        if self._company_snapshot is not None:
            self._apply_company_overrides(self._company_snapshot)
            # Старый снапшот с диска мог быть сохранён ДО удаления сущности
            self._strip_tombstoned(self._company_snapshot)
        return self._company_snapshot

    async def get_company_snapshot_text(self) -> str:
        """Получить текстовое представление снапшота компании."""
        snapshot = await self.get_company_snapshot()
        return snapshot.to_text()

    def _org_membership(self, name: str) -> str:
        """'head' | 'member' | '' — есть ли человек в оргструктуре компании.

        Оргструктура (cs.departments: members/head) правится пользователем и
        строится по реальным отделам — это самый надёжный сигнал «свой»,
        сильнее текста роли (у КПД каждый гость — «CEO/директор»)."""
        try:
            cs = self._company_snapshot
            if not cs or not getattr(cs, "departments", None):
                return ""
            nm = (name or "").strip().lower()
            if not nm:
                return ""
            first = nm.split()[0] if nm.split() else nm
            for d in cs.departments:
                head = (d.get("head") or "").strip().lower()
                if head and (nm in head or head in nm or head.split()[0] == first):
                    return "head"
                for m in (d.get("members") or []):
                    ml = (m or "").strip().lower()
                    if ml and (nm in ml or ml in nm):
                        return "member"
            return ""
        except Exception:
            return ""

    async def get_all_people_profiles(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Лёгкие профили всех людей (id/name/role/department) для списков/Team360.

        Раньше метод был только у старого SnapshotGenerator → Team360 на
        EnhancedSnapshotGenerator падал с AttributeError. Логика идентичная:
        Person-узлы через find_nodes_by_label с tenant-фильтром (важно —
        не тянуть людей из чужих аккаунтов), сортировка по вовлечённости.
        """
        if not self.graph:
            return []

        nodes: List[Dict[str, Any]] = []
        try:
            nodes = await self.graph.find_nodes_by_label(
                "Person", limit=2000, tenant_id=tenant_id, strict_tenant=True
            )
        except Exception as e:
            logger.debug(f"find_nodes_by_label(Person) failed: {e}")

        # ФЕДЕРАТИВНО: добираем людей из merged-графа (personal ∪ org). Раньше был
        # только personal-strict → org-промоутнутые сотрудники (например, ещё без
        # своего отдела, как «Алексей») не попадали в /people, Team360 и оргсхему,
        # хотя person-снепшот у них есть.
        have = {n.get("id") for n in nodes if n.get("id")}
        _nx = getattr(self.graph, "nx_graph", None)
        if _nx is not None:
            for nid, data in _nx.nodes(data=True):
                if data.get("_label") == "Person" and nid not in have:
                    nodes.append({"id": nid, **data}); have.add(nid)
        elif getattr(self.graph, "driver", None):
            try:
                # Neo4j: tenant_id=None → tenant_context (org_or_user), не strict.
                more = await self.graph.find_nodes_by_label(
                    "Person", limit=2000, tenant_id=None, strict_tenant=False)
                for n in more:
                    nid = n.get("id")
                    if nid and nid not in have:
                        nodes.append(n); have.add(nid)
            except Exception as e:
                logger.debug(f"federated Person read failed: {e}")

        profiles: List[Dict[str, Any]] = []
        seen: set = set()
        for n in nodes:
            name = (n.get("name") or "").strip()
            if not name:
                continue
            # Прячем составные/generic/тайм-фрагменты («Все участники», «Speaker 1»,
            # «Александр и Екатерина», «Катя 1:11:57») из списка людей/Team360 —
            # они не реальные сотрудники. Дедуп их не всегда сливает, а показывать
            # не нужно. Фильтр view-уровня: узлы в графе не трогаем.
            if _is_person_junk(name):
                continue
            pid = n.get("id") or n.get("person_id") or ""
            key = pid or name.lower()
            if key in seen:
                continue
            seen.add(key)
            role = n.get("role") or ""
            engagement = n.get("total_mentions", 0) or 0
            # Категория для группировки списка (руководство/сотрудники/партнёры/
            # клиенты/кандидаты/внешние) — единый каскад _categorize_person
            # (override > оргструктура > роль; чужие организации → external).
            category = self._categorize_person(
                pid, name, role,
                description=n.get("description") or "",
                department=n.get("department") or "",
                engagement=engagement)
            profiles.append({
                "person_id": pid,
                "id": pid,
                "name": name,
                "role": role,
                "department": n.get("department") or "",
                "category": category,
                "engagement_score": n.get("total_mentions", 0) or 0,
            })

        profiles.sort(key=lambda x: x.get("engagement_score", 0), reverse=True)
        return profiles

    async def _resolve_person_key(self, name_or_id: str) -> str:
        """Резолвит ИМЯ человека («Шитов») в реальный person_id узла.

        Снэпшоты ключуются по id (entity_<meeting>_<name> / canonical_id), а
        поиск/чат часто приходит с сырым именем. Без резолва get_person_snapshot
        возвращал None и вопросы про людей падали в десятки чанк-поисков вместо
        готового профиля. Матчим по: точному id → точному имени → вхождению
        (фамилия). Tenant-scoped через get_all_people_profiles.
        """
        if not name_or_id:
            return name_or_id
        if name_or_id in self._person_snapshots:
            return name_or_id
        try:
            people = await self.get_all_people_profiles(tenant_id=self.user_id or None)
        except Exception:
            people = []
        if not people:
            return name_or_id
        for p in people:  # 1) уже точный id
            if p.get("person_id") == name_or_id or p.get("id") == name_or_id:
                return name_or_id
        low = name_or_id.strip().lower()
        for p in people:  # 2) точное имя
            if (p.get("name") or "").strip().lower() == low:
                return p.get("person_id") or p.get("id") or name_or_id
        for p in people:  # 3) вхождение (фамилия ⊂ полное имя)
            nm = (p.get("name") or "").strip().lower()
            if low and nm and (low in nm or nm in low):
                return p.get("person_id") or p.get("id") or name_or_id
        return name_or_id

    async def get_person_snapshot(self, person_id: str, force_regenerate: bool = False) -> Optional[PersonSnapshot]:
        """
        Получить снапшот человека.

        Args:
            person_id: ID человека ИЛИ имя (резолвится в id)
            force_regenerate: Принудительно пересоздать

        Returns:
            PersonSnapshot или None
        """
        person_id = await self._resolve_person_key(person_id)
        if person_id not in self._person_snapshots:
            # Диск может быть свежее памяти: файлы снапшотов пишут и ночная
            # консолидация, и свежие генераторы других роутов, а кэшированный
            # singleton читает каталог один раз при создании. Реальный кейс:
            # иерархия (свежий генератор) показывает сотрудника, клик по нему
            # (singleton) отвечает «не найден в графе знаний».
            self._reload_person_snapshot(person_id)
        if person_id not in self._person_snapshots or self._should_regen(f"person:{person_id}", force_regenerate):
            new_snapshot = await self._generate_person_snapshot(person_id)
            if new_snapshot:
                if person_id in self._person_snapshots:
                    self._person_snapshots[person_id].merge(new_snapshot)
                else:
                    self._person_snapshots[person_id] = new_snapshot
                self._save_person_snapshot(person_id)
                self._mark_regen_done(f"person:{person_id}")

        snap = self._person_snapshots.get(person_id)
        if snap is not None:
            self._apply_person_overrides(person_id, snap)
        return snap

    async def get_person_snapshot_text(self, person_id: str) -> str:
        """Получить текстовое представление снапшота человека."""
        snapshot = await self.get_person_snapshot(person_id)
        if snapshot:
            return snapshot.to_text()
        return f"Снапшот для {person_id} не найден"

    async def get_project_snapshot(self, project_id: str, force_regenerate: bool = False) -> Optional[ProjectSnapshot]:
        """
        Получить снапшот проекта.

        Args:
            project_id: ID проекта
            force_regenerate: Принудительно пересоздать

        Returns:
            ProjectSnapshot или None
        """
        if project_id not in self._project_snapshots or self._should_regen(f"project:{project_id}", force_regenerate):
            new_snapshot = await self._generate_project_snapshot(project_id)
            if new_snapshot:
                if project_id in self._project_snapshots:
                    self._project_snapshots[project_id].merge(new_snapshot)
                else:
                    self._project_snapshots[project_id] = new_snapshot
                self._save_project_snapshot(project_id)
                self._mark_regen_done(f"project:{project_id}")

        return self._project_snapshots.get(project_id)

    async def get_project_snapshot_text(self, project_id: str) -> str:
        """Получить текстовое представление снапшота проекта."""
        snapshot = await self.get_project_snapshot(project_id)
        if snapshot:
            return snapshot.to_text()
        return f"Снапшот для проекта {project_id} не найден"

    def _save_project_snapshot(self, project_id: str):
        """Сохранить снапшот проекта с историей версий."""
        if project_id not in self._project_snapshots:
            return
        snap = self._project_snapshots[project_id]
        self._save_snapshot_with_versioning("projects", project_id, snap.to_dict(), snap.version)

    async def update_from_meeting(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Обновить снапшоты на основе данных встречи.

        Args:
            meeting_id: ID встречи
            meeting_data: Извлечённые данные встречи

        Returns:
            Статистика обновления
        """
        stats = {
            "company_updated": False,
            "persons_updated": [],
            "projects_updated": [],
        }

        if not self.is_enabled():
            return stats

        try:
            # Обновляем снапшот компании
            if self._company_snapshot:
                await self._update_company_from_meeting(meeting_data)
                stats["company_updated"] = True

            # Обновляем снапшоты участников
            participants = meeting_data.get("participants", [])
            for p in participants:
                person_id = p.get("person_id") or p.get("id") or p.get("name", "").lower().replace(" ", "_")
                if person_id and person_id in self._person_snapshots:
                    await self._update_person_from_meeting(person_id, meeting_data)
                    stats["persons_updated"].append(person_id)

            # ═══ Step 3: Индексация снэпшотов в Qdrant после обновления ═══
            await self._index_snapshots_to_qdrant()

            logger.info(f"Snapshots updated from meeting {meeting_id}")

            return stats

        except Exception as e:
            logger.error(f"Failed to update snapshots from meeting: {e}")
            return stats

    async def _index_snapshots_to_qdrant(self):
        """Step 3: Индексировать все снэпшоты в Qdrant для поиска."""
        try:
            from backend.core.store.vector_indexer import VectorIndexer
            vi = VectorIndexer()
            await vi.connect()

            # Индексируем Company Snapshot
            if self._company_snapshot:
                cs = self._company_snapshot
                if not cs.snippet:
                    cs.generate_snippet()

                text = f"{cs.snippet}. {cs.to_text(max_length=1500)}"
                await vi.upsert_vector_public(
                    collection=vi.collections.get("snapshots", "tessent_snapshots"),
                    vector_id=f"snapshot_company_{self.user_id}",
                    text=text,
                    metadata={
                        "snapshot_type": "company",
                        "entity_id": "company",
                        "snippet": cs.snippet,
                        "version": cs.version,
                        "hierarchy_level": 0,
                        "user_id": self.user_id,
                        "type": "snapshot",
                        "access_level": cs.access_level,
                        "delta_summary": cs.delta_summary,
                    }
                )

            # Индексируем Person Snapshots
            for person_id, ps in self._person_snapshots.items():
                snippet = f"{ps.name}: {ps.role}" if hasattr(ps, 'role') else ps.name if hasattr(ps, 'name') else person_id
                text = f"{snippet}. {ps.to_text(max_length=1000)}"
                await vi.upsert_vector_public(
                    collection=vi.collections.get("snapshots", "tessent_snapshots"),
                    vector_id=f"snapshot_person_{person_id}",
                    text=text,
                    metadata={
                        "snapshot_type": "person",
                        "entity_id": person_id,
                        "snippet": snippet,
                        "version": getattr(ps, 'version', 1),
                        "hierarchy_level": 3,
                        "user_id": self.user_id,
                        "type": "snapshot",
                    }
                )

            await vi.close()
            logger.debug(f"📸 Snapshots indexed to Qdrant: company + {len(self._person_snapshots)} persons")

        except Exception as e:
            logger.warning(f"Failed to index snapshots to Qdrant (non-critical): {e}")

    def get_snapshot_hierarchy(self) -> Dict[str, Any]:
        """Step 3: Вернуть иерархию снэпшотов с snippets для навигации.

        Возвращает два представления:
        1. Плоские списки (departments, persons, projects) — для обратной совместимости
        2. tree — навигационное дерево Company → Departments → People/Projects
        """
        hierarchy = {
            "company": None,
            "departments": [],
            "teams": [],
            "persons": [],
            "projects": [],
            "tree": None,  # Навигационная диаграмма
        }

        cs = self._company_snapshot

        if cs:
            if not cs.snippet:
                cs.generate_snippet()

            company_name = cs.name or "Компания"

            hierarchy["company"] = {
                "snapshot_id": f"snapshot_company_{self.user_id}",
                "name": company_name,
                "snippet": cs.snippet,
                "version": cs.version,
                "last_updated": cs.last_updated,
                "delta_summary": cs.delta_summary,
                "health_score": cs.health_score,
                "industry": cs.industry or "",
                "stage": cs.stage or "",
            }

            # Departments из company snapshot (теперь с members)
            for dept in cs.departments:
                dept_name = dept.get("name", "")
                hierarchy["departments"].append({
                    "name": dept_name,
                    "snippet": f"{dept_name}: {(dept.get('description') or '')[:80]}",
                    "head": dept.get("head", ""),
                    "employees_count": dept.get("employees_count", 0),
                    "members": dept.get("members", []),
                })

            # Projects из company snapshot — с project_id
            for proj in cs.active_projects:
                proj_name = proj.get("name", "")
                proj_id = proj_name.lower().replace(" ", "_") if proj_name else ""
                hierarchy["projects"].append({
                    "project_id": f"entity_project_{proj_id}" if proj_id else "",
                    "name": proj_name,
                    "snippet": f"Проект {proj_name}: {proj.get('status', 'active')}",
                    "status": proj.get("status", "active"),
                    "progress": proj.get("progress", ""),
                    "lead": proj.get("lead", ""),
                    "description": (proj.get("description") or "")[:120],
                })

            # Key people из company snapshot (если person snapshots пусты)
            key_people_map = {}
            for kp in (cs.key_people or []):
                kp_name = kp.get("name", "")
                if kp_name:
                    key_people_map[kp_name.lower()] = kp

        # Persons — из person_snapshots + key_people
        person_department_map = {}  # name.lower → department_name

        # Строим карту person → department из departments.members
        for dept_data in hierarchy["departments"]:
            dept_name = dept_data.get("name", "")
            for member_name in dept_data.get("members", []):
                if member_name:
                    person_department_map[member_name.lower()] = dept_name

        for person_id, ps in self._person_snapshots.items():
            name = getattr(ps, 'name', person_id)
            # Мусор/составные/тайм-фрагменты не показываем в навигации — старые
            # снэпшоты таких «людей» могли остаться на диске с прежних прогонов.
            if _is_person_junk(name):
                continue
            role = getattr(ps, 'role', '')
            department = getattr(ps, 'department', '') or person_department_map.get(name.lower(), "")
            _cat = self._categorize_person(person_id, name, role,
                                           department=department)
            hierarchy["persons"].append({
                "snapshot_id": f"snapshot_person_{person_id}",
                "person_id": person_id,
                "name": name,
                "snippet": f"{name}: {role}" if role else name,
                "role": role,
                "department": department,
                "category": _cat,
                "version": getattr(ps, 'version', 1),
            })

        # СЛИВАЕМ key_people из company snapshot с уже собранными (из
        # person_snapshots). Раньше стояло `if not self._person_snapshots` —
        # т.е. 20 человек компании добавлялись ТОЛЬКО когда кэш снэпшотов пуст;
        # стоило появиться 2 сгенерированным — иерархия показывала «2 чел»
        # вместо реальных 20. Теперь дедупим по имени и добавляем недостающих.
        if cs:
            _existing_names = {
                (p.get("name") or "").strip().lower() for p in hierarchy["persons"]
            }
            for kp in (cs.key_people or []):
                kp_name = kp.get("name", "")
                if not kp_name or kp_name.strip().lower() in _existing_names:
                    continue
                if _is_person_junk(kp_name):
                    continue
                _existing_names.add(kp_name.strip().lower())
                kp_id = f"person_{kp_name.lower().replace(' ', '_')}"
                department = person_department_map.get(kp_name.lower(), "")
                hierarchy["persons"].append({
                    "snapshot_id": f"snapshot_person_{kp_id}",
                    "person_id": kp_id,
                    "name": kp_name,
                    "snippet": f"{kp_name}: {kp.get('role', '')}",
                    "role": kp.get("role", ""),
                    "department": department,
                    "version": 0,
                    "from_key_people": True,
                })

        # ═══ Build navigation tree ═══
        # Company → Departments → [People, Projects]
        # Also: ungrouped people/projects at company level

        tree_children = []
        used_person_ids = set()
        used_project_names = set()

        # Departments as tree nodes
        for dept_data in hierarchy["departments"]:
            dept_name = dept_data.get("name", "")
            dept_persons = []
            dept_projects = []

            # Persons in this department
            for p in hierarchy["persons"]:
                if (p.get("department") or "").lower() == dept_name.lower():
                    dept_persons.append({
                        "type": "person",
                        "id": p["person_id"],
                        "name": p.get("name", ""),
                        "role": p.get("role", ""),
                        "snippet": p.get("snippet", ""),
                    })
                    used_person_ids.add(p["person_id"])

            # Projects led by department head or members
            dept_members_lower = {m.lower() for m in dept_data.get("members", [])}
            dept_members_lower.add(dept_data.get("head", "").lower())
            for proj in hierarchy["projects"]:
                lead_lower = (proj.get("lead") or "").lower()
                if lead_lower and lead_lower in dept_members_lower:
                    dept_projects.append({
                        "type": "project",
                        "id": proj.get("project_id", ""),
                        "name": proj.get("name", ""),
                        "status": proj.get("status", ""),
                        "snippet": proj.get("snippet", ""),
                    })
                    used_project_names.add(proj.get("name", "").lower())

            tree_children.append({
                "type": "department",
                "name": dept_name,
                "head": dept_data.get("head", ""),
                "snippet": dept_data.get("snippet", ""),
                "children": dept_persons + dept_projects,
                "count": len(dept_persons) + len(dept_projects),
            })

        # Ungrouped persons (no department)
        ungrouped_persons = []
        for p in hierarchy["persons"]:
            if p["person_id"] not in used_person_ids:
                ungrouped_persons.append({
                    "type": "person",
                    "id": p["person_id"],
                    "name": p.get("name", ""),
                    "role": p.get("role", ""),
                    "snippet": p.get("snippet", ""),
                })

        # Ungrouped projects
        ungrouped_projects = []
        for proj in hierarchy["projects"]:
            if proj.get("name", "").lower() not in used_project_names:
                ungrouped_projects.append({
                    "type": "project",
                    "id": proj.get("project_id", ""),
                    "name": proj.get("name", ""),
                    "status": proj.get("status", ""),
                    "snippet": proj.get("snippet", ""),
                })

        # Add ungrouped as a virtual department
        if ungrouped_persons or ungrouped_projects:
            tree_children.append({
                "type": "group",
                "name": "Без отдела",
                "head": "",
                "snippet": "Сотрудники и проекты без привязки к отделу",
                "children": ungrouped_persons + ungrouped_projects,
                "count": len(ungrouped_persons) + len(ungrouped_projects),
            })

        hierarchy["tree"] = {
            "type": "company",
            "name": (cs.name if cs else "Компания"),
            "snippet": hierarchy["company"]["snippet"] if hierarchy["company"] else "",
            "health_score": (cs.health_score if cs else 0),
            "children": tree_children,
            "total_persons": len(hierarchy["persons"]),
            "total_projects": len(hierarchy["projects"]),
            "total_departments": len(hierarchy["departments"]),
        }

        return hierarchy

    async def _generate_company_snapshot(self) -> CompanySnapshot:
        """
        Генерация снапшота компании из графа.

        NoCap-style: LLM анализирует ВСЕ данные из графа и формирует
        структурированный бизнес-профиль, фильтруя шум от реальных
        проектов/продуктов/людей.
        """
        snapshot = CompanySnapshot()
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()
        snapshot.update_source = "auto_generated"

        if not self.graph:
            return snapshot

        # ─── Шаг 1: Собираем ВСЕ данные из графа ───
        # strict-tenant: снапшот компании строим ТОЛЬКО из своих узлов. Иначе
        # NULL-tenant узлы других аккаунтов протекают (напр. «Алексей из другого
        # аккаунта», people=20 вместо реальной команды).
        _tid = self.user_id or None
        _strict = bool(_tid)
        all_entities = await self.graph.get_all_nodes_async(label="Entity", tenant_id=_tid, strict_tenant=_strict)
        projects_typed = await self.graph.get_all_nodes_async(label="Project", tenant_id=_tid, strict_tenant=_strict)
        persons = await self.graph.get_all_nodes_async(label="Person", tenant_id=_tid, strict_tenant=_strict)
        decisions = await self.graph.get_all_nodes_async(label="Decision", tenant_id=_tid, strict_tenant=_strict)
        tasks = await self.graph.get_all_nodes_async(label="Task", tenant_id=_tid, strict_tenant=_strict)
        meetings = await self.graph.get_all_nodes_async(label="Meeting", tenant_id=_tid, strict_tenant=_strict)
        regulations = await self.graph.get_all_nodes_async(label="Regulation", tenant_id=_tid, strict_tenant=_strict)
        await self.graph.get_all_nodes_async(label="Template", tenant_id=_tid, strict_tenant=_strict)

        # ─── Шаг 2: Предварительная классификация entities по типам ───
        entities_by_type: Dict[str, List[Dict[str, Any]]] = {}
        for e in all_entities:
            et = (e.get("entity_type") or "concept").lower().strip()
            entities_by_type.setdefault(et, []).append(e)

        # Типизированные проекты = Project nodes + Entity(project/product/service/platform)
        projects_all = list(projects_typed)
        seen_project_names = {p.get("name", "").lower() for p in projects_all}
        for et_key in ("project", "product", "service", "platform", "system"):
            for e in entities_by_type.get(et_key, []):
                name_lower = (e.get("name") or "").lower()
                if name_lower and name_lower not in seen_project_names:
                    projects_all.append(e)
                    seen_project_names.add(name_lower)

        # Типизированные люди = Person nodes + Entity(person/employee/...).
        # Отсекаем составные/generic/тайм-фрагменты (_is_person_junk): иначе
        # «Все участники», «маркдиры», «Александр и Екатерина», «Катя 1:11:57»
        # валятся в команду/партнёров компании.
        persons_all = [p for p in persons if not _is_person_junk(p.get("name"))]
        seen_person_names = {p.get("name", "").lower() for p in persons_all}
        # Entity(person) — имена, ПРОСТО УПОМЯНУТЫЕ в транскриптах (связь
        # MENTIONED_IN, не участие). «Лекун», «Илон», «Ишка» (ослышка ИИшки)
        # приходят именно отсюда. Помечаем: без ручной правки и без роли они
        # не могут стать «сотрудниками» — уйдут в related_people.
        mentioned_only_names: set = set()
        for et_key in ("person", "team_member", "employee", "speaker", "founder", "ceo"):
            for e in entities_by_type.get(et_key, []):
                name_lower = (e.get("name") or "").lower()
                if name_lower and name_lower not in seen_person_names and not _is_person_junk(e.get("name")):
                    persons_all.append(e)
                    seen_person_names.add(name_lower)
                    mentioned_only_names.add(name_lower)

        # Удалённые пользователем сущности (tombstones) не должны возвращаться
        # в снапшот НИ из какого источника (org-граф, старые данные, LLM) —
        # раньше tombstone блокировал только пересоздание узла, а генерация
        # читала граф без фильтра, и «Ишка» воскресал.
        persons_all = [p for p in persons_all
                       if not self._is_tombstoned_name(p.get("name"))]

        # Технологии
        tech_entities = []
        for et_key in ("technology", "tool", "framework", "language", "library", "infrastructure"):
            tech_entities.extend(entities_by_type.get(et_key, []))

        # Компании / организации
        company_entities = []
        for et_key in ("company", "organization", "startup", "corporation"):
            company_entities.extend(entities_by_type.get(et_key, []))

        # ─── Шаг 3: Простые (non-LLM) поля ───

        # Ключевые люди (из графа напрямую). person_id нужен фронту для
        # выбора человека и ручной классификации (external→internal).
        snapshot.key_people = [
            {
                "person_id": p.get("person_id") or p.get("id") or "",
                "name": p.get("name", "N/A"),
                "role": p.get("role") or p.get("description", "")[:60] or "",
            }
            for p in persons_all[:20]
            if p.get("name")
        ]

        # Технологии (из графа напрямую)
        snapshot.tech_stack = list({
            e.get("name", "") for e in tech_entities if e.get("name")
        })[:15]

        # Метрики (из графа напрямую — объективные данные)
        tasks_completed = len([t for t in tasks if t.get("status") in ("completed", "done")])
        tasks_total = len(tasks)

        if tasks_total > 0:
            snapshot.health_score = round((tasks_completed / tasks_total) * 100, 1)
        else:
            snapshot.health_score = min(100, len(all_entities) * 0.5)

        snapshot.kpis = [
            {"name": "Сущностей в базе знаний", "current_value": len(all_entities), "trend": "→"},
            {"name": "Встреч обработано", "current_value": len(meetings), "trend": "→"},
            {"name": "Решений принято", "current_value": len(decisions), "trend": "→"},
            {"name": "Регламентов", "current_value": len(regulations), "trend": "→"},
            {"name": "Людей в базе", "current_value": len(persons_all), "trend": "→"},
            {"name": "Проектов/Продуктов", "current_value": len(projects_all), "trend": "→"},
        ]

        # Реальные финансовые ряды (мост онтологии цифр, ночной шаг 12.5):
        # KPI-узлы с series — в отдельный блок карточки «Финансы».
        try:
            import json as _fjson
            _kpi_nodes = await self.graph.get_all_nodes_async(
                label="KPI", tenant_id=_tid, strict_tenant=_strict)
            _fin = []
            for k in _kpi_nodes:
                if not k.get("series"):
                    continue
                try:
                    _series = _fjson.loads(k["series"]) if isinstance(k["series"], str) else k["series"]
                except Exception:
                    continue
                _fin.append({
                    "name": k.get("name", ""),
                    "value": k.get("numeric_value"),
                    "period": k.get("period", ""),
                    "series": _series,
                    "source": k.get("source_dataset", ""),
                })
            snapshot.financial_kpis = _fin[:8]
        except Exception:
            logger.debug("financial kpis fetch skipped", exc_info=True)

        # Completed projects
        snapshot.completed_projects_count = len([
            p for p in projects_all if (p.get("status") or "").lower() in ("completed", "archived", "done")
        ])

        # ─── Шаг 4: LLM Enrichment — Главный промт для бизнес-профиля ───
        if self.llm:
            try:
                snapshot = await self._llm_enrich_company_snapshot(
                    snapshot, all_entities, entities_by_type,
                    projects_all, persons_all, company_entities,
                    tech_entities, decisions, meetings, tasks, regulations
                )
            except Exception as e:
                logger.error(f"❌ Failed to enrich snapshot with LLM: {e}")
                # Fallback: заполняем без LLM
                snapshot.name = "Компания"
                snapshot.active_projects = [
                    {
                        "name": p.get("name", "N/A"),
                        "status": p.get("status", "active"),
                        "description": (p.get("description") or "")[:100],
                    }
                    for p in projects_all[:10]
                ]
        else:
            # Без LLM — простое заполнение
            snapshot.name = "Компания"
            snapshot.active_projects = [
                {
                    "name": p.get("name", "N/A"),
                    "status": p.get("status", "active"),
                    "description": (p.get("description") or "")[:100],
                }
                for p in projects_all[:10]
            ]

        # ─── Шаг 5: Классификация людей — отделяем СОТРУДНИКОВ от партнёров/
        # внешних/кандидатов/клиентов. Раньше ВСЕ Person-узлы валились в
        # key_people («команда»), отсюда «Мартин-бизнес-ангел», «Видякин-зам
        # Грефа», «Ахлина-кандидат» среди сотрудников. ───
        try:
            desc_by_name = {}
            for p in persons_all:
                nm = (p.get("name") or "").strip().lower()
                if nm:
                    desc_by_name[nm] = (p.get("description") or "")
            employees: List[Dict[str, Any]] = []
            related: List[Dict[str, Any]] = []
            for kp in (snapshot.key_people or []):
                nm = (kp.get("name") or "").strip()
                role = kp.get("role") or ""
                desc = desc_by_name.get(nm.lower(), "")
                pid = kp.get("person_id") or kp.get("id") or ""
                # Ручная классификация имеет приоритет над авто (external→internal)
                override = self._classification_override_for(pid, nm)
                category = override or self._classify_person(role, desc)
                # Упомянутое имя без ручной правки не может быть «сотрудником»:
                # _classify_person при пустой роли/описании дефолтит в employee,
                # и упомянутые в разговоре люди становились командой компании.
                if (category == "employee" and not override
                        and nm.lower() in mentioned_only_names):
                    category = "external"
                if category == "employee":
                    employees.append(kp)
                else:
                    related.append({**kp, "category": category})
            snapshot.key_people = employees
            snapshot.related_people = related
        except Exception as e:
            logger.warning(f"person classification skipped: {e}")

        return snapshot

    @staticmethod
    def _classify_person(role: str, description: str = "") -> str:
        """Грубая классификация человека по тексту роли/описания.

        Возвращает: employee | partner | external | candidate | client.
        Цель — убрать из списка СОТРУДНИКОВ заведомых не-сотрудников. При
        неоднозначности возвращаем 'employee' (не over-filter'им реальных
        людей). Явные маркеры сотрудника (должность) перебивают слабые
        совпадения вроде «клиент» в «привлекает клиентов».
        """
        text = f"{role} {description}".lower()
        if not text.strip():
            return "employee"  # пусто — не выкидываем (может быть реальный сотрудник)

        # Члены советов директоров / спикеры-эксперты — это КЛИЕНТЫ/гости
        # бизнеса КПД, не команда. Проверяем ДО маркеров сотрудника: иначе
        # «член совета ДИРЕКТОРов» матчится на маркер «директор» → employee.
        if any(m in text for m in ("член совета", "председатель совета",
                                   "совет директоров", "board member",
                                   "профессиональный директор")):
            return "external"

        # Сильные маркеры сотрудника — если есть, считаем сотрудником.
        employee_markers = (
            "сотрудник", "штатн", "менеджер", "manager", "директор", "руковод",
            "ассистент", "assistant", "разработчик", "инженер", "engineer",
            "аналитик", "analyst", "дизайнер", "designer", "маркетолог",
            "продакт", "product manager", "project manager", "тимлид", "team lead",
            "head of", "cto", "cfo", "coo", "founder", "основатель",
        )
        if any(m in text for m in employee_markers):
            return "employee"

        # Не-сотрудники — по явным маркерам. «Управляющий партнёр» — это
        # руководитель компании, а не внешний партнёр: до общего маркера.
        if any(m in text for m in ("управляющий партнер", "управляющий партнёр",
                                   "managing partner")):
            return "employee"  # дальше _is_management_role поднимет в management
        if any(m in text for m in ("партнёр", "партнер", "partner")):
            return "partner"
        if any(m in text for m in ("кандидат", "candidate", "потенциальн",
                                   "рассматрива", "для вовлечения")):
            return "candidate"
        if any(m in text for m in ("клиент", "client", "заказчик")):
            return "client"
        if any(m in text for m in (
            "внешн", "эксперт", "expert", "бизнес-ангел", "ангел", "angel",
            "инвестор", "investor", "advisor", "консультант", "consultant",
            "ориентир", "зам ", "первый зам", "контакт", "владелец",
        )):
            return "external"
        return "employee"

    async def _llm_enrich_company_snapshot(
        self,
        snapshot: CompanySnapshot,
        all_entities: List[Dict],
        entities_by_type: Dict[str, List[Dict]],
        projects_all: List[Dict],
        persons_all: List[Dict],
        company_entities: List[Dict],
        tech_entities: List[Dict],
        decisions: List[Dict],
        meetings: List[Dict],
        tasks: List[Dict],
        regulations: List[Dict],
    ) -> CompanySnapshot:
        """
        LLM-обогащение снапшота компании.

        Один вызов LLM для анализа всех данных и формирования
        структурированного бизнес-профиля.
        """
        import re

        # Готовим подробный контекст для LLM
        context_sections = []

        # Entities сгруппированные по типам (топ-20 каждого типа)
        for etype, elist in sorted(entities_by_type.items()):
            if etype in ("concept",) and len(elist) > 30:
                # Для concept показываем только первые 15
                names = [e.get("name", "") for e in elist[:15] if e.get("name")]
                context_sections.append(f"Entity[{etype}] ({len(elist)} шт): {', '.join(names)}...")
            else:
                items = []
                for e in elist[:20]:
                    name = e.get("name", "")
                    desc = (e.get("description") or "")[:60]
                    if name:
                        items.append(f"{name}" + (f" ({desc})" if desc else ""))
                if items:
                    context_sections.append(f"Entity[{etype}] ({len(elist)}): {'; '.join(items)}")

        # Typed nodes
        if projects_all:
            items = []
            for p in projects_all[:15]:
                name = p.get("name", "")
                desc = (p.get("description") or "")[:60]
                status = p.get("status", "")
                items.append(f"{name}" + (f" [{status}]" if status else "") + (f" ({desc})" if desc else ""))
            context_sections.append(f"Проекты/Продукты ({len(projects_all)}): {'; '.join(items)}")

        if persons_all:
            items = []
            for p in persons_all[:15]:
                name = p.get("name", "")
                role = p.get("role", "")
                items.append(f"{name}" + (f" ({role})" if role else ""))
            context_sections.append(f"Люди ({len(persons_all)}): {'; '.join(items)}")

        if company_entities:
            names = [e.get("name", "") for e in company_entities if e.get("name")]
            context_sections.append(f"Компании/Организации: {', '.join(names)}")

        if meetings:
            titles = [m.get("title", "") for m in meetings[:10] if m.get("title")]
            context_sections.append(f"Встречи ({len(meetings)}): {', '.join(titles)}")

        if decisions:
            items = [(d.get("summary") or d.get("name") or d.get("text", ""))[:80] for d in decisions[:8]]
            context_sections.append(f"Решения ({len(decisions)}): {'; '.join(items)}")

        if tasks:
            items = [(t.get("title") or t.get("name", ""))[:50] for t in tasks[:8] if t.get("title") or t.get("name")]
            context_sections.append(f"Задачи ({len(tasks)}): {'; '.join(items)}")

        if regulations:
            names = [r.get("name", "") for r in regulations[:5] if r.get("name")]
            context_sections.append(f"Регламенты ({len(regulations)}): {', '.join(names)}")

        if tech_entities:
            names = list({e.get("name", "") for e in tech_entities if e.get("name")})
            context_sections.append(f"Технологии: {', '.join(names[:15])}")

        context_text = "\n".join(context_sections)

        prompt = f"""Ты — бизнес-аналитик. Проанализируй данные из базы знаний компании и создай СТРУКТУРИРОВАННЫЙ БИЗНЕС-ПРОФИЛЬ.

ДАННЫЕ ИЗ ГРАФА ЗНАНИЙ:
{context_text}

ВАЖНО — ПРОФИЛЬ ТОЛЬКО ИЗ ДАННЫХ ВЫШЕ:
- Каждое поле заполняй ТОЛЬКО фактами из «ДАННЫХ ИЗ ГРАФА ЗНАНИЙ». Ничего не
  добавляй из общих знаний и не выдумывай.
- Плейсхолдеры в JSON-шаблоне ниже («Название продукта», «Конкурент 1», «Название
  проекта» и т.п.) — это ПОДСКАЗКА ФОРМАТА, а НЕ данные. Никогда не переноси их в
  ответ. Нет реального значения → пустая строка/пустой массив, а не заглушка.
- name — название компании-владельца строго из данных. Если оно не следует
  однозначно из контекста, лучше оставь общее/пустое, чем угадывай правдоподобное
  имя. Не бери название внешней компании или продукта как имя компании.
- НЕ дублируй абстрактные concept-entities как "проекты". Проект/продукт — это КОНКРЕТНАЯ разработка, платформа, система, сервис, приложение.
- Отличай КОМПАНИЮ (владельца базы знаний, чьи встречи записаны) от ВНЕШНИХ компаний (клиенты, партнёры, конкуренты).
- ОСНОВАТЕЛЬ/CEO — определяй ТОЛЬКО по прямым сигналам: кого прямо называют основателем/CEO/владельцем/генеральным; кто принимает финальные решения; кому докладывают руководители; кто ставит задачи руководителям отделов. НЕ выбирай человека лишь потому, что он чаще всех упоминается или активно говорит — самый обсуждаемый участник часто НЕ руководитель. Если прямых сигналов нет — оставь поле пустым.
- Если данных для какого-то поля НЕ ХВАТАЕТ, оставь его пустым ("" или []), НЕ выдумывай.
- Все тексты на русском языке.

ЗАПОЛНИ JSON (ответь ТОЛЬКО JSON без markdown):
{{
    "name": "Название компании (определи из контекста)",
    "description": "Описание компании: чем занимается, что делает (2-3 предложения)",
    "industry": "Отрасль",
    "mission": "Миссия компании (если можно определить из целей и проектов)",
    "founded": "Год основания (если упоминается)",
    "location": "Локация (если упоминается)",
    "website": "Сайт (если упоминается)",
    "stage": "Стадия: idea/mvp/growth/scale/mature",

    "founder": {{
        "name": "Имя основателя/CEO",
        "role": "Роль",
        "background": "Краткий бэкграунд (если известен)"
    }},

    "products": [
        {{"name": "Название продукта/сервиса", "description": "Что это", "status": "active/development/planned", "target_audience": "Для кого"}}
    ],
    "business_model": "Бизнес-модель (B2B/B2C/SaaS/...)",
    "target_market": "Целевой рынок",
    "revenue_model": "Модель монетизации (если понятна)",
    "competitors": ["Конкурент 1"],

    "active_projects": [
        {{"name": "Название проекта", "status": "active/planning/completed", "description": "Краткое описание", "lead": "Руководитель (если известен)"}}
    ],

    "current_status": "growth/stable/restructuring/crisis",
    "current_priorities": ["Приоритет 1", "Приоритет 2"],
    "current_challenges": ["Вызов 1"],

    "strategic_goals": [
        {{"goal": "Цель", "timeframe": "Срок", "progress": 0}}
    ],

    "strengths": ["Сильная сторона 1"],
    "weaknesses": ["Слабая сторона 1"],
    "opportunities": ["Возможность 1"],
    "threats": ["Угроза 1"],

    "departments": [
        {{"name": "Название отдела/направления", "description": "Чем занимается", "head": "Руководитель (если известен)", "members": ["Имя сотрудника 1"]}}
    ],

    "resources": [
        {{"name": "Ресурс", "url": "", "type": "document/channel/tool"}}
    ]
}}"""

        response = await self.llm.generate(prompt)

        # Парсим JSON из ответа
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            logger.warning("LLM did not return valid JSON for company snapshot")
            snapshot.name = "Компания"
            return snapshot

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM JSON: {e}")
            snapshot.name = "Компания"
            return snapshot

        # ─── Заполняем snapshot из LLM-ответа ───

        # Базовые поля
        snapshot.name = data.get("name") or "Компания"
        snapshot.description = data.get("description", "")
        snapshot.industry = data.get("industry", "")
        snapshot.mission = data.get("mission", "")
        snapshot.founded = data.get("founded", "")
        snapshot.location = data.get("location", "")
        snapshot.website = data.get("website", "")
        snapshot.stage = data.get("stage", "")

        # Founder. Если пользователь поставил ручной оверрайд — LLM-догадку
        # даже не пишем в базовый снапшот (оверрайд и так наложится сверху,
        # но так founder не «мигает» между регенерациями в сохранённом файле).
        founder_data = data.get("founder")
        if (isinstance(founder_data, dict) and founder_data.get("name")
                and "founder" not in (self._company_overrides or {})):
            snapshot.founder = founder_data

        # Продукты (LLM-отфильтрованные реальные продукты/сервисы)
        products_data = data.get("products", [])
        if isinstance(products_data, list):
            snapshot.products = [
                {
                    "name": p.get("name", ""),
                    "description": p.get("description", ""),
                    "status": p.get("status", "active"),
                    "target_audience": p.get("target_audience", ""),
                }
                for p in products_data
                if isinstance(p, dict) and p.get("name")
            ]

        # Бизнес-модель
        snapshot.business_model = data.get("business_model", "")
        snapshot.target_market = data.get("target_market", "")
        snapshot.revenue_model = data.get("revenue_model", "")

        # Конкуренты
        competitors = data.get("competitors", [])
        if isinstance(competitors, list):
            snapshot.competitors = [c for c in competitors if isinstance(c, str) and c]

        # Проекты (LLM-отфильтрованные реальные проекты)
        active_projects_data = data.get("active_projects", [])
        if isinstance(active_projects_data, list):
            snapshot.active_projects = [
                {
                    "name": p.get("name", ""),
                    "status": p.get("status", "active"),
                    "description": p.get("description", ""),
                    "lead": p.get("lead", ""),
                }
                for p in active_projects_data
                if isinstance(p, dict) and p.get("name")
            ]

        # Статус и приоритеты
        snapshot.current_status = data.get("current_status", "")

        priorities = data.get("current_priorities", [])
        if isinstance(priorities, list):
            snapshot.current_priorities = [p for p in priorities if isinstance(p, str) and p]

        challenges = data.get("current_challenges", [])
        if isinstance(challenges, list):
            snapshot.current_challenges = [c for c in challenges if isinstance(c, str) and c]

        # Стратегические цели
        goals_data = data.get("strategic_goals", [])
        if isinstance(goals_data, list):
            snapshot.strategic_goals = [
                {
                    "goal": g.get("goal", ""),
                    "timeframe": g.get("timeframe", ""),
                    "progress": g.get("progress", 0),
                }
                for g in goals_data
                if isinstance(g, dict) and g.get("goal")
            ]

        # SWOT
        for field_name in ("strengths", "weaknesses", "opportunities", "threats"):
            val = data.get(field_name, [])
            if isinstance(val, list):
                setattr(snapshot, field_name, [v for v in val if isinstance(v, str) and v])

        # Ресурсы
        resources_data = data.get("resources", [])
        if isinstance(resources_data, list):
            snapshot.resources = [
                {
                    "name": r.get("name", ""),
                    "url": r.get("url", ""),
                    "type": r.get("type", "document"),
                }
                for r in resources_data
                if isinstance(r, dict) and r.get("name")
            ]

        # Departments (для навигационной диаграммы)
        departments_data = data.get("departments", [])
        if isinstance(departments_data, list):
            snapshot.departments = [
                {
                    "name": d.get("name", ""),
                    "description": d.get("description", ""),
                    "head": d.get("head", ""),
                    "employees_count": len(d.get("members", [])),
                    "members": d.get("members", []),
                }
                for d in departments_data
                if isinstance(d, dict) and d.get("name")
            ]

        logger.info(
            "✅ Company snapshot enriched with LLM: "
            f"name={snapshot.name}, products={len(snapshot.products)}, "
            f"projects={len(snapshot.active_projects)}, people={len(snapshot.key_people)}"
        )

        return snapshot

    async def _owned_meeting_ids(self, raw_ids: List[Any]) -> Optional[set]:
        """Множество id встреч (в формах id И meeting_id), реально принадлежащих
        self.user_id (сверка с таблицей meetings). None → проверить нельзя
        (нет user_id / ошибка запроса) → вызывающий НЕ режет список (fail-open).
        Защита от кросс-tenant контаминации графа: чужая встреча, попавшая в
        per-user граф прошлым импортом, не покажется в истории сотрудника."""
        if not self.user_id or not raw_ids:
            return None
        cands = set()
        for rid in raw_ids:
            s = str(rid or "").strip()
            if s.startswith("meeting_"):
                s = s[len("meeting_"):]
            if s:
                cands.add(s)
        if not cands:
            return None
        # id — UUID-колонка: в id.in.() кладём только UUID-подобные (иначе PostgREST
        # 400 на парсинге); meeting_id — текст, туда можно все кандидаты.
        _UUID = _re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        uuids = [c for c in cands if _UUID.fullmatch(c)]
        try:
            from backend.db.supabase_client import get_supabase_client
            clauses = []
            if uuids:
                clauses.append(f"id.in.({','.join(uuids)})")
            clauses.append(f"meeting_id.in.({','.join(cands)})")
            rows = await get_supabase_client()._request(
                "GET", "/rest/v1/meetings",
                params={"user_id": f"eq.{self.user_id}",
                        "or": f"({','.join(clauses)})",
                        "select": "id,meeting_id", "limit": "1000"})
        except Exception as e:
            logger.debug("owned meeting check failed (fail-open): %s", e)
            return None
        owned = set()
        for r in (rows or []):
            for k in ("id", "meeting_id"):
                v = str(r.get(k) or "").strip()
                if v:
                    owned.add(v)
        return owned

    async def _generate_person_snapshot(self, person_id: str) -> Optional[PersonSnapshot]:
        """Генерация снапшота человека из графа."""
        if not self.graph:
            return None

        # Нормализуем person_id для сравнения
        pid_lower = person_id.lower().strip()
        pid_underscore = pid_lower.replace(" ", "_")
        # Убираем prefix "person_" если есть — для сравнения с именами
        pid_name = pid_lower.removeprefix("person_").replace("_", " ")

        # Ищем узел человека. tenant_id — анти-утечка на ОБЩЕМ Neo4j: узел чужого
        # тенанта не возвращается. Без strict — свои legacy-узлы (без штампа)
        # сохраняются, отсекаются только явно чужие. self.user_id="" → None → как
        # раньше (без фильтра).
        _tid = self.user_id or None
        person_node_data = None

        # 1. Пробуем найти по точному ID
        node = await self.graph.get_node_by_id(person_id, tenant_id=_tid)
        if node and node.get("_label") in ("Person", "Entity"):
            person_node_data = node

        # 2. Если не нашли, ищем среди Person nodes
        if not person_node_data:
            persons = await self.graph.get_all_nodes_async(label="Person", tenant_id=_tid)
            for p in persons:
                p_id = (p.get("id") or "").lower()
                p_person_id = (p.get("person_id") or "").lower()
                p_name = (p.get("name") or "").lower()
                p_name_underscore = p_name.replace(" ", "_")

                if (p_id == pid_lower or
                    p_person_id == pid_lower or
                    p_name == pid_name or
                    p_name_underscore == pid_underscore or
                    f"person_{p_name_underscore}" == pid_lower):
                    person_node_data = p
                    break

        # 3. Fallback: ищем среди Entity nodes с entity_type=person
        if not person_node_data:
            all_entities = await self.graph.get_all_nodes_async(label="Entity", tenant_id=_tid)
            for e in all_entities:
                et = (e.get("entity_type") or "").lower()
                if et not in ("person", "employee", "speaker", "founder", "ceo"):
                    continue
                e_name = (e.get("name") or "").lower()
                e_name_underscore = e_name.replace(" ", "_")
                e_id = (e.get("id") or "").lower()

                if (e_id == pid_lower or
                    e_name == pid_name or
                    e_name_underscore == pid_underscore or
                    f"person_{e_name_underscore}" == pid_lower):
                    person_node_data = e
                    break

        # 4. Широкий поиск: search_nodes по имени
        if not person_node_data:
            try:
                search_results = await self.graph.search_nodes(query=pid_name, limit=10)
                for sr in search_results:
                    sr_name = (sr.get("name") or "").lower()
                    sr_id = sr.get("id")
                    if not sr_id:
                        continue  # Пропускаем узлы без id
                    if sr_name == pid_name or sr_name.replace(" ", "_") == pid_underscore:
                        person_node_data = sr
                        break
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # 5. Fallback на key_people из company snapshot
        if not person_node_data:
            if self._company_snapshot:
                for kp in (self._company_snapshot.key_people or []):
                    kp_name = (kp.get("name") or "").lower()
                    if kp_name == pid_name or kp_name.replace(" ", "_") == pid_underscore:
                        snapshot = PersonSnapshot()
                        snapshot.person_id = person_id
                        snapshot.name = kp.get("name", pid_name)
                        snapshot.role = kp.get("role", "")
                        snapshot.last_updated = datetime.now(timezone.utc).isoformat()
                        return snapshot
            return None

        node_id = person_node_data.get("id")
        attrs = person_node_data

        # Если узел найден, но без id — создаём базовый snapshot из свойств узла
        if not node_id:
            snapshot = PersonSnapshot()
            snapshot.person_id = person_id
            snapshot.name = attrs.get("name", pid_name)
            snapshot.role = attrs.get("role", "")
            snapshot.department = attrs.get("department", "")
            snapshot.last_updated = datetime.now(timezone.utc).isoformat()
            return snapshot

        snapshot = PersonSnapshot()
        snapshot.person_id = person_id
        snapshot.name = attrs.get("name", "N/A")
        snapshot.role = attrs.get("role", "")
        snapshot.department = attrs.get("department", "")
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()

        # Собираем связанные данные через API GraphBuilder
        rels = await self.graph.get_node_relationships(node_id)

        projects = []
        tasks = []
        decisions = []
        ideas = []
        opinions = []
        contradictions = []
        psychological: Dict[str, Any] = {}
        colleagues = set()

        # Исходящие связи. tenant_id на КАЖДОМ чтении связанного узла — вот тут и
        # протекала чужая встреча/задача: связь Person→Meeting на общем Neo4j
        # могла вести в узел другого тенанта, а get_node_by_id без фильтра его
        # возвращал. Теперь чужие штампованные узлы отсекаются.
        for rel in rels["outgoing"]:
            target_id = rel["target"]
            target_node = await self.graph.get_node_by_id(target_id, tenant_id=_tid)
            if not target_node: continue

            label = target_node.get("_label", "")

            if label == "Project":
                projects.append({
                    "project": target_node.get("name", "N/A"),
                    "role": rel.get("relation", "участник"),
                    "status": target_node.get("status", "active"),
                })
            elif label == "Task":
                tasks.append({
                    "task": target_node.get("title", target_node.get("name", "N/A")),
                    "status": target_node.get("status", "pending"),
                    "priority": target_node.get("priority", "normal"),
                })
            elif label == "Decision":
                # Появилось после подключения Person-DECIDED->Decision (атрибуция).
                decisions.append({
                    "summary": target_node.get("summary") or target_node.get("name", ""),
                    "category": target_node.get("decision_category", ""),
                    "date": target_node.get("created_at") or target_node.get("date") or "",
                })
            elif label == "Idea":
                _s = target_node.get("summary") or target_node.get("name", "")
                if _s:
                    ideas.append(_s)
            elif label == "Opinion":
                _s = target_node.get("summary") or target_node.get("topic") or target_node.get("name", "")
                if _s:
                    opinions.append({"summary": _s, "sentiment": target_node.get("sentiment", "")})
            elif label == "Contradiction":
                _s = (target_node.get("description") or target_node.get("summary")
                      or target_node.get("name", ""))
                if _s:
                    contradictions.append(_s)
            elif label == "PsychologicalProfile":
                psychological = {
                    "personality_type": target_node.get("personality_type") or "",
                    "team_role": target_node.get("team_role") or "",
                    "leadership_style": target_node.get("leadership_style") or "",
                    "communication_style": target_node.get("communication_style") or "",
                    "dominant_traits": target_node.get("dominant_traits") or [],
                    "strengths": target_node.get("strengths") or [],
                    "motivation_drivers": target_node.get("motivation_drivers") or [],
                    "weaknesses": target_node.get("weaknesses") or [],
                    "growth_areas": target_node.get("growth_areas") or target_node.get("development_areas") or [],
                }
            elif label == "Person":
                colleagues.add(target_node.get("name", ""))

        # Входящие связи
        for rel in rels["incoming"]:
            source_id = rel["source"]
            source_node = await self.graph.get_node_by_id(source_id, tenant_id=_tid)
            if not source_node: continue

            label = source_node.get("_label", "")

            if label == "Person":
                relation = rel.get("relation", "") or rel.get("type", "")
                if "manager" in relation.lower() or "руководитель" in relation.lower():
                    snapshot.manager = source_node.get("name", "")
                else:
                    colleagues.add(source_node.get("name", ""))

        snapshot.current_projects = projects[:5]
        # Балансируем: to-do и done группы обе должны дожить до карточки.
        # Иначе tasks[:10] в порядке обхода графа мог обрезать целую группу
        # (человек с 12 выполненными задачами → ни одной активной в срезе).
        _DONE_ST = ("completed", "done", "выполнено")
        _todo = [t for t in tasks if str(t.get("status", "")).lower() not in _DONE_ST]
        _done_tasks = [t for t in tasks if str(t.get("status", "")).lower() in _DONE_ST]
        snapshot.current_tasks = (_todo[:12] + _done_tasks[:12])[:20]
        snapshot.colleagues = list(colleagues)[:10]

        # Метрики
        snapshot.tasks_in_progress = len([t for t in tasks if t.get("status") == "in_progress"])
        snapshot.tasks_completed_week = len([t for t in tasks if t.get("status") in ("completed", "done")])

        # Извлекаем недавние митинги (связи Person → Meeting)
        meetings = []
        for rel in rels["outgoing"] + rels["incoming"]:
            target_or_source = rel.get("target") or rel.get("source")
            if not target_or_source:
                continue
            m_node = await self.graph.get_node_by_id(target_or_source, tenant_id=_tid)
            if m_node and m_node.get("_label") == "Meeting":
                meetings.append({
                    "meeting_id": m_node.get("id", ""),
                    "title": m_node.get("title", m_node.get("name", "Без названия")),
                    "date": m_node.get("date", m_node.get("meeting_date", "")),
                    "role_in_meeting": rel.get("relation", "участник"),
                })

        # Сортируем по дате (свежие первыми)
        meetings.sort(key=lambda m: m.get("date", ""), reverse=True)
        # Защита от кросс-tenant контаминации графа: оставляем ТОЛЬКО встречи,
        # реально принадлежащие этому пользователю. Прошлые импорты заносили в
        # per-user граф чужие встречи — они всплывали в истории сотрудника.
        # Fail-open: не смогли проверить (нет user_id/ошибка) → список не режем.
        owned = await self._owned_meeting_ids([m.get("meeting_id") for m in meetings])
        if owned is not None:
            def _mk(m: Dict[str, Any]) -> str:
                s = str(m.get("meeting_id") or "").strip()
                return s[len("meeting_"):] if s.startswith("meeting_") else s
            _before = len(meetings)
            meetings = [m for m in meetings if _mk(m) in owned]
            if _before != len(meetings):
                logger.warning("person snapshot %s: отброшено %d чужих встреч "
                               "(кросс-tenant защита)", person_id, _before - len(meetings))
        snapshot.recent_meetings = meetings[:10]
        snapshot.meetings_participated = len(meetings)

        # ─── Скоринг вклада (грунтуется на атрибутированных данных, НЕ выдумка) ───
        # decisions_made — реальные решения, привязанные к человеку (DECIDED).
        # collaboration_score (0-100) — прозрачная взвешенная сумма: решения
        # весят больше всего, плюс выполненные/активные задачи, участие во
        # встречах и связность в графе (degree как прокси центральности).
        snapshot.decisions_made = len(decisions)
        # Сохраняем СОДЕРЖИМОЕ (не только счётчики) — чтобы карточка показывала,
        # что человек реально решал/предлагал/высказывал, а не «2 решения».
        snapshot.decisions = decisions[:15]
        snapshot.ideas = ideas[:15]
        snapshot.opinions = opinions[:15]
        snapshot.contradictions = contradictions[:10]
        snapshot.psychological = psychological

        # Достижения и вызовы ИЗ РЕАЛЬНЫХ данных (раньше были LLM-поля и пустые).
        # Достижения: выполненные задачи + принятые решения. Вызовы:
        # заблокированные/просроченные задачи + противоречия, в которых замечен.
        _done = [t.get("task") for t in tasks
                 if str(t.get("status", "")).lower() in ("done", "completed", "выполнено")]
        _blocked = [t.get("task") for t in tasks
                    if str(t.get("status", "")).lower() in ("blocked", "overdue", "просрочено", "заблокировано")]
        _ach = [a for a in _done if a][:6] + [d.get("summary") for d in decisions[:4] if d.get("summary")]
        if _ach:
            snapshot.recent_achievements = _ach[:8]
        _chal = [b for b in _blocked if b][:5] + contradictions[:3]
        if _chal:
            snapshot.current_challenges = _chal[:8]

        # Зоны роста ИЗ РЕАЛЬНЫХ данных: growth_areas/weaknesses из психопрофиля
        # (если синтезированы), иначе мягкий сигнал из заблокированных задач.
        _growth = list(psychological.get("growth_areas") or [])
        _growth += [w for w in (psychological.get("weaknesses") or []) if w not in _growth]
        if _growth:
            snapshot.areas_for_improvement = [str(g) for g in _growth if g][:8]

        _degree = len(rels.get("outgoing", [])) + len(rels.get("incoming", []))
        _contribution = (
            len(decisions) * 8.0
            + snapshot.tasks_completed_week * 4.0
            + snapshot.tasks_in_progress * 2.0
            + snapshot.meetings_participated * 1.5
            + _degree * 0.5
        )
        snapshot.collaboration_score = round(min(100.0, _contribution), 1)

        # LLM-саммари активности (если есть LLM)
        try:
            from backend.core.llm.router import LLMRouter
            llm = LLMRouter()

            context_parts = [
                f"Имя: {snapshot.name}",
                f"Роль: {snapshot.role}",
                f"Отдел: {snapshot.department}",
            ]
            if projects:
                proj_names = ", ".join(p["project"] for p in projects[:5])
                context_parts.append(f"Проекты: {proj_names}")
            if tasks:
                active_tasks = [t["task"] for t in tasks[:5] if t.get("status") in ("in_progress", "pending")]
                if active_tasks:
                    context_parts.append(f"Активные задачи: {', '.join(active_tasks)}")
            if meetings:
                recent_titles = [m["title"] for m in meetings[:3]]
                context_parts.append(f"Недавние встречи: {', '.join(recent_titles)}")

            # Обогащение ПО ИМЕНИ: узел человека часто изолирован (имя без
            # связей), поэтому projects/tasks/meetings пусты, а карточка —
            # пустая («про X ничего нет»). Ищем упоминания имени по графу
            # (чанки/решения/задачи) и подкладываем их в саммари. Если
            # упоминаний реально нет — саммари остаётся честно пустым.
            mentions: List[str] = []
            try:
                name_l = (snapshot.name or "").lower().strip()
                if name_l and len(name_l) > 2:
                    hits = await self.graph.search_nodes(query=snapshot.name, limit=20)
                    for h in hits or []:
                        txt = (h.get("text") or h.get("description") or h.get("summary")
                               or h.get("title") or "")
                        if txt and name_l in txt.lower() and len(txt.strip()) > 30:
                            mentions.append(txt.strip()[:300])
                        if len(mentions) >= 8:
                            break
            except Exception:
                logger.debug("person mention search skipped", exc_info=True)
            if mentions:
                context_parts.append(
                    "Упоминания в данных:\n- " + "\n- ".join(mentions))

            context = "\n".join(context_parts)

            summary_prompt = f"""На основе данных сотрудника напиши краткое саммари (2-3 предложения)
о том, чем он сейчас занимается, над чем работает, какова его активность.

ТОЛЬКО по данным ниже: не выдумывай проекты, задачи, роли и достижения, которых
там нет. Если данных мало — короткое честное саммари ("данных пока немного"),
без домыслов. Нейтральный тон (саммари читают коллеги и сам человек).

{context}

Ответь только текстом саммари, без заголовков, на русском языке."""

            summary = await llm.generate(
                prompt=summary_prompt,
                system_prompt="Ты аналитик корпоративных данных. Пиши строго по данным, без домыслов.",
                temperature=0.3,
                max_tokens=200,
            )
            snapshot.activity_summary = summary.strip()
        except Exception as e:
            logger.debug(f"Could not generate activity summary: {e}")

        # CAPABILITY_READINESS C8 (вторая часть): merge psych-профилей
        # (PsychologicalProfile узлы графа) в strengths/communication_style/
        # delta_details.psych. Best-effort: нет узлов / нет графа → snapshot
        # не меняется (не выдумываем).
        try:
            from backend.core.sleep.psych_to_person import (
                filter_profiles_for_person,
                merge_psych_into_person,
            )
            all_nodes = await self.graph.get_all_nodes_async() if self.graph else []
            psych_profiles = filter_profiles_for_person(all_nodes, snapshot.name)
            if psych_profiles:
                merge_stats = merge_psych_into_person(snapshot, psych_profiles)
                if any(merge_stats.get(k) for k in
                       ("strengths_added", "traits_added", "motives_added",
                        "communication_style_set", "personality_type_set",
                        "team_role_set")):
                    logger.info(
                        f"🧠 psych→person merged for {snapshot.name}: {merge_stats}")
        except Exception as e:
            logger.debug(f"psych→person merge skipped: {e}")

        return snapshot

    async def _generate_project_snapshot(self, project_id: str) -> Optional[ProjectSnapshot]:
        """Генерация снапшота проекта из графа."""
        if not self.graph:
            return None

        # Нормализуем project_id для сравнения
        proj_lower = project_id.lower().strip()
        proj_underscore = proj_lower.replace(" ", "_")
        # Убираем prefix "entity_project_" если есть
        proj_name = proj_lower.removeprefix("entity_project_").removeprefix("project_").replace("_", " ")

        # Ищем узел проекта
        project_node_data = None

        # 1. Пробуем найти по точному ID
        node = await self.graph.get_node_by_id(project_id)
        if node and node.get("_label") in ("Project", "Entity"):
            project_node_data = node

        # 2. Ищем среди Project nodes
        if not project_node_data:
            projects = await self.graph.get_all_nodes_async(label="Project")
            for p in projects:
                p_id = (p.get("id") or "").lower()
                p_project_id = (p.get("project_id") or "").lower()
                p_name = (p.get("name") or "").lower()
                p_name_underscore = p_name.replace(" ", "_")

                if (p_id == proj_lower or
                    p_project_id == proj_lower or
                    p_name == proj_name or
                    p_name_underscore == proj_underscore):
                    project_node_data = p
                    break

        # 3. Fallback: ищем среди Entity nodes с project-like типами
        if not project_node_data:
            all_entities = await self.graph.get_all_nodes_async(label="Entity")
            best_entity_match = None
            best_entity_score = 0

            for e in all_entities:
                et = (e.get("entity_type") or "").lower()
                if et not in ("project", "product", "service", "platform", "system"):
                    continue
                e_name = (e.get("name") or "").lower()
                e_name_underscore = e_name.replace(" ", "_")
                e_id = (e.get("id") or "").lower()

                # Точное совпадение
                if (e_id == proj_lower or
                    e_name == proj_name or
                    e_name_underscore == proj_underscore or
                    f"entity_project_{e_name_underscore}" == proj_lower):
                    project_node_data = e
                    break

                # Нечёткое: contains или пересечение слов
                if proj_name in e_name or e_name in proj_name:
                    if best_entity_score < 80:
                        best_entity_match = e
                        best_entity_score = 80
                else:
                    stop_words = {"и", "в", "на", "для", "с", "из", "по", "к", "о", "а"}
                    p_words = set(proj_name.replace("-", " ").split()) - stop_words
                    e_words = set(e_name.replace("-", " ").split()) - stop_words
                    if p_words and e_words:
                        common = p_words & e_words
                        if not common:
                            for pw in p_words:
                                for ew in e_words:
                                    if len(pw) >= 4 and len(ew) >= 4 and pw[:4] == ew[:4]:
                                        common.add(pw)
                        score = len(common) / max(len(p_words), len(e_words)) * 100
                        if score > best_entity_score and score >= 40:
                            best_entity_match = e
                            best_entity_score = score

            if not project_node_data and best_entity_match:
                project_node_data = best_entity_match
                logger.info(
                    f"Entity matched via fuzzy (score={best_entity_score:.0f}): "
                    f"'{proj_name}' → '{best_entity_match.get('name', '')}'"
                )

        # 4. Широкий поиск: ищем ЛЮБОЙ Entity с таким именем (независимо от entity_type)
        if not project_node_data:
            try:
                # Поиск по ключевым словам из имени проекта
                search_results = await self.graph.search_nodes(query=proj_name, limit=10)
                for sr in search_results:
                    sr_name = (sr.get("name") or "").lower()
                    sr_id = sr.get("id")
                    if not sr_id:
                        continue  # Пропускаем узлы без id
                    # Точное или contains совпадение
                    if (sr_name == proj_name or
                        sr_name.replace(" ", "_") == proj_underscore or
                        proj_name in sr_name or
                        sr_name in proj_name):
                        project_node_data = sr
                        break
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # 5. Если узел не найден, но проект есть в company snapshot — виртуальный snapshot
        if not project_node_data:
            if self._company_snapshot:
                best_match = None
                best_score = 0

                for ap in (self._company_snapshot.active_projects or []):
                    ap_name = (ap.get("name") or "").lower()
                    ap_underscore = ap_name.replace(" ", "_")

                    # 5a. Точное совпадение
                    if ap_name == proj_name or ap_underscore == proj_underscore:
                        best_match = ap
                        best_score = 100
                        break

                    # 5b. Нечёткое сравнение: пересечение значимых слов (>= 50% общих)
                    # Решает проблему когда название проекта менялось между версиями снэпшотов
                    # Пример: "разработка системы генерации smm-плана" vs "генерация smm-планов"
                    stop_words = {"и", "в", "на", "для", "с", "из", "по", "к", "о", "а"}
                    proj_words = set(proj_name.replace("-", " ").split()) - stop_words
                    ap_words = set(ap_name.replace("-", " ").split()) - stop_words

                    if proj_words and ap_words:
                        intersection = proj_words & ap_words
                        # Stem-like: проверяем первые 4 символа для морфологических вариантов
                        if not intersection:
                            for pw in proj_words:
                                for aw in ap_words:
                                    if len(pw) >= 4 and len(aw) >= 4 and pw[:4] == aw[:4]:
                                        intersection.add(pw)

                        max_len = max(len(proj_words), len(ap_words))
                        score = len(intersection) / max_len * 100 if max_len > 0 else 0

                        if score > best_score and score >= 40:
                            best_match = ap
                            best_score = score

                    # 5c. Один содержит другой
                    if ap_name in proj_name or proj_name in ap_name:
                        if best_score < 80:
                            best_match = ap
                            best_score = 80

                if best_match:
                    snapshot = ProjectSnapshot()
                    snapshot.project_id = project_id
                    snapshot.name = best_match.get("name", proj_name)
                    snapshot.description = best_match.get("description", "")
                    snapshot.status = best_match.get("status", "active")
                    snapshot.lead = best_match.get("lead", "")
                    snapshot.progress = float(best_match.get("progress", 0) or 0)
                    snapshot.last_updated = datetime.now(timezone.utc).isoformat()
                    snapshot.health_score = 50.0  # Unknown
                    logger.info(
                        f"Project matched via fuzzy (score={best_score:.0f}): "
                        f"'{proj_name}' → '{best_match.get('name', '')}'"
                    )
                    return snapshot
            return None

        node_id = project_node_data.get("id")
        attrs = project_node_data

        # Если узел найден, но без id — создаём базовый snapshot из свойств узла
        if not node_id:
            snapshot = ProjectSnapshot()
            snapshot.project_id = project_id
            snapshot.name = attrs.get("name", proj_name)
            snapshot.description = attrs.get("description", "")
            snapshot.status = attrs.get("status", "active")
            snapshot.last_updated = datetime.now(timezone.utc).isoformat()
            return snapshot

        snapshot = ProjectSnapshot()
        snapshot.project_id = project_id
        snapshot.name = attrs.get("name", "N/A")
        snapshot.description = attrs.get("description", "")
        snapshot.status = attrs.get("status", "unknown")
        snapshot.progress = float(attrs.get("progress", 0))
        snapshot.lead = attrs.get("lead", attrs.get("owner", ""))
        snapshot.start_date = attrs.get("start_date", "")
        snapshot.deadline = attrs.get("deadline", attrs.get("end_date", ""))
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()

        # Собираем связанные данные
        team_members = []
        tasks = []
        decisions = []
        risks = []

        rels = await self.graph.get_node_relationships(node_id)

        # Исходящие связи от проекта
        for rel in rels["outgoing"]:
            target_id = rel["target"]
            target_node = await self.graph.get_node_by_id(target_id)
            if not target_node: continue

            label = target_node.get("_label", "")

            if label == "Person":
                team_members.append({
                    "name": target_node.get("name", "N/A"),
                    "role": rel.get("relation", target_node.get("role", "участник")),
                })
            elif label == "Task":
                tasks.append({
                    "title": target_node.get("title", target_node.get("name", "N/A")),
                    "status": target_node.get("status", "pending"),
                    "priority": target_node.get("priority", "normal"),
                    "assignee": target_node.get("assignee", ""),
                })
            elif label == "Decision":
                decisions.append({
                    "decision": target_node.get("text", target_node.get("title", "N/A")),
                    "date": target_node.get("date", ""),
                    "made_by": target_node.get("made_by", ""),
                })
            elif label == "Risk":
                risks.append({
                    "risk": target_node.get("description", target_node.get("name", "N/A")),
                    "severity": target_node.get("severity", "medium"),
                    "status": target_node.get("status", "open"),
                })

        # Входящие связи к проекту
        for rel in rels["incoming"]:
            source_id = rel["source"]
            source_node = await self.graph.get_node_by_id(source_id)
            if not source_node: continue

            label = source_node.get("_label", "")

            if label == "Person":
                relation = rel.get("relation", "") or rel.get("type", "")
                if "lead" in relation.lower() or "руководитель" in relation.lower() or "owner" in relation.lower():
                    snapshot.lead = source_node.get("name", "")
                else:
                    team_members.append({
                        "name": source_node.get("name", "N/A"),
                        "role": rel.get("relation", "участник"),
                    })
            elif label == "Task":
                tasks.append({
                    "title": source_node.get("title", source_node.get("name", "N/A")),
                    "status": source_node.get("status", "pending"),
                    "priority": source_node.get("priority", "normal"),
                })

        # Заполняем снапшот
        snapshot.team_members = team_members[:20]
        snapshot.recent_decisions = decisions[:10]
        snapshot.risks = risks[:10]

        # Метрики задач
        snapshot.tasks_total = len(tasks)
        snapshot.tasks_completed = len([t for t in tasks if t.get("status") in ("completed", "done")])
        snapshot.tasks_in_progress = len([t for t in tasks if t.get("status") == "in_progress"])
        snapshot.tasks_blocked = len([t for t in tasks if t.get("status") in ("blocked", "on_hold")])

        # Блокеры
        snapshot.blockers = [
            t.get("title", "N/A") for t in tasks
            if t.get("status") in ("blocked", "on_hold")
        ][:5]

        # Health score
        if snapshot.tasks_total > 0:
            completion_rate = snapshot.tasks_completed / snapshot.tasks_total
            blocked_rate = snapshot.tasks_blocked / snapshot.tasks_total
            snapshot.health_score = max(0, min(100, completion_rate * 100 - blocked_rate * 50))

        # LLM-выжимка «что с проектом»: этап, что хорошо/плохо, перспективы.
        # Только если по проекту реально есть данные (не плодим выдумки на пустоте).
        if decisions or tasks or (snapshot.description and len(snapshot.description) > 40):
            facts = []
            if snapshot.description:
                facts.append(f"Описание: {snapshot.description[:300]}")
            facts.append(
                f"Статус: {snapshot.status}; задач всего {snapshot.tasks_total}, "
                f"выполнено {snapshot.tasks_completed}, в работе "
                f"{snapshot.tasks_in_progress}, заблокировано {snapshot.tasks_blocked}")
            if snapshot.lead:
                facts.append(f"Лид: {snapshot.lead}")
            if team_members:
                facts.append("Команда: " + ", ".join(m["name"] for m in team_members[:8]))
            for d in decisions[:6]:
                facts.append(f"Решение: {str(d.get('decision'))[:150]}")
            for r in risks[:4]:
                facts.append(f"Риск ({r.get('severity')}): {str(r.get('risk'))[:120]}")
            if snapshot.blockers:
                facts.append("Блокеры: " + "; ".join(snapshot.blockers[:3]))
            snapshot.ai_summary = await self._llm_entity_summary(
                "проекта", snapshot.name, "\n".join(facts))

        return snapshot

    async def _llm_entity_summary(self, kind: str, name: str, facts: str) -> str:
        """Одна LLM-выжимка для карточки сущности (проект/отдел/продукт).

        Возвращает 3-5 предложений: этап, что идёт хорошо, что плохо/риски,
        перспективы. Пустая строка при недоступном LLM — карточка живёт без
        вердикта, факты из графа всё равно на месте (best-effort)."""
        if not self.llm:
            return ""
        try:
            prompt = (
                f"По фактам ниже напиши сводку состояния {kind} «{name}» для "
                "руководителя: 3-5 коротких предложений — на каком этапе, что "
                "идёт хорошо, что плохо/риски, ближайшие перспективы. Только по "
                "фактам, без выдумок и без воды. Русский язык, без заголовков.\n\n"
                f"Факты:\n{facts[:3000]}"
            )
            resp = await self.llm.generate(prompt=prompt, temperature=0.2, max_tokens=350)
            text = resp.get("text", "") if isinstance(resp, dict) else str(resp or "")
            return text.strip()[:1200]
        except Exception as e:
            logger.debug(f"entity summary LLM skipped for {name}: {e}")
            return ""

    # ════════════════════════════════════════════════════════════
    # PRODUCT / DEPARTMENT / TEAM — save, get, generate
    # ════════════════════════════════════════════════════════════

    def _save_snapshot_with_versioning(self, entity_type: str, entity_id: str, snapshot_dict: dict, version: int):
        """
        Универсальный метод: сохранить снапшот с историей версий.

        Args:
            entity_type: "products", "departments", "teams", "projects"
            entity_id: ID сущности
            snapshot_dict: Данные снапшота
            version: Текущая версия
        """
        entity_dir = self.storage_path / entity_type
        entity_dir.mkdir(exist_ok=True)
        file_path = entity_dir / f"{entity_id}.json"

        # Сохраняем предыдущую версию
        try:
            if file_path.exists() and version > 1:
                versions_dir = self.storage_path / "versions" / entity_type / entity_id
                versions_dir.mkdir(parents=True, exist_ok=True)
                version_file = versions_dir / f"v{version - 1}.json"
                if not version_file.exists():
                    import shutil
                    shutil.copy2(file_path, version_file)

                self._apply_retention_policy(versions_dir, max_versions=10)
        except Exception as e:
            logger.warning(f"Failed to save {entity_type} version history: {e}")

        # Сохраняем текущую версию
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(snapshot_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {entity_type} snapshot {entity_id}: {e}")

    def _save_product_snapshot(self, product_id: str):
        """Сохранить снапшот продукта с историей версий."""
        if product_id not in self._product_snapshots:
            return
        snap = self._product_snapshots[product_id]
        self._save_snapshot_with_versioning("products", product_id, snap.to_dict(), snap.version)

    def _save_department_snapshot(self, department_id: str):
        """Сохранить снапшот отдела с историей версий."""
        if department_id not in self._department_snapshots:
            return
        snap = self._department_snapshots[department_id]
        self._save_snapshot_with_versioning("departments", department_id, snap.to_dict(), snap.version)

    def _save_team_snapshot(self, team_id: str):
        """Сохранить снапшот команды с историей версий."""
        if team_id not in self._team_snapshots:
            return
        snap = self._team_snapshots[team_id]
        self._save_snapshot_with_versioning("teams", team_id, snap.to_dict(), snap.version)

    async def get_product_snapshot(self, product_id: str, force_regenerate: bool = False) -> Optional['ProductSnapshot']:
        """Получить снапшот продукта."""
        if product_id in self._product_snapshots and not force_regenerate:
            return self._product_snapshots[product_id]

        new_snap = await self._generate_product_snapshot(product_id)
        if new_snap:
            if product_id in self._product_snapshots:
                self._product_snapshots[product_id].merge(new_snap)
            else:
                self._product_snapshots[product_id] = new_snap
            self._save_product_snapshot(product_id)
        return self._product_snapshots.get(product_id)

    async def get_department_snapshot(self, department_id: str, force_regenerate: bool = False) -> Optional['DepartmentSnapshot']:
        """Получить снапшот отдела."""
        if department_id in self._department_snapshots and not force_regenerate:
            return self._department_snapshots[department_id]

        new_snap = await self._generate_department_snapshot(department_id)
        if new_snap:
            if department_id in self._department_snapshots:
                self._department_snapshots[department_id].merge(new_snap)
            else:
                self._department_snapshots[department_id] = new_snap
            self._save_department_snapshot(department_id)
        return self._department_snapshots.get(department_id)

    async def get_team_snapshot(self, team_id: str, force_regenerate: bool = False) -> Optional['TeamSnapshot']:
        """Получить снапшот команды."""
        if team_id in self._team_snapshots and not force_regenerate:
            return self._team_snapshots[team_id]

        new_snap = await self._generate_team_snapshot(team_id)
        if new_snap:
            if team_id in self._team_snapshots:
                self._team_snapshots[team_id].merge(new_snap)
            else:
                self._team_snapshots[team_id] = new_snap
            self._save_team_snapshot(team_id)
        return self._team_snapshots.get(team_id)

    async def _generate_product_snapshot(self, product_id: str) -> Optional['ProductSnapshot']:
        """Генерация снапшота продукта из графа."""
        if not self.graph:
            return None

        # Ищем узел продукта
        product_node = await self.graph.get_node_by_id(product_id)

        if not product_node:
            # Ищем среди Product и Entity nodes
            for label in ("Product", "Entity"):
                nodes = await self.graph.get_all_nodes_async(label=label)
                for n in nodes:
                    n_id = (n.get("id") or "").lower()
                    n_name = (n.get("name") or "").lower()
                    pid = product_id.lower()
                    if n_id == pid or n_name == pid.replace("_", " "):
                        product_node = n
                        break
                    # Entity с типом product
                    if label == "Entity" and n.get("entity_type", "").lower() in ("product", "service", "platform"):
                        if pid.replace("_", " ") in n_name or n_name in pid.replace("_", " "):
                            product_node = n
                            break
                if product_node:
                    break

        if not product_node:
            return None

        node_id = product_node.get("id", product_id)
        attrs = product_node

        snapshot = ProductSnapshot()
        snapshot.product_id = product_id
        snapshot.name = attrs.get("name", "N/A")
        snapshot.description = attrs.get("description", "")
        snapshot.product_type = attrs.get("product_type", attrs.get("entity_type", ""))
        snapshot.status = attrs.get("status", "active")
        snapshot.target_audience = attrs.get("target_audience", "")
        snapshot.owner = attrs.get("owner", attrs.get("lead", ""))
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()

        # Собираем связанные данные через relationships
        if node_id:
            try:
                rels = await self.graph.get_node_relationships(node_id)

                for rel in rels.get("outgoing", []) + rels.get("incoming", []):
                    target_id = rel.get("target", rel.get("source", ""))
                    if not target_id:
                        continue
                    related = await self.graph.get_node_by_id(target_id)
                    if not related:
                        continue

                    label = related.get("_label", "")
                    if label == "Person":
                        snapshot.team_members.append(related.get("name", "N/A"))
                    elif label == "Project":
                        snapshot.related_projects.append(related.get("name", "N/A"))
                    elif label == "KPI":
                        snapshot.kpis.append({
                            "name": related.get("name", "N/A"),
                            "value": related.get("numeric_value", related.get("value", "")),
                            "unit": related.get("unit", ""),
                        })
                    elif label == "Risk":
                        snapshot.risks.append(related.get("description", related.get("name", "N/A")))
            except Exception as e:
                logger.warning(f"Error getting product relationships: {e}")

        logger.info(f"📦 Generated product snapshot: {snapshot.name}")
        return snapshot

    async def _generate_department_snapshot(self, department_id: str) -> Optional['DepartmentSnapshot']:
        """Генерация снапшота отдела из графа."""
        if not self.graph:
            return None

        # Ищем узел отдела
        dept_node = await self.graph.get_node_by_id(department_id)

        if not dept_node:
            nodes = await self.graph.get_all_nodes_async(label="Department")
            for n in nodes:
                n_id = (n.get("id") or "").lower()
                n_name = (n.get("name") or "").lower()
                did = department_id.lower()
                if n_id == did or n_name == did.replace("_", " "):
                    dept_node = n
                    break

        if not dept_node:
            return None

        node_id = dept_node.get("id", department_id)
        attrs = dept_node

        snapshot = DepartmentSnapshot()
        snapshot.department_id = department_id
        snapshot.name = attrs.get("name", "N/A")
        snapshot.description = attrs.get("description", "")
        snapshot.head = attrs.get("head", attrs.get("lead", ""))
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()

        # Связи: команды, люди, проекты
        if node_id:
            try:
                rels = await self.graph.get_node_relationships(node_id)

                members = []
                teams = []
                projects = []

                for rel in rels.get("outgoing", []) + rels.get("incoming", []):
                    target_id = rel.get("target", rel.get("source", ""))
                    if not target_id:
                        continue
                    related = await self.graph.get_node_by_id(target_id)
                    if not related:
                        continue

                    label = related.get("_label", "")
                    if label == "Person":
                        members.append({
                            "name": related.get("name", "N/A"),
                            "role": related.get("role", ""),
                        })
                    elif label == "Team":
                        teams.append(related.get("name", "N/A"))
                    elif label == "Project":
                        projects.append(related.get("name", "N/A"))
                    elif label == "KPI":
                        snapshot.kpis.append({
                            "name": related.get("name", "N/A"),
                            "value": related.get("numeric_value", related.get("value", "")),
                            "unit": related.get("unit", ""),
                        })

                snapshot.key_people = members[:20]
                snapshot.members_count = len(members)
                snapshot.teams = teams
                snapshot.active_projects = projects
            except Exception as e:
                logger.warning(f"Error getting department relationships: {e}")

        # LLM-выжимка «что с отделом» — только если по отделу есть содержимое.
        if snapshot.key_people or snapshot.active_projects or snapshot.kpis:
            facts = []
            if snapshot.description:
                facts.append(f"Описание: {snapshot.description[:300]}")
            if snapshot.head:
                facts.append(f"Руководитель: {snapshot.head}")
            if snapshot.key_people:
                facts.append("Люди: " + ", ".join(
                    f"{p['name']}{' (' + p['role'] + ')' if p.get('role') else ''}"
                    for p in snapshot.key_people[:10]))
            if snapshot.active_projects:
                facts.append("Проекты: " + ", ".join(snapshot.active_projects[:8]))
            for k in snapshot.kpis[:6]:
                facts.append(f"KPI: {k.get('name')} = {k.get('value')} {k.get('unit', '')}")
            snapshot.ai_summary = await self._llm_entity_summary(
                "отдела", snapshot.name, "\n".join(facts))

        logger.info(f"🏢 Generated department snapshot: {snapshot.name}")
        return snapshot

    async def _generate_team_snapshot(self, team_id: str) -> Optional['TeamSnapshot']:
        """Генерация снапшота команды из графа."""
        if not self.graph:
            return None

        # Ищем узел команды
        team_node = await self.graph.get_node_by_id(team_id)

        if not team_node:
            nodes = await self.graph.get_all_nodes_async(label="Team")
            for n in nodes:
                n_id = (n.get("id") or "").lower()
                n_name = (n.get("name") or "").lower()
                tid = team_id.lower()
                if n_id == tid or n_name == tid.replace("_", " "):
                    team_node = n
                    break

        if not team_node:
            return None

        node_id = team_node.get("id", team_id)
        attrs = team_node

        snapshot = TeamSnapshot()
        snapshot.team_id = team_id
        snapshot.name = attrs.get("name", "N/A")
        snapshot.description = attrs.get("description", "")
        snapshot.department = attrs.get("department", "")
        snapshot.lead = attrs.get("lead", attrs.get("team_lead", ""))
        snapshot.focus_area = attrs.get("focus", attrs.get("focus_area", ""))
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()

        # Связи: участники, проекты
        if node_id:
            try:
                rels = await self.graph.get_node_relationships(node_id)

                members = []
                projects = []
                tasks = []

                for rel in rels.get("outgoing", []) + rels.get("incoming", []):
                    target_id = rel.get("target", rel.get("source", ""))
                    if not target_id:
                        continue
                    related = await self.graph.get_node_by_id(target_id)
                    if not related:
                        continue

                    label = related.get("_label", "")
                    if label == "Person":
                        members.append({
                            "name": related.get("name", "N/A"),
                            "role": related.get("role", ""),
                        })
                    elif label == "Project":
                        projects.append(related.get("name", "N/A"))
                    elif label == "Task":
                        tasks.append(related.get("status", "pending"))
                    elif label == "Department":
                        if not snapshot.department:
                            snapshot.department = related.get("name", "")

                snapshot.members = members[:30]
                snapshot.members_count = len(members)
                snapshot.current_projects = projects
                snapshot.tasks_total = len(tasks)
                snapshot.tasks_completed = sum(1 for t in tasks if t in ("completed", "done"))
            except Exception as e:
                logger.warning(f"Error getting team relationships: {e}")

        logger.info(f"👥 Generated team snapshot: {snapshot.name}")
        return snapshot

    async def generate_all_snapshots(self):
        """
        Генерация ВСЕХ снапшотов из графа (вызывается при полной sync).

        Генерирует: Company, Persons, Projects, Products, Departments, Teams.
        """
        if not self.graph:
            logger.warning("Cannot generate snapshots: no graph")
            return

        # Метка расходов: генерация снапшотов — заметная статья (LLM-вызов на
        # сущность). Без метки весь прогон в экране затрат висит «unknown».
        from backend.core.llm.usage_tracker import UsageContext
        async with UsageContext(agent_mode="snapshots",
                                request_type="snapshot_generation",
                                user_id=self.user_id or None):
            return await self._generate_all_snapshots_inner()

    async def _generate_all_snapshots_inner(self):
        stats = {"company": 0, "persons": 0, "projects": 0, "products": 0, "departments": 0, "teams": 0}

        # Tenant-скоуп: без него get_all_nodes_async в фоновом прогоне
        # (нет tenant_context) фильтрует по `tenant_id IS NULL` и не видит
        # людей/проекты, проштампованные user_id → persons=0 в логе, хотя
        # get_all_people_profiles (иерархия) их находит. Штампуем явно.
        _tid = self.user_id or None
        _strict = bool(_tid)

        # Кап генерации снапшотов: раньше генерился снапшот на КАЖДУЮ сущность
        # с отдельным LLM-вызовом. На раздутом дублями графе это 900+ вызовов
        # за прогон (persons=323, projects=358, departments=162 — почти всё
        # дубли/мусор). Ограничиваем количество и пропускаем мусорные имена.
        # TESSENT_SNAPSHOT_CAP=0 → без лимита (старое поведение).
        import os as _os
        try:
            _cap = int(_os.getenv("TESSENT_SNAPSHOT_CAP", "60"))
        except (TypeError, ValueError):
            _cap = 60
        _JUNK = {"", "-", "n/a", "не указан", "не указано", "неизвестно",
                 "не определен", "не определён", "external", "unknown", "внешний"}

        # products/departments/teams — почти всегда шум экстракции (инструменты,
        # дубли, обрывки), их снапшоты почти не смотрят → жёстче лимит.
        _cap_minor = min(_cap, 20) if _cap > 0 else 0

        def _worthy(nodes, cap=None):
            lim = _cap if cap is None else cap
            good = [n for n in nodes
                    if (n.get("name") or "").strip().lower() not in _JUNK]
            return good if lim <= 0 else good[:lim]

        def _worthy_persons(nodes, cap=None):
            # Строже, чем _worthy: отсекает составные/generic/тайм-фрагменты.
            # КРИТИЧНО: сортируем по вовлечённости ДО капа — раньше брались
            # первые 60 в порядке выдачи Neo4j, и сотрудник «на каждой
            # встрече» мог не получить снапшот, а случайный гость — получить.
            lim = _cap if cap is None else cap
            good = [n for n in nodes if not _is_person_junk(n.get("name"))]
            good.sort(key=lambda n: int(n.get("total_mentions") or 0),
                      reverse=True)
            return good if lim <= 0 else good[:lim]

        # Company
        try:
            await self.get_company_snapshot(force_regenerate=True)
            stats["company"] = 1
        except Exception as e:
            logger.warning(f"Failed to generate company snapshot: {e}")

        # Persons
        try:
            persons = await self.graph.get_all_nodes_async(label="Person", tenant_id=_tid, strict_tenant=_strict)
            for p in _worthy_persons(persons):
                pid = p.get("id")
                if pid:
                    try:
                        await self.get_person_snapshot(pid, force_regenerate=True)
                        stats["persons"] += 1
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to generate person snapshots: {e}")

        # Projects
        try:
            projects = await self.graph.get_all_nodes_async(label="Project", tenant_id=_tid, strict_tenant=_strict)
            for p in _worthy(projects):
                pid = p.get("id")
                if pid:
                    try:
                        await self.get_project_snapshot(pid, force_regenerate=True)
                        stats["projects"] += 1
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to generate project snapshots: {e}")

        # Products
        try:
            products = await self.graph.get_all_nodes_async(label="Product", tenant_id=_tid, strict_tenant=_strict)
            for p in _worthy(products, _cap_minor):
                pid = p.get("id")
                if pid:
                    try:
                        await self.get_product_snapshot(pid, force_regenerate=True)
                        stats["products"] += 1
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to generate product snapshots: {e}")

        # Departments
        try:
            departments = await self.graph.get_all_nodes_async(label="Department", tenant_id=_tid, strict_tenant=_strict)
            for d in _worthy(departments, _cap_minor):
                did = d.get("id")
                if did:
                    try:
                        await self.get_department_snapshot(did, force_regenerate=True)
                        stats["departments"] += 1
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to generate department snapshots: {e}")

        # Teams
        try:
            teams = await self.graph.get_all_nodes_async(label="Team", tenant_id=_tid, strict_tenant=_strict)
            for t in _worthy(teams, _cap_minor):
                tid = t.get("id")
                if tid:
                    try:
                        await self.get_team_snapshot(tid, force_regenerate=True)
                        stats["teams"] += 1
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to generate team snapshots: {e}")

        # Архивация осиротевших снапшотов: сущность исчезла из графа (слита
        # дедупом / удалена) — её файл переезжает в archive/<type>/, а не
        # висит вечно в persons/ и памяти (_load_snapshots грузит всё).
        # Историю НЕ удаляем — она нужна для анализа динамики компании.
        try:
            import shutil as _shutil
            live_by_type = {
                "persons": locals().get("persons"),
                "projects": locals().get("projects"),
                "products": locals().get("products"),
                "departments": locals().get("departments"),
                "teams": locals().get("teams"),
            }
            archived = 0
            for _t, _nodes in live_by_type.items():
                if not _nodes:  # выборка не удалась/пуста — НЕ архивируем
                    continue    # (иначе временный сбой графа снёс бы всё)
                ids = {n.get("id") for n in _nodes if n.get("id")}
                d = self.storage_path / _t
                if not ids or not d.exists():
                    continue
                arch = self.storage_path / "archive" / _t
                for f in d.glob("*.json"):
                    if f.stem not in ids:
                        arch.mkdir(parents=True, exist_ok=True)
                        _shutil.move(str(f), str(arch / f.name))
                        archived += 1
                        for cache in (self._person_snapshots, self._project_snapshots,
                                      self._product_snapshots, self._department_snapshots,
                                      self._team_snapshots):
                            cache.pop(f.stem, None)
            if archived:
                logger.info(f"🗄 Archived {archived} orphaned snapshot file(s)")
        except Exception:
            logger.debug("orphan snapshot archive skipped", exc_info=True)

        logger.info(
            f"📸 All snapshots generated: company={stats['company']}, "
            f"persons={stats['persons']}, projects={stats['projects']}, "
            f"products={stats['products']}, departments={stats['departments']}, "
            f"teams={stats['teams']}"
        )
        return stats

    async def generate_periodic_report(self, period: str = "weekly") -> Optional[str]:
        """
        Генерация периодического отчёта из снапшотов.

        Args:
            period: "weekly" или "monthly"

        Returns:
            Markdown отчёт или None
        """
        if not self._company_snapshot:
            await self.get_company_snapshot()

        if not self._company_snapshot:
            return None

        company = self._company_snapshot

        sections = []
        now = datetime.now(timezone.utc)

        if period == "weekly":
            sections.append("# 📊 Еженедельный отчёт компании")
            sections.append(f"*{company.name} — неделя {now.strftime('%d.%m.%Y')}*\n")
        else:
            sections.append("# 📊 Ежемесячный отчёт компании")
            sections.append(f"*{company.name} — {now.strftime('%B %Y')}*\n")

        # Общий статус
        sections.append("## Общий статус")
        sections.append(f"- Статус: **{company.current_status or 'активна'}**")
        sections.append(f"- Проектов: **{len(company.active_projects)}** активных")
        sections.append(f"- Сотрудников: **{len(company.key_people)}**")
        if company.health_score:
            sections.append(f"- Здоровье компании: **{company.health_score}/100**")

        # Проекты
        if company.active_projects:
            sections.append("\n## Проекты")
            for proj in company.active_projects[:10]:
                status = proj.get("status", "")
                progress = proj.get("progress", "")
                lead = proj.get("lead", "")
                line = f"- **{proj.get('name', 'N/A')}**"
                if status:
                    line += f" — {status}"
                if progress:
                    line += f" ({progress}%)"
                if lead:
                    line += f" [ведёт: {lead}]"
                sections.append(line)

        # KPI
        if company.kpis:
            sections.append("\n## Ключевые показатели (KPI)")
            for kpi in company.kpis[:10]:
                name = kpi.get("name", "N/A")
                value = kpi.get("value", kpi.get("numeric_value", ""))
                unit = kpi.get("unit", "")
                trend = kpi.get("trend", "")
                trend_emoji = "📈" if trend == "up" else "📉" if trend == "down" else "➡️"
                sections.append(f"- {name}: **{value}** {unit} {trend_emoji}")

        # Достижения
        if company.recent_achievements:
            sections.append("\n## Достижения")
            for ach in company.recent_achievements[:5]:
                sections.append(f"- {ach.get('achievement', str(ach))}")

        # Проблемы/блокеры
        if company.challenges:
            sections.append("\n## Проблемы и вызовы")
            for ch in company.challenges[:5]:
                sections.append(f"- ⚠️ {ch.get('challenge', str(ch))}")

        # Delta (что изменилось)
        if company.delta_summary and company.delta_summary != "Минорные обновления":
            sections.append("\n## Что изменилось")
            sections.append(company.delta_summary)

        # Сохраняем отчёт
        report_text = "\n".join(sections)

        try:
            reports_dir = self.storage_path / "reports"
            reports_dir.mkdir(exist_ok=True)

            report_file = reports_dir / f"{period}_{now.strftime('%Y_%m_%d')}.md"
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(report_text)

            logger.info(f"📊 Generated {period} report: {report_file} ({len(report_text)} chars)")
        except Exception as e:
            logger.warning(f"Failed to save report: {e}")

        return report_text

    async def _update_company_from_meeting(self, meeting_data: Dict[str, Any]):
        """Обновить снапшот компании данными встречи."""
        if not self._company_snapshot:
            return

        # Обновляем timestamp
        self._company_snapshot.last_updated = datetime.now(timezone.utc).isoformat()
        self._company_snapshot.update_source = meeting_data.get("meeting_id", "meeting")

        # Добавляем новые решения в достижения
        decisions = meeting_data.get("decisions", [])
        # Анти-спам версий: бампим версию только если встреча реально что-то
        # добавила (есть решения). Иначе за день от бэклога копятся v274…v285,
        # и «Сравнить» между ними показывает «изменений нет».
        if decisions:
            self._company_snapshot.version += 1
        for d in decisions[:3]:
            self._company_snapshot.recent_achievements.append({
                "achievement": d.get("decision", d.get("text", "")),
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "impact": "medium",
            })

        # Ограничиваем размер
        self._company_snapshot.recent_achievements = self._company_snapshot.recent_achievements[-10:]

        self._save_company_snapshot()

    async def _update_person_from_meeting(self, person_id: str, meeting_data: Dict[str, Any]):
        """Обновить снапшот человека данными встречи."""
        if person_id not in self._person_snapshots:
            return

        snapshot = self._person_snapshots[person_id]
        snapshot.last_updated = datetime.now(timezone.utc).isoformat()
        snapshot.last_active = datetime.now(timezone.utc).isoformat()
        snapshot.meetings_participated += 1
        snapshot.version += 1

        # Добавляем новые задачи
        tasks = meeting_data.get("tasks", [])
        for t in tasks:
            assignee = t.get("assignee", "").lower()
            if person_id.lower() in assignee or snapshot.name.lower() in assignee:
                snapshot.current_tasks.append({
                    "task": t.get("title", t.get("name", "")),
                    "status": "pending",
                    "priority": t.get("priority", "normal"),
                })

        # Ограничиваем размер
        snapshot.current_tasks = snapshot.current_tasks[-15:]

        self._save_person_snapshot(person_id)


# Реестр генераторов: ОДИН экземпляр НА ПОЛЬЗОВАТЕЛЯ (не общий singleton).
#
# Раньше был единый singleton, который на каждый запрос «перенастраивали»
# (.graph / .user_id / .storage_path) на текущего юзера. Под async-конкуренцией
# это гонка: запрос юзера A уходит в await (LLM-генерация), в это время запрос
# юзера B (или фоновая джоба — nightly, insight scan, автоматизации)
# переключает singleton на себя, A просыпается и (а) дописывает снапшот,
# собранный из графа A, в каталог B, (б) досоздаёт снапшот уже из графа B —
# получался «гибрид» (шапка одной компании + люди другой) и межтенантная
# утечка. Per-user экземпляры устраняют сам класс проблемы: у каждого тенанта
# свои graph/кэш/каталог, переключать нечего.
from collections import OrderedDict as _OrderedDict

_instances: "_OrderedDict[str, EnhancedSnapshotGenerator]" = _OrderedDict()
# LRU-предел: экземпляр лёгкий (кэш снапшотов + ссылка на граф-вью), но при
# тысячах тенантов бессрочно копить их в памяти нельзя. Вытеснённый экземпляр
# ничего не теряет — состояние на диске, следующий запрос пересоздаст.
_MAX_GENERATOR_INSTANCES = 12


def get_enhanced_snapshot_generator(graph_builder=None, user_id: Optional[str] = None) -> EnhancedSnapshotGenerator:
    """Получить экземпляр EnhancedSnapshotGenerator для КОНКРЕТНОГО тенанта.

    Экземпляры кэшируются по user_id (LRU). graph_builder обновляется на
    каждый вызов — вью графа строится per-request и должно быть свежим.
    user_id="" — легаси-экземпляр с общей data/snapshots (для старых вызовов
    без тенанта); он больше не пересекается с per-user данными.
    """
    # Защита от легаси-вызова get_enhanced_snapshot_generator("<uid>"):
    # строка в позиции graph_builder — это user_id, а не граф.
    if isinstance(graph_builder, str):
        if not user_id:
            user_id = graph_builder
        graph_builder = None

    key = (user_id or "").strip()
    inst = _instances.get(key)
    if inst is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
        storage: Optional[str] = None
        if key:
            # Каталог тенанта задаём СРАЗУ в конструкторе: иначе __init__
            # успел бы загрузить в память легаси-общий data/snapshots, и
            # чужой снапшот отдавался бы до первой регенерации.
            try:
                from backend.core.store.tenant_paths import snapshots_dir_for_user
                storage = str(snapshots_dir_for_user(key))
            except Exception:
                logger.debug("per-user snapshot path skipped", exc_info=True)
        if storage:
            inst = EnhancedSnapshotGenerator(graph_builder, llm_router=llm, storage_path=storage)
        else:
            inst = EnhancedSnapshotGenerator(graph_builder, llm_router=llm)
        inst.user_id = key
        _instances[key] = inst
        while len(_instances) > _MAX_GENERATOR_INSTANCES:
            _instances.popitem(last=False)
    else:
        _instances.move_to_end(key)
        if graph_builder is not None:
            inst.graph = graph_builder
    return inst


def invalidate_snapshot_cache(user_id: Optional[str] = None) -> None:
    """Сбросить кэш снапшотов после появления новых данных (sync/инжест).

    Иначе страница «Компания» отдавала СТАРЫЙ кэшированный снапшот до
    рестарта процесса или кнопки «Пересобрать». Работает и МЕЖПРОЦЕССНО:
    маркер .needs_regen пишется в per-user каталог снапшотов, а _should_regen
    проверяет его на каждый read — поэтому инжест в воркере (отдельный
    процесс) корректно инвалидирует кэш API-процесса.
    """
    def _write_marker(d: Path) -> None:
        # Персистентный маркер: переживает рестарт процесса. Без него после
        # рестарта __init__ грузил старый company_snapshot.json, _dirty=False —
        # и регенерация не запускалась (снапшот «застывал»).
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / ".needs_regen").write_text("1", encoding="utf-8")
        except Exception:
            logger.debug("write regen marker failed", exc_info=True)

    if user_id:
        inst = _instances.get(user_id)
        if inst is not None:
            inst._reset_caches()
        # Каталог маркера: per-user каталог ЗАПРОШЕННОГО юзера — даже если
        # его экземпляра нет в памяти (инжест в другом процессе/воркере).
        try:
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            _write_marker(Path(snapshots_dir_for_user(user_id)))
        except Exception:
            logger.debug("per-user marker dir resolve failed", exc_info=True)
    else:
        # Без user_id — глобальная инвалидация: все живые экземпляры.
        for inst in list(_instances.values()):
            inst._reset_caches()
            _write_marker(Path(inst.storage_path))
