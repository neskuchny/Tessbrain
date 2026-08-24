# -*- coding: utf-8 -*-
"""
Скрипт для обогащения графа данными из встреч.

Использует CaptureOrchestrator для извлечения:
- Participants (Person)
- Tasks
- Decisions
- Entities (projects, technologies, organizations)
- KPIs

И добавляет их в граф.

Использование:
    python scripts/enrich_graph_from_meetings.py --user-id <USER_ID> --limit 10
"""
import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

load_dotenv()

# Добавляем путь к backend
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging

from backend.core.capture.orchestrator import CaptureOrchestrator
from backend.core.store.graph_builder import GraphBuilder
from backend.db.supabase_client import SupabaseClient, set_user_context

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class GraphEnricher:
    """
    Обогащает граф данными из встреч.
    """

    def __init__(self, graph: GraphBuilder, user_id: str):
        self.graph = graph
        self.user_id = user_id
        self.orchestrator = CaptureOrchestrator()
        self.supabase = SupabaseClient()

        # Статистика
        self.stats = {
            "meetings_processed": 0,
            "persons_added": 0,
            "tasks_added": 0,
            "decisions_added": 0,
            "entities_added": 0,
            "kpis_added": 0,
            "edges_added": 0,
            "errors": 0
        }

    async def enrich_from_meetings(self, limit: int = 10, skip_processed: bool = True, project_id: str = None):
        """
        Обогатить граф данными из встреч.

        Args:
            limit: Максимальное количество встреч для обработки
            skip_processed: Пропускать уже обработанные встречи
            project_id: ID проекта для фильтрации (опционально)
        """
        print(f"\n{'='*60}")
        print("🔄 ENRICHING GRAPH FROM MEETINGS")
        print(f"{'='*60}\n")

        # Получаем список встреч с транскриптами
        set_user_context(user_id=self.user_id)

        if project_id:
            print(f"📋 Загружаю встречи из проекта {project_id[:8]}...")
            # Получаем встречи из конкретного проекта с транскриптами
            meetings = await self.supabase._request(
                'GET', '/rest/v1/meetings',
                params={
                    'project_id': f'eq.{project_id}',
                    'status': 'eq.active',
                    'transcription_status': 'eq.completed',
                    'select': 'id,meeting_id,title,created_at,transcription_status,meeting_type,project_id,folder_id',
                    'order': 'created_at.desc',
                    'limit': str(limit * 2)
                }
            )
        else:
            print(f"📋 Загружаю встречи для user_id={self.user_id[:8]}...")
            meetings = await self.supabase.get_meetings(
                user_id=self.user_id,
                limit=limit * 2  # Берём больше, т.к. не у всех есть транскрипты
            )

        print(f"   ✅ Найдено {len(meetings)} встреч")

        processed_count = 0

        for meeting in meetings:
            if processed_count >= limit:
                break

            meeting_id = meeting.get("meeting_id") or meeting.get("id")
            title = meeting.get("title", "Без названия")

            # Проверяем есть ли уже в графе
            if skip_processed:
                meeting_node_id = f"Meeting_{meeting_id[:8]}"
                if self.graph.nx_graph.has_node(meeting_node_id):
                    # Проверяем есть ли связанные сущности
                    neighbors = list(self.graph.nx_graph.neighbors(meeting_node_id))
                    has_entities = any(
                        self.graph.nx_graph.nodes[n].get("_label") in ["Person", "Task", "Decision", "Entity"]
                        for n in neighbors if self.graph.nx_graph.has_node(n)
                    )
                    if has_entities:
                        print(f"   ⏭️ Пропускаю (уже обработана): {title[:40]}...")
                        continue

            # Получаем транскрипт
            print(f"\n📝 Обрабатываю: {title[:50]}...")

            try:
                transcript = await self.supabase.get_meeting_transcript(meeting_id)

                if not transcript or len(transcript) < 100:
                    print("   ⚠️ Нет транскрипта или слишком короткий")
                    continue

                print(f"   📄 Транскрипт: {len(transcript)} символов")

                # Извлекаем сущности
                results = await self.orchestrator.process_meeting(
                    transcript=transcript,
                    meeting_id=meeting_id,
                    meeting_context={
                        "title": title,
                        "project_id": meeting.get("project_id"),
                        "folder_id": meeting.get("folder_id")
                    }
                )

                if results.get("status") == "error":
                    print(f"   ❌ Ошибка обработки: {results.get('error')}")
                    self.stats["errors"] += 1
                    continue

                # Добавляем в граф
                await self._add_results_to_graph(meeting, results)

                processed_count += 1
                self.stats["meetings_processed"] += 1

                # Выводим сводку
                summary = results.get("summary", {})
                print(f"   ✅ Извлечено: "
                      f"{summary.get('participants_count', 0)} участников, "
                      f"{summary.get('tasks_count', 0)} задач, "
                      f"{summary.get('decisions_count', 0)} решений, "
                      f"{summary.get('entities_count', 0)} сущностей")

            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                self.stats["errors"] += 1
                continue

        # Сохраняем граф
        print("\n💾 Сохраняю граф...")
        self.graph.save_graph()

        # Выводим итоговую статистику
        print(f"\n{'='*60}")
        print("✅ ОБОГАЩЕНИЕ ЗАВЕРШЕНО!")
        print(f"{'='*60}")
        print(f"   📊 Обработано встреч: {self.stats['meetings_processed']}")
        print(f"   👤 Добавлено людей: {self.stats['persons_added']}")
        print(f"   ✅ Добавлено задач: {self.stats['tasks_added']}")
        print(f"   📋 Добавлено решений: {self.stats['decisions_added']}")
        print(f"   🏷️ Добавлено сущностей: {self.stats['entities_added']}")
        print(f"   📈 Добавлено KPI: {self.stats['kpis_added']}")
        print(f"   🔗 Добавлено связей: {self.stats['edges_added']}")
        print(f"   ❌ Ошибок: {self.stats['errors']}")
        print(f"\n   📊 Всего узлов в графе: {self.graph.nx_graph.number_of_nodes()}")
        print(f"   🔗 Всего связей в графе: {self.graph.nx_graph.number_of_edges()}")
        print(f"{'='*60}\n")

        return self.stats

    async def _add_results_to_graph(self, meeting: Dict, results: Dict):
        """Добавляет извлечённые результаты в граф."""
        meeting_id = meeting.get("meeting_id") or meeting.get("id")
        meeting_node_id = f"Meeting_{meeting_id[:8]}"

        # 1. Добавляем участников (Person)
        for participant in results.get("participants", []):
            person_id = await self._add_person(participant, meeting_node_id)
            if person_id:
                self.stats["persons_added"] += 1

        # 2. Добавляем задачи (Task)
        for task in results.get("tasks", []):
            task_id = await self._add_task(task, meeting_node_id)
            if task_id:
                self.stats["tasks_added"] += 1

        # 3. Добавляем решения (Decision)
        for decision in results.get("decisions", []):
            decision_id = await self._add_decision(decision, meeting_node_id)
            if decision_id:
                self.stats["decisions_added"] += 1

        # 4. Добавляем сущности (Entity)
        for entity in results.get("entities", []):
            entity_id = await self._add_entity(entity, meeting_node_id)
            if entity_id:
                self.stats["entities_added"] += 1

        # 5. Добавляем KPI
        for kpi in results.get("kpis", []):
            kpi_id = await self._add_kpi(kpi, meeting_node_id)
            if kpi_id:
                self.stats["kpis_added"] += 1

    async def _add_person(self, participant: Dict, meeting_node_id: str) -> str:
        """Добавляет участника в граф."""
        name = participant.get("name", "").strip()
        if not name:
            return None

        # Создаём ID из имени
        person_id = f"Person_{name.replace(' ', '_')}"

        # Проверяем существует ли уже
        if self.graph.nx_graph.has_node(person_id):
            # Обновляем связь с встречей
            if not self.graph.nx_graph.has_edge(person_id, meeting_node_id):
                self.graph.nx_graph.add_edge(
                    person_id, meeting_node_id,
                    relation="PARTICIPATED_IN",
                    role=participant.get("role", "participant")
                )
                self.stats["edges_added"] += 1
            return None  # Не считаем как новый

        # Создаём узел
        self.graph.nx_graph.add_node(
            person_id,
            id=person_id,
            name=name,
            role=participant.get("role", ""),
            department=participant.get("department", ""),
            expertise=participant.get("expertise", []),
            _label="Person",
            _key=name,
            user_id=self.user_id,
            access_group="private",
            created_at=datetime.now().isoformat()
        )

        # Связь с встречей
        self.graph.nx_graph.add_edge(
            person_id, meeting_node_id,
            relation="PARTICIPATED_IN",
            role=participant.get("role", "participant")
        )
        self.stats["edges_added"] += 1

        return person_id

    async def _add_task(self, task: Dict, meeting_node_id: str) -> str:
        """Добавляет задачу в граф."""
        title = task.get("title") or task.get("task") or ""
        if not title:
            return None

        task_id = task.get("task_id") or f"Task_{str(uuid.uuid4())[:8]}"
        node_id = f"Task_{task_id[:8]}"

        # Проверяем существует ли
        if self.graph.nx_graph.has_node(node_id):
            return None

        self.graph.nx_graph.add_node(
            node_id,
            id=node_id,
            task_id=task_id,
            title=title,
            description=task.get("description", ""),
            priority=task.get("priority", "medium"),
            status=task.get("status", "pending"),
            deadline=task.get("deadline", ""),
            _label="Task",
            _key=task_id,
            user_id=self.user_id,
            access_group="private",
            created_at=datetime.now().isoformat()
        )

        # Связь с встречей
        self.graph.nx_graph.add_edge(
            node_id, meeting_node_id,
            relation="CREATED_IN"
        )
        self.stats["edges_added"] += 1

        # Связь с исполнителем
        assignee = task.get("assignee", {})
        if isinstance(assignee, dict) and assignee.get("name"):
            assignee_name = assignee.get("name")
            person_id = f"Person_{assignee_name.replace(' ', '_')}"
            if self.graph.nx_graph.has_node(person_id):
                self.graph.nx_graph.add_edge(
                    node_id, person_id,
                    relation="ASSIGNED_TO"
                )
                self.stats["edges_added"] += 1

        return node_id

    async def _add_decision(self, decision: Dict, meeting_node_id: str) -> str:
        """Добавляет решение в граф."""
        summary = decision.get("decision_summary") or decision.get("summary") or ""
        if not summary:
            return None

        decision_id = decision.get("decision_id") or f"Decision_{str(uuid.uuid4())[:8]}"
        node_id = f"Decision_{decision_id[:8]}"

        if self.graph.nx_graph.has_node(node_id):
            return None

        self.graph.nx_graph.add_node(
            node_id,
            id=node_id,
            decision_id=decision_id,
            summary=summary,
            rationale=decision.get("rationale", ""),
            impact=decision.get("impact", ""),
            decision_type=decision.get("decision_type", "operational"),
            _label="Decision",
            _key=decision_id,
            user_id=self.user_id,
            access_group="private",
            created_at=datetime.now().isoformat()
        )

        # Связь с встречей
        self.graph.nx_graph.add_edge(
            node_id, meeting_node_id,
            relation="MADE_IN"
        )
        self.stats["edges_added"] += 1

        return node_id

    async def _add_entity(self, entity: Dict, meeting_node_id: str) -> str:
        """Добавляет сущность в граф."""
        name = entity.get("name", "").strip()
        entity_type = entity.get("entity_type", "entity")

        if not name:
            return None

        entity_id = entity.get("entity_id") or f"{entity_type}_{str(uuid.uuid4())[:8]}"
        node_id = f"Entity_{name.replace(' ', '_')}"

        if self.graph.nx_graph.has_node(node_id):
            # Добавляем связь с встречей если нет
            if not self.graph.nx_graph.has_edge(node_id, meeting_node_id):
                self.graph.nx_graph.add_edge(
                    node_id, meeting_node_id,
                    relation="MENTIONED_IN"
                )
                self.stats["edges_added"] += 1
            return None

        # Определяем label на основе типа
        label_map = {
            "technology": "Technology",
            "organization": "Organization",
            "project": "Project",
            "product": "Product",
            "document": "Document",
            "event": "Event",
            "idea": "Idea"
        }
        label = label_map.get(entity_type, "Entity")

        self.graph.nx_graph.add_node(
            node_id,
            id=node_id,
            entity_id=entity_id,
            name=name,
            entity_type=entity_type,
            description=entity.get("description", ""),
            attributes=entity.get("attributes", {}),
            importance=entity.get("importance", "medium"),
            _label=label,
            _key=name,
            user_id=self.user_id,
            access_group="private",
            created_at=datetime.now().isoformat()
        )

        # Связь с встречей
        self.graph.nx_graph.add_edge(
            node_id, meeting_node_id,
            relation="MENTIONED_IN"
        )
        self.stats["edges_added"] += 1

        return node_id

    async def _add_kpi(self, kpi: Dict, meeting_node_id: str) -> str:
        """Добавляет KPI в граф."""
        name = kpi.get("name") or kpi.get("metric_name", "").strip()
        if not name:
            return None

        kpi_id = kpi.get("kpi_id") or f"KPI_{str(uuid.uuid4())[:8]}"
        node_id = f"KPI_{name.replace(' ', '_')}"

        if self.graph.nx_graph.has_node(node_id):
            return None

        self.graph.nx_graph.add_node(
            node_id,
            id=node_id,
            kpi_id=kpi_id,
            name=name,
            value=kpi.get("value"),
            unit=kpi.get("unit", ""),
            trend=kpi.get("trend", ""),
            target=kpi.get("target"),
            _label="KPI",
            _key=name,
            user_id=self.user_id,
            access_group="private",
            created_at=datetime.now().isoformat()
        )

        # Связь с встречей
        self.graph.nx_graph.add_edge(
            node_id, meeting_node_id,
            relation="REPORTED_IN"
        )
        self.stats["edges_added"] += 1

        return node_id


async def main():
    parser = argparse.ArgumentParser(description="Enrich graph from meetings")
    parser.add_argument("--user-id", required=True, help="User ID from Supabase")
    parser.add_argument("--project-id", help="Process meetings from specific project")
    parser.add_argument("--limit", type=int, default=10, help="Max meetings to process")
    parser.add_argument("--force", action="store_true", help="Process all meetings (don't skip processed)")

    args = parser.parse_args()

    # Создаём граф
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    # Создаём enricher
    enricher = GraphEnricher(graph, args.user_id)

    # Обогащаем
    stats = await enricher.enrich_from_meetings(
        limit=args.limit,
        skip_processed=not args.force,
        project_id=args.project_id
    )

    return stats


if __name__ == "__main__":
    asyncio.run(main())

