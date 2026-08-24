# -*- coding: utf-8 -*-
"""
Скрипт для пересборки векторного индекса из всех узлов графа.

Индексирует:
- Meeting: title + summary
- Person: name + role + description
- Task: title + description + status
- Decision: summary + description
- Entity: name + description + entity_type
- KPI: name + value + description
- Project: name + description
"""
import asyncio
import sys
from pathlib import Path

# Добавляем путь к проекту
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def rebuild_vector_index():
    """Пересобрать векторный индекс из графа."""
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.vector_indexer import VectorIndexer

    # Загружаем граф
    logger.info("📊 Загружаю граф...")
    graph_builder = GraphBuilder(
        use_networkx=True,
        graph_storage_path="data/tessent_brain_graph.json"
    )
    await graph_builder.connect()

    graph = graph_builder.nx_graph
    if not graph:
        logger.error("❌ Граф не загружен!")
        return

    node_count = graph.number_of_nodes()
    logger.info(f"✅ Граф загружен: {node_count} узлов, {graph.number_of_edges()} рёбер")

    # Инициализируем векторный индексер
    logger.info("🔍 Инициализирую векторный индексер...")
    indexer = VectorIndexer(
        use_qdrant=False,  # In-memory для простоты
        storage_path="data/vector_index.json",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2"  # Явно указываем модель
    )
    await indexer.connect()

    # Очищаем старый индекс
    logger.info("🗑️ Очищаю старый индекс...")
    # Очищаем все коллекции
    for collection_name in indexer.COLLECTIONS.values():
        indexer.memory_index[collection_name] = []

    # Собираем узлы по типам
    nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

    stats = {
        "Meeting": 0,
        "Person": 0,
        "Task": 0,
        "Decision": 0,
        "Entity": 0,
        "KPI": 0,
        "Project": 0,
        "Folder": 0,
        "Organization": 0,
        "Other": 0,
    }

    indexed = 0
    errors = 0

    for node in nodes:
        label = node.get("_label", "Unknown")
        node_id = node.get("id", "")

        try:
            # Создаём текст для индексации в зависимости от типа
            text = ""
            metadata = {
                "node_id": node_id,
                "label": label,
                "access_group": node.get("access_group", "public"),
            }

            if label == "Meeting":
                title = node.get("title", "")
                summary = node.get("summary", "")
                date = node.get("date", node.get("created_at", ""))
                text = f"Встреча: {title}. {summary}"
                metadata["title"] = title
                metadata["date"] = date
                stats["Meeting"] += 1

            elif label == "Person":
                name = node.get("name", "")
                role = node.get("role", "")
                description = node.get("description", "")
                text = f"Человек: {name}. Роль: {role}. {description}"
                metadata["name"] = name
                metadata["role"] = role
                stats["Person"] += 1

            elif label == "Task":
                title = node.get("title", "")
                description = node.get("description", "")
                status = node.get("status", "pending")
                assignee = node.get("assignee", "")
                text = f"Задача: {title}. {description}. Статус: {status}. Исполнитель: {assignee}"
                metadata["title"] = title
                metadata["status"] = status
                metadata["assignee"] = assignee
                stats["Task"] += 1

            elif label == "Decision":
                summary = node.get("summary", node.get("description", ""))
                rationale = node.get("rationale", "")
                text = f"Решение: {summary}. Обоснование: {rationale}"
                metadata["summary"] = summary
                stats["Decision"] += 1

            elif label == "Entity":
                name = node.get("name", "")
                entity_type = node.get("entity_type", "")
                description = node.get("description", "")
                text = f"Сущность ({entity_type}): {name}. {description}"
                metadata["name"] = name
                metadata["entity_type"] = entity_type
                stats["Entity"] += 1

            elif label == "KPI":
                name = node.get("name", "")
                value = node.get("value", "")
                unit = node.get("unit", "")
                description = node.get("description", "")
                text = f"KPI: {name} = {value} {unit}. {description}"
                metadata["name"] = name
                metadata["value"] = str(value)
                stats["KPI"] += 1

            elif label == "Project":
                name = node.get("name", "")
                description = node.get("description", "")
                text = f"Проект: {name}. {description}"
                metadata["name"] = name
                stats["Project"] += 1

            elif label == "Folder":
                name = node.get("name", "")
                text = f"Папка: {name}"
                metadata["name"] = name
                stats["Folder"] += 1

            elif label == "Organization":
                name = node.get("name", "")
                description = node.get("description", "")
                text = f"Организация: {name}. {description}"
                metadata["name"] = name
                stats["Organization"] += 1

            else:
                # Пробуем извлечь что-то полезное
                name = node.get("name", node.get("title", ""))
                description = node.get("description", node.get("summary", ""))
                if name or description:
                    text = f"{label}: {name}. {description}"
                    metadata["name"] = name
                    stats["Other"] += 1

            # Индексируем если есть текст
            if text and len(text.strip()) > 10:
                # Создаём эмбеддинг
                embedding = indexer._get_embedding(text)
                if embedding is not None:
                    # Определяем коллекцию по типу
                    collection_map = {
                        "Meeting": "tessent_meetings",
                        "Person": "tessent_participants",
                        "Task": "tessent_tasks",
                        "Decision": "tessent_decisions",
                        "Entity": "tessent_entities",
                        "KPI": "tessent_kpis",
                        "Project": "tessent_entities",
                        "Folder": "tessent_entities",
                        "Organization": "tessent_entities",
                    }
                    collection = collection_map.get(label, "tessent_chunks")

                    # Добавляем в memory_index
                    if collection not in indexer.memory_index:
                        indexer.memory_index[collection] = []

                    indexer.memory_index[collection].append({
                        "id": node_id,
                        "vector": embedding,
                        "metadata": metadata,
                        "text": text[:500]  # Сохраняем текст для отладки
                    })
                    indexed += 1

        except Exception as e:
            logger.warning(f"⚠️ Ошибка при индексации узла {node_id}: {e}")
            errors += 1

    # Сохраняем индекс
    logger.info("💾 Сохраняю индекс...")
    indexer.save_index()

    # Выводим статистику
    logger.info("\n" + "="*50)
    logger.info("📊 СТАТИСТИКА ИНДЕКСАЦИИ")
    logger.info("="*50)
    for label, count in sorted(stats.items(), key=lambda x: -x[1]):
        if count > 0:
            logger.info(f"   {label}: {count}")
    logger.info("-"*50)
    logger.info(f"✅ Всего проиндексировано: {indexed} узлов")
    logger.info(f"⚠️ Ошибок: {errors}")
    total_vectors = sum(len(v) for v in indexer.memory_index.values())
    logger.info(f"📦 Размер индекса: {total_vectors} векторов")
    logger.info("="*50)

    # Закрываем соединения
    await graph_builder.close()
    await indexer.close()

    return indexed


if __name__ == "__main__":
    result = asyncio.run(rebuild_vector_index())
    print(f"\n✅ Готово! Проиндексировано {result} узлов.")

