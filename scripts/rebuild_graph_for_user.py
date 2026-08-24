# -*- coding: utf-8 -*-
"""
Скрипт для пересборки графа для конкретного пользователя.

Использование:
    python scripts/rebuild_graph_for_user.py --user-id <USER_ID>

Пример:
    python scripts/rebuild_graph_for_user.py --user-id 7b16a506-a775-411a-96a7-46cbbaa4c7e2
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Добавляем путь к backend
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.store.graph_builder import GraphBuilder
from backend.db.supabase_client import SupabaseClient, set_user_context


async def rebuild_graph(user_id: str, process_meetings: bool = True):
    """
    Пересобрать граф для пользователя.

    Args:
        user_id: ID пользователя
        process_meetings: Обрабатывать ли встречи (требует LLM)
    """
    print(f"\n{'='*60}")
    print(f"🔄 REBUILD GRAPH FOR USER: {user_id}")
    print(f"{'='*60}\n")

    # 1. Создаём новый пустой граф
    print("📊 Создаю новый граф...")
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    # Очищаем граф
    graph.nx_graph.clear()
    print("   ✅ Граф очищен")

    # 2. Подключаемся к Supabase
    print("\n📡 Подключаюсь к Supabase...")
    supabase = SupabaseClient()
    set_user_context(user_id=user_id)

    # 3. Загружаем проекты пользователя
    print(f"\n📁 Загружаю проекты для user_id={user_id[:8]}...")
    projects = await supabase._request(
        "GET", "/rest/v1/projects",
        params={
            "user_id": f"eq.{user_id}",
            "status": "eq.active",
            "select": "project_id,name,description,created_at,status",
            "order": "created_at.desc"
        }
    )
    print(f"   ✅ Найдено {len(projects)} проектов")

    # Добавляем проекты в граф
    for project in projects:
        node_id = f"Project_{project['project_id'][:8]}"
        graph.nx_graph.add_node(
            node_id,
            id=node_id,
            project_id=project['project_id'],
            name=project['name'],
            description=project.get('description', ''),
            created_at=project['created_at'],
            status=project['status'],
            _label="Project",
            _key=project['project_id'],
            user_id=user_id,
            access_group="private"
        )
        print(f"   + Project: {project['name']}")

    # 4. Загружаем папки
    print("\n📂 Загружаю папки...")
    folders = await supabase._request(
        "GET", "/rest/v1/folders",
        params={
            "user_id": f"eq.{user_id}",
            "status": "eq.active",
            "select": "id,name,project_id,parent_folder_id,created_at",
            "order": "created_at.desc"
        }
    )
    print(f"   ✅ Найдено {len(folders)} папок")

    for folder in folders:
        node_id = f"Folder_{folder['id'][:8]}"
        graph.nx_graph.add_node(
            node_id,
            id=node_id,
            folder_id=folder['id'],
            name=folder['name'],
            project_id=folder.get('project_id', ''),
            parent_folder_id=folder.get('parent_folder_id', ''),
            created_at=folder['created_at'],
            _label="Folder",
            _key=folder['id'],
            user_id=user_id,
            access_group="private"
        )

        # Связь с проектом
        if folder.get('project_id'):
            project_node = f"Project_{folder['project_id'][:8]}"
            if graph.nx_graph.has_node(project_node):
                graph.nx_graph.add_edge(node_id, project_node, relation="BELONGS_TO")

    # 5. Загружаем встречи
    print("\n📋 Загружаю встречи...")
    meetings = await supabase.get_meetings(user_id=user_id, limit=200)
    print(f"   ✅ Найдено {len(meetings)} встреч")

    for meeting in meetings:
        node_id = f"Meeting_{meeting['meeting_id'][:8]}"
        graph.nx_graph.add_node(
            node_id,
            id=node_id,
            meeting_id=meeting['meeting_id'],
            title=meeting['title'],
            created_at=meeting['created_at'],
            transcription_status=meeting.get('transcription_status', ''),
            meeting_type=meeting.get('meeting_type', ''),
            project_id=meeting.get('project_id', ''),
            folder_id=meeting.get('folder_id', ''),
            _label="Meeting",
            _key=meeting['meeting_id'],
            user_id=user_id,
            access_group="private"
        )

        # Связь с проектом
        if meeting.get('project_id'):
            project_node = f"Project_{meeting['project_id'][:8]}"
            if graph.nx_graph.has_node(project_node):
                graph.nx_graph.add_edge(node_id, project_node, relation="BELONGS_TO")

        # Связь с папкой
        if meeting.get('folder_id'):
            folder_node = f"Folder_{meeting['folder_id'][:8]}"
            if graph.nx_graph.has_node(folder_node):
                graph.nx_graph.add_edge(node_id, folder_node, relation="IN_FOLDER")

    # 6. Добавляем узел Organization/Company
    print("\n🏢 Создаю узел Organization...")
    org_node_id = "Organization_main"
    graph.nx_graph.add_node(
        org_node_id,
        id=org_node_id,
        name="Моя компания",
        description="Основная организация пользователя",
        type="Organization",
        _label="Organization",
        _key="main_org",
        user_id=user_id,
        created_at=datetime.now().isoformat(),
        # Метрики будут обновляться
        total_projects=len(projects),
        total_meetings=len(meetings),
        total_folders=len(folders),
        access_group="private"
    )

    # Связываем проекты с организацией
    for project in projects:
        project_node = f"Project_{project['project_id'][:8]}"
        if graph.nx_graph.has_node(project_node):
            graph.nx_graph.add_edge(project_node, org_node_id, relation="OWNED_BY")

    # 7. Сохраняем граф
    print("\n💾 Сохраняю граф...")
    graph.save_graph()

    total_nodes = graph.nx_graph.number_of_nodes()
    total_edges = graph.nx_graph.number_of_edges()

    print(f"\n{'='*60}")
    print("✅ ГРАФ УСПЕШНО ПЕРЕСОБРАН!")
    print(f"{'='*60}")
    print(f"   📊 Узлов: {total_nodes}")
    print(f"   🔗 Связей: {total_edges}")
    print(f"   📁 Проектов: {len(projects)}")
    print(f"   📂 Папок: {len(folders)}")
    print(f"   📋 Встреч: {len(meetings)}")
    print("\n   Файл: data/tessent_brain_graph.json")
    print(f"{'='*60}\n")

    return {
        "nodes": total_nodes,
        "edges": total_edges,
        "projects": len(projects),
        "folders": len(folders),
        "meetings": len(meetings)
    }


async def main():
    parser = argparse.ArgumentParser(description="Rebuild graph for user")
    parser.add_argument("--user-id", required=True, help="User ID from Supabase")
    parser.add_argument("--process-meetings", action="store_true", help="Process meeting transcripts with LLM")

    args = parser.parse_args()

    result = await rebuild_graph(args.user_id, args.process_meetings)
    return result


if __name__ == "__main__":
    asyncio.run(main())

