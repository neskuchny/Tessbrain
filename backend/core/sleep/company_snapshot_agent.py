# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Company Snapshot Agent
=======================================

Создаёт и обновляет "умный слепок" компании - компактное резюме,
которое содержит актуальное состояние организации и позволяет
отвечать на 40-50% вопросов без дополнительного поиска.

Ключевые особенности:
- Обновляется с каждой встречей
- НЕ РАСТЁТ бесконечно (макс 2 страницы)
- Содержит только актуальную информацию
- Используется как контекст для всех агентов
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CompanySnapshotAgent:
    """
    Агент создания и обновления Company Snapshot.

    Snapshot содержит:
    - Описание компании и продуктов
    - Список сотрудников с характеристиками
    - Текущие проекты со статусами
    - Критические задачи
    - Текущие проблемы и возможности
    - Стратегические цели

    Пример использования:
    ```python
    agent = CompanySnapshotAgent(llm_client, graph_builder)

    # После обработки встречи
    await agent.update_snapshot(meeting_results)

    # Получить текущий snapshot для промпта
    snapshot_text = agent.get_snapshot_for_prompt()
    ```
    """

    MAX_SNAPSHOT_LENGTH = 4000  # ~2 страницы А4
    STORAGE_PATH = "data/snapshots"
    CURRENT_SNAPSHOT_FILE = "current_snapshot.json"

    def __init__(
        self,
        llm_client=None,
        graph_builder=None,
        storage_path: Optional[str] = None
    ):
        """
        Args:
            llm_client: Клиент LLM для генерации и обновления
            graph_builder: GraphBuilder для доступа к данным
            storage_path: Путь для хранения снапшотов
        """
        self.llm = llm_client
        self.graph = graph_builder
        self.storage_path = storage_path or self.STORAGE_PATH

        # Текущий snapshot
        self._current_snapshot: Optional[Dict[str, Any]] = None

        # Загружаем существующий snapshot
        self._load_current_snapshot()

        logger.info("🏢 CompanySnapshotAgent initialized")

    def _load_current_snapshot(self):
        """Загружает текущий snapshot из файла"""
        try:
            filepath = os.path.join(self.storage_path, self.CURRENT_SNAPSHOT_FILE)
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    self._current_snapshot = json.load(f)
                logger.info(f"📂 Loaded existing snapshot from {filepath}")
        except Exception as e:
            logger.warning(f"⚠️ Could not load snapshot: {e}")
            self._current_snapshot = None

    def _save_current_snapshot(self):
        """Сохраняет текущий snapshot в файл"""
        try:
            os.makedirs(self.storage_path, exist_ok=True)

            # Сохраняем текущий
            filepath = os.path.join(self.storage_path, self.CURRENT_SNAPSHOT_FILE)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._current_snapshot, f, ensure_ascii=False, indent=2)

            # Сохраняем в историю
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            history_file = os.path.join(self.storage_path, f"snapshot_{timestamp}.json")
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(self._current_snapshot, f, ensure_ascii=False, indent=2)

            logger.info(f"💾 Snapshot saved to {filepath}")
        except Exception as e:
            logger.error(f"❌ Could not save snapshot: {e}")

    async def update_snapshot(
        self,
        meeting_results: Dict[str, Any],
        meeting_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Обновляет snapshot на основе результатов обработки встречи.

        Args:
            meeting_results: Результаты CaptureOrchestrator
            meeting_id: ID встречи

        Returns:
            Обновлённый snapshot
        """
        logger.info(f"🔄 Updating company snapshot from meeting {meeting_id}")

        try:
            # Получаем текущий snapshot
            current = self._current_snapshot or self._create_initial_snapshot()

            # Извлекаем новые данные из встречи
            new_data = self._extract_meeting_data(meeting_results)

            # Обновляем snapshot через LLM
            if self.llm:
                updated = await self._llm_update_snapshot(current, new_data)
            else:
                updated = self._manual_update_snapshot(current, new_data)

            # Обрезаем если слишком длинный
            updated = self._trim_snapshot(updated)

            # Добавляем метаданные
            updated["metadata"] = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "last_meeting_id": meeting_id,
                "version": current.get("metadata", {}).get("version", 0) + 1
            }

            # Сохраняем
            self._current_snapshot = updated
            self._save_current_snapshot()

            logger.info(f"✅ Snapshot updated (version {updated['metadata']['version']})")
            return updated

        except Exception as e:
            logger.error(f"❌ Error updating snapshot: {e}")
            return self._current_snapshot or self._create_initial_snapshot()

    def _create_initial_snapshot(self) -> Dict[str, Any]:
        """Создаёт начальный пустой snapshot"""
        return {
            "company": {
                "name": "",
                "description": "",
                "products": [],
                "target_audience": "",
                "usp": ""
            },
            "team": [],
            "projects": [],
            "critical_tasks": [],
            "strategic_goals": [],
            "current_problems": [],
            "opportunities": [],
            "tech_stack": [],
            "metadata": {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "version": 0
            }
        }

    def _extract_meeting_data(self, meeting_results: Dict[str, Any]) -> Dict[str, Any]:
        """Извлекает релевантные данные из результатов встречи"""
        return {
            "participants": meeting_results.get("participants", []),
            "decisions": meeting_results.get("decisions", []),
            "tasks": meeting_results.get("tasks", []),
            "entities": meeting_results.get("entities", []),
            "kpis": meeting_results.get("kpis", []),
            "opinions": meeting_results.get("opinions", []),
            "ideas": meeting_results.get("ideas", []),
            "project_statuses": meeting_results.get("project_statuses", []),
            "meeting_context": meeting_results.get("metadata", {}).get("context", {})
        }

    async def _llm_update_snapshot(
        self,
        current: Dict[str, Any],
        new_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Обновляет snapshot через LLM"""
        prompt = f'''Ты - агент обновления Company Snapshot.

ТЕКУЩИЙ SNAPSHOT:
```json
{json.dumps(current, ensure_ascii=False, indent=2)[:3000]}
```

НОВЫЕ ДАННЫЕ ИЗ ВСТРЕЧИ:
```json
{json.dumps(new_data, ensure_ascii=False, indent=2)[:3000]}
```

ЗАДАЧА:
1. Проанализируй новые данные
2. Определи что нужно:
   - ДОБАВИТЬ (новые проекты, люди, задачи)
   - ОБНОВИТЬ (статусы, мотивация, проблемы)
   - УДАЛИТЬ (неактуальные задачи, решённые проблемы)
3. Сгенерируй обновлённый snapshot

ПРАВИЛА:
- Snapshot должен быть КОМПАКТНЫМ (макс 3000 символов)
- Сохраняй только АКТУАЛЬНУЮ информацию
- Обновляй статусы проектов и задач
- Добавляй новых участников с их ролями
- Удаляй выполненные задачи из critical_tasks
- Переноси решённые проблемы в opportunities (если применимо)

ФОРМАТ ВЫВОДА - JSON:
```json
{{
  "company": {{
    "name": "Название компании",
    "description": "Краткое описание деятельности",
    "products": ["Продукт 1", "Продукт 2"],
    "target_audience": "Целевая аудитория",
    "usp": "Уникальное торговое предложение"
  }},
  "team": [
    {{
      "name": "Имя",
      "role": "Роль",
      "focus": "Текущий фокус",
      "motivation": "high|medium|low",
      "notes": "Заметки"
    }}
  ],
  "projects": [
    {{
      "name": "Название проекта",
      "status": "in_progress|completed|on_hold",
      "progress": 60,
      "health": "green|yellow|red",
      "critical_issues": ["Проблемы"],
      "next_milestone": "Следующий этап"
    }}
  ],
  "critical_tasks": [
    {{
      "task": "Описание задачи",
      "assignee": "Ответственный",
      "deadline": "Срок",
      "priority": "critical|high|medium"
    }}
  ],
  "strategic_goals": ["Цель 1", "Цель 2"],
  "current_problems": [
    {{
      "problem": "Описание проблемы",
      "severity": "high|medium|low",
      "proposed_solution": "Предложенное решение"
    }}
  ],
  "opportunities": ["Возможность 1", "Возможность 2"],
  "tech_stack": ["Технология 1", "Технология 2"]
}}
```

Верни ТОЛЬКО JSON без пояснений.
'''

        try:
            result = await self.llm.generate_json(prompt)
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.error(f"❌ LLM update failed: {e}")

        return self._manual_update_snapshot(current, new_data)

    def _manual_update_snapshot(
        self,
        current: Dict[str, Any],
        new_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Обновляет snapshot без LLM (fallback)"""
        updated = current.copy()

        # Добавляем новых участников
        existing_names = {p.get("name", "").lower() for p in updated.get("team", [])}
        for participant in new_data.get("participants", []):
            name = participant.get("name", "")
            if name.lower() not in existing_names:
                updated.setdefault("team", []).append({
                    "name": name,
                    "role": participant.get("role", ""),
                    "focus": "",
                    "motivation": "medium"
                })
                existing_names.add(name.lower())

        # Обновляем проекты
        existing_projects = {p.get("name", "").lower() for p in updated.get("projects", [])}
        for status in new_data.get("project_statuses", []):
            name = status.get("project_name", "")
            if name.lower() not in existing_projects:
                updated.setdefault("projects", []).append({
                    "name": name,
                    "status": status.get("status_assessment", {}).get("current_status", "in_progress"),
                    "progress": status.get("status_assessment", {}).get("progress_percentage", 0),
                    "health": status.get("status_assessment", {}).get("health_indicator", "yellow")
                })

        # Добавляем критические задачи
        for task in new_data.get("tasks", []):
            priority = task.get("priority", "medium")
            if priority in ("critical", "high"):
                updated.setdefault("critical_tasks", []).append({
                    "task": task.get("title", task.get("description", "")),
                    "assignee": task.get("assignee", {}).get("name", "") if isinstance(task.get("assignee"), dict) else task.get("assignee", ""),
                    "deadline": task.get("deadline", ""),
                    "priority": priority
                })

        # Добавляем идеи в opportunities
        for idea in new_data.get("ideas", []):
            priority = idea.get("priority_scoring", {}).get("overall_priority", "medium")
            if priority in ("critical", "high"):
                updated.setdefault("opportunities", []).append(
                    idea.get("idea_summary", "")
                )

        return updated

    def _trim_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Обрезает snapshot до максимального размера"""
        # Сериализуем для проверки размера
        serialized = json.dumps(snapshot, ensure_ascii=False)

        if len(serialized) <= self.MAX_SNAPSHOT_LENGTH:
            return snapshot

        logger.info(f"✂️ Trimming snapshot from {len(serialized)} to {self.MAX_SNAPSHOT_LENGTH}")

        # Приоритеты обрезки (от менее важного к более важному)
        trim_order = [
            ("opportunities", 3),
            ("tech_stack", 5),
            ("current_problems", 3),
            ("critical_tasks", 5),
            ("projects", 5),
            ("team", 10)
        ]

        for field, max_items in trim_order:
            if field in snapshot and isinstance(snapshot[field], list):
                if len(snapshot[field]) > max_items:
                    snapshot[field] = snapshot[field][:max_items]

            # Проверяем размер
            serialized = json.dumps(snapshot, ensure_ascii=False)
            if len(serialized) <= self.MAX_SNAPSHOT_LENGTH:
                break

        return snapshot

    def get_snapshot_for_prompt(self) -> str:
        """
        Возвращает snapshot в формате для LLM промпта.

        Returns:
            Текстовое представление snapshot
        """
        if not self._current_snapshot:
            return "Нет данных о компании."

        s = self._current_snapshot

        lines = [
            "# COMPANY SNAPSHOT",
            f"Обновлено: {s.get('metadata', {}).get('last_updated', 'N/A')[:10]}",
            ""
        ]

        # Компания
        company = s.get("company", {})
        if company.get("name"):
            lines.append(f"## 🏢 О компании: {company.get('name')}")
            if company.get("description"):
                lines.append(company.get("description"))
            if company.get("products"):
                lines.append(f"Продукты: {', '.join(company.get('products', []))}")
            lines.append("")

        # Команда
        team = s.get("team", [])
        if team:
            lines.append(f"## 👥 Команда ({len(team)} человек)")
            for member in team[:10]:
                motivation_emoji = {"high": "✅", "medium": "➖", "low": "⚠️"}.get(
                    member.get("motivation", "medium"), "➖"
                )
                role = f" ({member.get('role')})" if member.get("role") else ""
                focus = f" - {member.get('focus')}" if member.get("focus") else ""
                lines.append(f"- **{member.get('name')}**{role}{focus} {motivation_emoji}")
            lines.append("")

        # Проекты
        projects = s.get("projects", [])
        if projects:
            lines.append(f"## 🚀 Проекты ({len(projects)})")
            for proj in projects[:5]:
                health_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(
                    proj.get("health", "yellow"), "🟡"
                )
                progress = proj.get("progress", 0)
                lines.append(f"- **{proj.get('name')}** [{progress}%] {health_emoji}")
                if proj.get("critical_issues"):
                    lines.append(f"  ⚠️ {', '.join(proj.get('critical_issues', [])[:2])}")
            lines.append("")

        # Критические задачи
        tasks = s.get("critical_tasks", [])
        if tasks:
            lines.append(f"## 📋 Критические задачи ({len(tasks)})")
            for task in tasks[:5]:
                priority_emoji = {"critical": "🔥", "high": "⚠️"}.get(
                    task.get("priority", "high"), "⚠️"
                )
                assignee = f" → {task.get('assignee')}" if task.get("assignee") else ""
                deadline = f" (до {task.get('deadline')})" if task.get("deadline") else ""
                lines.append(f"- {priority_emoji} {task.get('task')}{assignee}{deadline}")
            lines.append("")

        # Стратегические цели
        goals = s.get("strategic_goals", [])
        if goals:
            lines.append("## 🎯 Стратегические цели")
            for goal in goals[:5]:
                lines.append(f"- {goal}")
            lines.append("")

        # Текущие проблемы
        problems = s.get("current_problems", [])
        if problems:
            lines.append("## ⚠️ Текущие проблемы")
            for prob in problems[:3]:
                if isinstance(prob, dict):
                    lines.append(f"- {prob.get('problem')}")
                    if prob.get("proposed_solution"):
                        lines.append(f"  → Решение: {prob.get('proposed_solution')}")
                else:
                    lines.append(f"- {prob}")
            lines.append("")

        # Возможности
        opportunities = s.get("opportunities", [])
        if opportunities:
            lines.append("## 💡 Возможности")
            for opp in opportunities[:3]:
                lines.append(f"- {opp}")
            lines.append("")

        return "\n".join(lines)

    def get_current_snapshot(self) -> Optional[Dict[str, Any]]:
        """Возвращает текущий snapshot как словарь"""
        return self._current_snapshot

    async def rebuild_from_graph(self) -> Dict[str, Any]:
        """
        Перестраивает snapshot из графа знаний.
        Используется для инициализации или восстановления.
        """
        if not self.graph or not hasattr(self.graph, 'nx_graph'):
            logger.warning("Graph not available for rebuild")
            return self._create_initial_snapshot()

        logger.info("🔄 Rebuilding snapshot from graph...")

        graph = self.graph.nx_graph
        snapshot = self._create_initial_snapshot()

        # Собираем данные из графа
        for _node_id, data in graph.nodes(data=True):
            label = data.get("_label", "")

            if label == "Person":
                snapshot["team"].append({
                    "name": data.get("name", "Unknown"),
                    "role": data.get("role", ""),
                    "focus": "",
                    "motivation": "medium"
                })

            elif label == "Project":
                snapshot["projects"].append({
                    "name": data.get("name", "Unknown"),
                    "status": data.get("status", "in_progress"),
                    "progress": data.get("progress", 0),
                    "health": "yellow"
                })

            elif label == "Task":
                priority = data.get("priority", "medium")
                if priority in ("critical", "high"):
                    snapshot["critical_tasks"].append({
                        "task": data.get("title", data.get("name", "")),
                        "assignee": data.get("assignee", ""),
                        "deadline": data.get("deadline", ""),
                        "priority": priority
                    })

            elif label == "Organization":
                snapshot["company"]["name"] = data.get("name", "")
                snapshot["company"]["description"] = data.get("description", "")

            elif label == "Entity":
                entity_type = data.get("entity_type", "")
                if entity_type == "technology":
                    snapshot["tech_stack"].append(data.get("name", ""))
                elif entity_type == "product":
                    snapshot["company"]["products"].append(data.get("name", ""))

        # Обрезаем и сохраняем
        snapshot = self._trim_snapshot(snapshot)
        snapshot["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

        self._current_snapshot = snapshot
        self._save_current_snapshot()

        logger.info(f"✅ Snapshot rebuilt: {len(snapshot['team'])} people, {len(snapshot['projects'])} projects")
        return snapshot


# Singleton
_global_snapshot_agent: Optional[CompanySnapshotAgent] = None


def get_company_snapshot_agent(
    llm_client=None,
    graph_builder=None
) -> CompanySnapshotAgent:
    """Получить глобальный экземпляр агента"""
    global _global_snapshot_agent
    if _global_snapshot_agent is None:
        _global_snapshot_agent = CompanySnapshotAgent(llm_client, graph_builder)
    return _global_snapshot_agent


