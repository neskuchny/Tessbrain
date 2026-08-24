"""Диагностика содержимого графа знаний"""
import json
from collections import Counter
from pathlib import Path


def main():
    graph_path = Path("data/tessent_brain_graph.json")

    if not graph_path.exists():
        print("❌ Граф не найден!")
        return

    with open(graph_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    print("=" * 60)
    print("📊 СОДЕРЖИМОЕ ГРАФА ЗНАНИЙ")
    print("=" * 60)

    # Подсчет по типам (метка в _label)
    labels = Counter(n.get("_label", n.get("label", "Unknown")) for n in nodes)
    print(f"\n📦 Узлы по типам ({len(nodes)} всего):")
    for label, count in labels.most_common():
        print(f"   {label}: {count}")

    # Access groups
    groups = Counter(n.get("access_group", "public") for n in nodes)
    print("\n🔐 Группы доступа:")
    for group, count in groups.most_common():
        print(f"   {group}: {count}")

    # Проекты
    projects = [n for n in nodes if n.get("_label") == "Project"]
    print(f"\n🏗️ Проекты ({len(projects)}):")
    if projects:
        for p in projects[:10]:
            print(f"   - {p.get('name', '?')}")
    else:
        print("   (нет проектов)")

    # Встречи
    meetings = [n for n in nodes if n.get("_label") == "Meeting"]
    print(f"\n📅 Встречи ({len(meetings)}):")
    for m in meetings[:10]:
        title = m.get("title", m.get("name", "?"))
        date = m.get("date", "?")
        print(f"   - {title} ({date})")

    # Люди
    people = [n for n in nodes if n.get("_label") == "Person"]
    print(f"\n👥 Люди ({len(people)}):")
    for p in people[:20]:
        name = p.get("name", "?")
        role = p.get("role", "")
        print(f"   - {name}" + (f" ({role})" if role else ""))

    # Задачи
    tasks = [n for n in nodes if n.get("_label") == "Task"]
    print(f"\n✅ Задачи ({len(tasks)}):")
    for t in tasks[:15]:
        title = (t.get("title", t.get("name", "?")))[:60]
        status = t.get("status", "?")
        assignee = t.get("assignee", "")
        print(f"   - [{status}] {title}" + (f" → {assignee}" if assignee else ""))

    # Решения
    decisions = [n for n in nodes if n.get("_label") == "Decision"]
    print(f"\n💡 Решения ({len(decisions)}):")
    for d in decisions[:10]:
        summary = (d.get("summary", d.get("name", "?")))[:60]
        print(f"   - {summary}")

    # Темы/Сущности
    entities = [n for n in nodes if n.get("_label") == "Entity"]
    print(f"\n🏷️ Сущности/Темы ({len(entities)}):")
    entity_types = Counter(e.get("entity_type", "unknown") for e in entities)
    for etype, count in entity_types.most_common(10):
        print(f"   {etype}: {count}")

    # Примеры сущностей
    print("\n   Примеры:")
    for e in entities[:15]:
        name = e.get("name", "?")
        etype = e.get("entity_type", "?")
        print(f"   - {name} ({etype})")

    # Связи
    print(f"\n🔗 Связи ({len(edges)}):")
    rel_types = Counter(e.get("type", "RELATED") for e in edges)
    for rtype, count in rel_types.most_common(10):
        print(f"   {rtype}: {count}")

    print("\n" + "=" * 60)
    print("✅ Диагностика завершена")

if __name__ == "__main__":
    main()

