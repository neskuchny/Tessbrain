# -*- coding: utf-8 -*-
"""
Скрипт для анализа связей между встречами.

Запуск:
    python scripts/analyze_cross_meeting_links.py --meeting-id <ID>
    python scripts/analyze_cross_meeting_links.py --all
    python scripts/analyze_cross_meeting_links.py --project "Название проекта"
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Добавляем путь к проекту
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from backend.core.capture.agents.cross_meeting_analyzer import CrossMeetingAnalyzer
from backend.core.llm.router import LLMRouter
from backend.core.store.graph_builder import GraphBuilder
from backend.db.supabase_client import get_supabase_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def analyze_single_meeting(meeting_id: str):
    """Анализирует связи одной встречи"""
    logger.info(f"🔗 Анализ связей для встречи: {meeting_id}")

    # Инициализация
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    llm = LLMRouter()

    analyzer = CrossMeetingAnalyzer(graph_builder=graph, llm_router=llm)

    # Получаем данные встречи из графа
    meeting_data = {}
    nodes = graph.get_all_nodes()

    for node in nodes:
        if node.get("id") == meeting_id or node.get("meeting_id") == meeting_id:
            if node.get("_label") == "Meeting":
                meeting_data = node
                break

    if not meeting_data:
        logger.warning(f"⚠️ Встреча {meeting_id} не найдена в графе")
        # Попробуем получить из Supabase
        try:
            supabase = get_supabase_client()
            meetings = await supabase.get_meetings(meeting_ids=[meeting_id])
            if meetings:
                meeting_data = meetings[0]
        except Exception as e:
            logger.error(f"Ошибка получения из Supabase: {e}")

    # Анализируем
    result = await analyzer.analyze_meeting_links(
        meeting_id=meeting_id,
        meeting_data=meeting_data,
        max_links=15
    )

    # Выводим результаты
    print("\n" + "="*60)
    print("📊 РЕЗУЛЬТАТЫ АНАЛИЗА СВЯЗЕЙ")
    print(f"   Встреча: {meeting_id}")
    print("="*60)

    links = result.get("links", [])
    if links:
        print(f"\n🔗 Найдено {len(links)} связанных встреч:\n")
        for i, link in enumerate(links, 1):
            print(f"  {i}. {link['target_meeting_id']}")
            print(f"     📈 Релевантность: {link['relevance_score']:.2%}")
            print(f"     🏷️ Тип связи: {link['link_type']}")
            if link.get('common_participants'):
                print(f"     👥 Общие участники: {', '.join(link['common_participants'][:5])}")
            if link.get('common_projects'):
                print(f"     📁 Общие проекты: {', '.join(link['common_projects'][:5])}")
            if link.get('common_topics'):
                print(f"     🏷️ Общие темы: {', '.join(link['common_topics'][:5])}")
            print()
    else:
        print("\n⚠️ Связанных встреч не найдено")

    # Эволюция сущностей
    evolution = result.get("entity_evolution", [])
    if evolution:
        print(f"\n📈 Эволюция сущностей ({len(evolution)}):\n")
        for ent in evolution[:5]:
            print(f"  • {ent['entity_name']} ({ent['entity_type']})")
            print(f"    Упоминаний: {ent.get('total_mentions', 0)}")

    # Повторяющиеся темы
    recurring = result.get("recurring_topics", [])
    if recurring:
        print(f"\n🔄 Повторяющиеся темы ({len(recurring)}):\n")
        for topic in recurring[:5]:
            print(f"  • {topic['topic']}: {topic['mention_count']} упоминаний")

    # Статистика
    stats = result.get("stats", {})
    print("\n📊 Статистика:")
    print(f"   • Время анализа: {stats.get('duration_seconds', 0):.2f}s")
    print(f"   • Встреч проанализировано: {stats.get('meetings_analyzed', 0)}")
    print(f"   • Связей найдено: {stats.get('links_found', 0)}")

    # Сохраняем результат
    output_path = Path("data/cross_meeting_analysis")
    output_path.mkdir(parents=True, exist_ok=True)

    output_file = output_path / f"analysis_{meeting_id[:20]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Результат сохранён: {output_file}")

    return result


async def analyze_all_meetings():
    """Анализирует связи всех встреч"""
    logger.info("🔗 Анализ связей всех встреч")

    # Инициализация
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    llm = LLMRouter()

    analyzer = CrossMeetingAnalyzer(graph_builder=graph, llm_router=llm)

    # Получаем все встречи
    nodes = graph.get_all_nodes()
    meetings = [n for n in nodes if n.get("_label") == "Meeting"]

    logger.info(f"📋 Найдено {len(meetings)} встреч")

    all_results = []
    for i, meeting in enumerate(meetings[:20], 1):  # Ограничиваем до 20
        meeting_id = meeting.get("id") or meeting.get("meeting_id")
        if not meeting_id:
            continue

        logger.info(f"[{i}/{min(len(meetings), 20)}] Анализ {meeting_id}")

        result = await analyzer.analyze_meeting_links(
            meeting_id=meeting_id,
            meeting_data=meeting,
            max_links=5
        )

        if result.get("links"):
            all_results.append({
                "meeting_id": meeting_id,
                "links_count": len(result.get("links", [])),
                "top_link": result["links"][0] if result.get("links") else None
            })

    # Выводим сводку
    print("\n" + "="*60)
    print("📊 СВОДКА ПО ВСЕМ ВСТРЕЧАМ")
    print("="*60)

    total_links = sum(r["links_count"] for r in all_results)
    meetings_with_links = len([r for r in all_results if r["links_count"] > 0])

    print("\n📈 Статистика:")
    print(f"   • Встреч проанализировано: {len(meetings)}")
    print(f"   • Встреч с связями: {meetings_with_links}")
    print(f"   • Всего связей найдено: {total_links}")
    print(f"   • Среднее связей на встречу: {total_links/max(len(all_results),1):.1f}")

    # Топ встречи по количеству связей
    top_meetings = sorted(all_results, key=lambda x: x["links_count"], reverse=True)[:10]
    if top_meetings:
        print("\n🏆 Топ-10 встреч по связям:")
        for i, m in enumerate(top_meetings, 1):
            print(f"   {i}. {m['meeting_id'][:30]}... - {m['links_count']} связей")

    return all_results


async def analyze_project_timeline(project_name: str):
    """Анализирует временную линию проекта"""
    logger.info(f"📅 Анализ timeline проекта: {project_name}")

    # Инициализация
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    llm = LLMRouter()

    CrossMeetingAnalyzer(graph_builder=graph, llm_router=llm)

    # Получаем все узлы
    nodes = graph.get_all_nodes()
    project_lower = project_name.lower()

    timeline = {
        "project": project_name,
        "meetings": [],
        "decisions": [],
        "tasks": [],
        "participants": set()
    }

    for node in nodes:
        label = node.get("_label", "")

        # Встречи с проектом
        if label == "Meeting":
            meeting_project = (node.get("project") or node.get("name") or "").lower()
            if project_lower in meeting_project:
                timeline["meetings"].append({
                    "id": node.get("id"),
                    "title": node.get("title") or node.get("name"),
                    "date": node.get("created_at") or node.get("date")
                })

        # Решения
        elif label == "Decision":
            dec_project = (node.get("project") or "").lower()
            if project_lower in dec_project:
                timeline["decisions"].append({
                    "text": node.get("text") or node.get("description"),
                    "meeting_id": node.get("meeting_id"),
                    "date": node.get("created_at")
                })

        # Задачи
        elif label == "Task":
            task_project = (node.get("project") or "").lower()
            if project_lower in task_project:
                timeline["tasks"].append({
                    "title": node.get("title") or node.get("description"),
                    "status": node.get("status"),
                    "assignee": node.get("assignee"),
                    "meeting_id": node.get("meeting_id")
                })

        # Участники
        elif label == "Person":
            person_project = (node.get("project") or "").lower()
            if project_lower in person_project:
                name = node.get("name")
                if name:
                    timeline["participants"].add(name)

    timeline["participants"] = list(timeline["participants"])

    # Выводим результаты
    print("\n" + "="*60)
    print(f"📅 TIMELINE ПРОЕКТА: {project_name}")
    print("="*60)

    print("\n📊 Статистика:")
    print(f"   • Встреч: {len(timeline['meetings'])}")
    print(f"   • Решений: {len(timeline['decisions'])}")
    print(f"   • Задач: {len(timeline['tasks'])}")
    print(f"   • Участников: {len(timeline['participants'])}")

    if timeline["meetings"]:
        print("\n📅 Встречи:")
        for m in sorted(timeline["meetings"], key=lambda x: x.get("date") or "", reverse=True)[:10]:
            print(f"   • {m.get('date', 'N/A')}: {m.get('title', 'N/A')[:50]}")

    if timeline["decisions"]:
        print("\n📋 Последние решения:")
        for d in timeline["decisions"][:5]:
            print(f"   • {d.get('text', 'N/A')[:60]}...")

    if timeline["participants"]:
        print(f"\n👥 Участники: {', '.join(timeline['participants'][:10])}")

    return timeline


async def main():
    parser = argparse.ArgumentParser(description="Анализ связей между встречами")
    parser.add_argument("--meeting-id", help="ID конкретной встречи для анализа")
    parser.add_argument("--all", action="store_true", help="Анализ всех встреч")
    parser.add_argument("--project", help="Анализ timeline проекта")

    args = parser.parse_args()

    if args.meeting_id:
        await analyze_single_meeting(args.meeting_id)
    elif args.all:
        await analyze_all_meetings()
    elif args.project:
        await analyze_project_timeline(args.project)
    else:
        # По умолчанию - анализ всех
        await analyze_all_meetings()


if __name__ == "__main__":
    asyncio.run(main())


