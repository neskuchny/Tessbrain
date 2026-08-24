# -*- coding: utf-8 -*-
"""
Скрипт для построения Company Snapshot из существующего графа.
"""
import asyncio
import os
import sys

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from backend.core.llm.router import get_llm_router
from backend.core.sleep.company_snapshot_agent import CompanySnapshotAgent
from backend.core.store.graph_builder import GraphBuilder


async def main():
    print("=" * 60)
    print("🏢 REBUILDING COMPANY SNAPSHOT FROM GRAPH")
    print("=" * 60)

    # Инициализируем компоненты
    print("\n📦 Initializing components...")

    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()
    print(f"   ✅ Graph loaded: {graph.nx_graph.number_of_nodes()} nodes, {graph.nx_graph.number_of_edges()} edges")

    llm = get_llm_router()
    print("   ✅ LLM router initialized")

    # Создаём агент
    snapshot_agent = CompanySnapshotAgent(
        llm_client=llm,
        graph_builder=graph,
        storage_path="data/snapshots"
    )
    print("   ✅ Company Snapshot Agent created")

    # Перестраиваем snapshot из графа
    print("\n🔄 Rebuilding snapshot from graph...")
    snapshot = await snapshot_agent.rebuild_from_graph()

    # Выводим результат
    print("\n" + "=" * 60)
    print("📊 COMPANY SNAPSHOT RESULT")
    print("=" * 60)

    company = snapshot.get("company", {})
    print(f"\n🏢 Company: {company.get('name', 'Not set')}")
    if company.get("description"):
        print(f"   {company.get('description')[:100]}...")

    team = snapshot.get("team", [])
    print(f"\n👥 Team ({len(team)} people):")
    for member in team[:10]:
        role = f" ({member.get('role')})" if member.get("role") else ""
        print(f"   - {member.get('name')}{role}")
    if len(team) > 10:
        print(f"   ... and {len(team) - 10} more")

    projects = snapshot.get("projects", [])
    print(f"\n🚀 Projects ({len(projects)}):")
    for proj in projects[:5]:
        status = proj.get("status", "unknown")
        progress = proj.get("progress", 0)
        print(f"   - {proj.get('name')} [{progress}%] ({status})")
    if len(projects) > 5:
        print(f"   ... and {len(projects) - 5} more")

    tasks = snapshot.get("critical_tasks", [])
    print(f"\n📋 Critical Tasks ({len(tasks)}):")
    for task in tasks[:5]:
        print(f"   - {task.get('task', 'Unknown')[:50]}")

    # Выводим текстовое представление для промпта
    print("\n" + "=" * 60)
    print("📝 SNAPSHOT FOR LLM PROMPT")
    print("=" * 60)

    prompt_text = snapshot_agent.get_snapshot_for_prompt()
    print(prompt_text[:2000])
    if len(prompt_text) > 2000:
        print(f"\n... ({len(prompt_text)} chars total)")

    print("\n" + "=" * 60)
    print("✅ COMPANY SNAPSHOT REBUILT SUCCESSFULLY!")
    print("=" * 60)
    print("\n📁 Saved to: data/snapshots/current_snapshot.json")


if __name__ == "__main__":
    asyncio.run(main())


