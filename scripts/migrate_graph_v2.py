#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Скрипт миграции графа на новую версию (v2).

Добавляет новые поля к узлам и рёбрам:

Узлы:
- weight (float, default=1.0)
- access_count (int, default=0)
- frequency_score (float, default=0.0)
- total_mentions (int, default=0)
- emotional_salience (float, default=0.0)
- last_accessed (str, nullable)

Рёбра:
- weight (float, default=1.0)
- co_occurrences (int, default=1)
- description (str, default="")
- last_strengthened (str, nullable)

Использование:
    python scripts/migrate_graph_v2.py --graph-path data/knowledge_graph.json
    python scripts/migrate_graph_v2.py --graph-path data/knowledge_graph.json --backup
    python scripts/migrate_graph_v2.py --graph-path data/knowledge_graph.json --dry-run
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def load_graph(path: Path) -> Dict[str, Any]:
    """Загрузить граф из JSON."""
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_graph(path: Path, data: Dict[str, Any]):
    """Сохранить граф в JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_backup(path: Path) -> Path:
    """Создать backup файла."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.parent / f"{path.stem}_backup_{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)
    print(f"[BACKUP] Created: {backup_path}")
    return backup_path


def migrate_nodes(nodes: List[Dict[str, Any]]) -> tuple[int, int]:
    """
    Мигрировать узлы.

    Returns:
        (migrated_count, skipped_count)
    """
    migrated = 0
    skipped = 0

    default_node_fields = {
        "weight": 1.0,
        "access_count": 0,
        "frequency_score": 0.0,
        "total_mentions": 0,
        "emotional_salience": 0.0,
        "last_accessed": None,
    }

    for node in nodes:
        updated = False

        for field, default_value in default_node_fields.items():
            if field not in node:
                node[field] = default_value
                updated = True

        if updated:
            migrated += 1
        else:
            skipped += 1

    return migrated, skipped


def migrate_edges(edges: List[Dict[str, Any]]) -> tuple[int, int]:
    """
    Мигрировать рёбра.

    Returns:
        (migrated_count, skipped_count)
    """
    migrated = 0
    skipped = 0

    default_edge_fields = {
        "weight": 1.0,
        "co_occurrences": 1,
        "description": "",
        "last_strengthened": None,
    }

    for edge in edges:
        updated = False

        # Рёбра могут иметь вложенную структуру data
        data = edge.get("data", edge)

        for field, default_value in default_edge_fields.items():
            if field not in data:
                data[field] = default_value
                updated = True

        # Если data была отдельная, обновляем edge
        if "data" in edge and data is not edge:
            edge["data"] = data

        if updated:
            migrated += 1
        else:
            skipped += 1

    return migrated, skipped


def add_metadata(graph: Dict[str, Any]):
    """Добавить метаданные миграции."""
    if "metadata" not in graph:
        graph["metadata"] = {}

    graph["metadata"]["version"] = "2.0"
    graph["metadata"]["migrated_at"] = datetime.now(timezone.utc).isoformat()
    graph["metadata"]["features"] = [
        "node_weights",
        "edge_weights",
        "frequency_tracking",
        "emotional_salience",
    ]


def migrate_graph(
    graph_path: str,
    backup: bool = True,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Выполнить миграцию графа.

    Args:
        graph_path: Путь к файлу графа
        backup: Создать backup перед миграцией
        dry_run: Только показать что будет сделано

    Returns:
        Статистика миграции
    """
    path = Path(graph_path)

    print(f"[MIGRATE] Graph: {path}")
    print(f"   Backup: {'yes' if backup else 'no'}")
    print(f"   Dry run: {'yes' if dry_run else 'no'}")
    print()

    # Загружаем граф
    graph = load_graph(path)

    # Проверяем структуру
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", graph.get("links", []))

    print("[STATS] Current state:")
    print(f"   Nodes: {len(nodes)}")
    print(f"   Edges: {len(edges)}")
    print()

    # Проверяем версию
    current_version = graph.get("metadata", {}).get("version", "1.0")
    if current_version == "2.0":
        print("[INFO] Graph already migrated to v2.0")
        return {
            "status": "already_migrated",
            "version": current_version,
        }

    # Создаём backup
    if backup and not dry_run:
        create_backup(path)

    # Мигрируем узлы
    nodes_migrated, nodes_skipped = migrate_nodes(nodes)
    print("[NODES]")
    print(f"   Migrated: {nodes_migrated}")
    print(f"   Skipped: {nodes_skipped}")

    # Мигрируем рёбра
    edges_migrated, edges_skipped = migrate_edges(edges)
    print("[EDGES]")
    print(f"   Migrated: {edges_migrated}")
    print(f"   Skipped: {edges_skipped}")

    # Добавляем метаданные
    add_metadata(graph)

    # Сохраняем
    if not dry_run:
        save_graph(path, graph)
        print()
        print(f"[OK] Migration complete! Graph saved: {path}")
    else:
        print()
        print("[DRY RUN] Complete. Changes NOT saved.")

    return {
        "status": "success",
        "nodes_migrated": nodes_migrated,
        "nodes_skipped": nodes_skipped,
        "edges_migrated": edges_migrated,
        "edges_skipped": edges_skipped,
        "new_version": "2.0",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Миграция графа знаний на версию 2.0"
    )
    parser.add_argument(
        "--graph-path",
        type=str,
        default="data/knowledge_graph.json",
        help="Путь к файлу графа"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Создать backup перед миграцией"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать что будет сделано"
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Мигрировать графы всех пользователей"
    )

    args = parser.parse_args()

    if args.all_users:
        # Мигрируем все графы пользователей
        users_dir = Path("data/users")
        if not users_dir.exists():
            print(f"[ERROR] Directory not found: {users_dir}")
            sys.exit(1)

        total_stats = {
            "users_processed": 0,
            "nodes_migrated": 0,
            "edges_migrated": 0,
        }

        for user_dir in users_dir.iterdir():
            if user_dir.is_dir():
                graph_path = user_dir / "knowledge_graph.json"
                if graph_path.exists():
                    print(f"\n{'='*60}")
                    print(f"Пользователь: {user_dir.name}")
                    print(f"{'='*60}")

                    stats = migrate_graph(
                        str(graph_path),
                        backup=args.backup,
                        dry_run=args.dry_run
                    )

                    if stats.get("status") == "success":
                        total_stats["users_processed"] += 1
                        total_stats["nodes_migrated"] += stats.get("nodes_migrated", 0)
                        total_stats["edges_migrated"] += stats.get("edges_migrated", 0)

        print(f"\n{'='*60}")
        print("ИТОГО:")
        print(f"  Пользователей обработано: {total_stats['users_processed']}")
        print(f"  Узлов мигрировано: {total_stats['nodes_migrated']}")
        print(f"  Рёбер мигрировано: {total_stats['edges_migrated']}")
    else:
        migrate_graph(
            args.graph_path,
            backup=args.backup,
            dry_run=args.dry_run
        )


if __name__ == "__main__":
    main()

