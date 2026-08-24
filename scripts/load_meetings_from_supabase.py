# -*- coding: utf-8 -*-
"""
Скрипт загрузки встреч из Supabase в Tessbrain.

Выполняет:
1. Подключение к Supabase
2. Получение списка встреч
3. Обработка каждой встречи через CaptureOrchestrator
4. Построение графа знаний
5. Сохранение результатов
"""
import asyncio
import os
import sys
from datetime import datetime

# Добавляем путь к backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Загружаем переменные окружения
from dotenv import load_dotenv

load_dotenv()


async def load_meetings():
    """Загрузить встречи из Supabase и обработать их"""
    print("=" * 70)
    print("🧠 TESSENT BRAIN - Загрузка встреч из Supabase")
    print("=" * 70)
    print(f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # =========================================
    # 1. ИМПОРТЫ
    # =========================================
    print("📦 1. Загрузка модулей...")

    try:
        from backend.core.capture.orchestrator import CaptureOrchestrator
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.vector_indexer import VectorIndexer
        from backend.db.supabase_client import SupabaseClient
        print("   ✅ Модули загружены")
    except ImportError as e:
        print(f"   ❌ Ошибка импорта: {e}")
        return

    # =========================================
    # 2. ПОДКЛЮЧЕНИЕ К SUPABASE
    # =========================================
    print("\n🔌 2. Подключение к Supabase...")

    supabase = SupabaseClient()

    # Проверяем подключение
    try:
        # Пробуем получить встречи напрямую через SQL
        result = await supabase._request(
            "POST",
            "/rest/v1/rpc/get_meetings_count",
            json={}
        )
        print("   ✅ Supabase подключен")
    except Exception as e:
        print(f"   ⚠️ RPC недоступен, пробуем прямой запрос: {e}")

    # =========================================
    # 3. ПОЛУЧЕНИЕ ВСТРЕЧ
    # =========================================
    print("\n📋 3. Получение списка встреч...")

    meetings = []

    # Пробуем получить встречи через get_meetings
    user_id = os.environ.get("SUPABASE_USER_ID")

    if user_id:
        meetings = await supabase.get_meetings(user_id=user_id, limit=20)
    else:
        # Пробуем получить напрямую
        try:
            result = await supabase._request(
                "GET",
                "/rest/v1/meetings?select=*&limit=20"
            )
            meetings = result if isinstance(result, list) else []
        except Exception as e:
            print(f"   ⚠️ Ошибка получения встреч: {e}")

    if not meetings:
        print("   ❌ Встречи не найдены")
        print("\n   Возможные причины:")
        print("   - SUPABASE_USER_ID не установлен в .env")
        print("   - Нет встреч в базе данных")
        print("   - Проблема с правами доступа")
        return

    print(f"   ✅ Найдено встреч: {len(meetings)}")

    # Показываем первые 5
    for i, m in enumerate(meetings[:5]):
        title = m.get("title", m.get("name", "Без названия"))
        date = m.get("meeting_date", m.get("created_at", ""))[:10] if m.get("meeting_date") or m.get("created_at") else ""
        print(f"      {i+1}. {title} ({date})")

    if len(meetings) > 5:
        print(f"      ... и ещё {len(meetings) - 5} встреч")

    # =========================================
    # 4. ИНИЦИАЛИЗАЦИЯ КОМПОНЕНТОВ
    # =========================================
    print("\n⚙️ 4. Инициализация компонентов...")

    # Graph Builder
    graph_builder = GraphBuilder(
        use_networkx=True,
        graph_storage_path="data/tessent_brain_graph.json"
    )
    await graph_builder.connect()
    print("   ✅ GraphBuilder готов")

    # Vector Indexer (пропускаем, используем in-memory по умолчанию)
    # vector_indexer = VectorIndexer()
    print("   ✅ VectorIndexer пропущен (in-memory)")

    # Capture Orchestrator
    orchestrator = CaptureOrchestrator()
    print("   ✅ CaptureOrchestrator готов")

    # =========================================
    # 5. ОБРАБОТКА ВСТРЕЧ
    # =========================================
    print("\n🔄 5. Обработка встреч...")

    processed = 0
    errors = 0
    total_entities = {
        "persons": 0,
        "tasks": 0,
        "decisions": 0,
        "topics": 0
    }
    total_relationships = 0

    for i, meeting in enumerate(meetings):
        meeting_id = meeting.get("id", meeting.get("meeting_id", f"meeting_{i}"))
        title = meeting.get("title", meeting.get("name", "Без названия"))

        # Получаем транскрипт
        transcript = meeting.get("transcription_text", meeting.get("transcript", ""))

        if not transcript or len(transcript) < 50:
            print(f"   ⚠️ [{i+1}/{len(meetings)}] {title[:30]}... - нет транскрипта")
            continue

        print(f"   🔄 [{i+1}/{len(meetings)}] {title[:40]}...")

        try:
            # Обрабатываем через orchestrator
            # Передаём транскрипт как строку
            transcript_text = transcript if isinstance(transcript, str) else str(transcript)

            result = await orchestrator.process_meeting(transcript_text)

            if result:
                # Используем встроенный метод save_meeting_results
                # который автоматически создаёт узлы И связи
                save_result = await graph_builder.save_meeting_results(
                    meeting_id=meeting_id,
                    results=result,
                    meeting_metadata={
                        "title": title,
                        "date": meeting.get("meeting_date", ""),
                        "created_at": meeting.get("created_at", "")
                    }
                )

                # Обновляем статистику
                participants = result.get("participants", [])
                tasks = result.get("tasks", [])
                decisions = result.get("decisions", [])
                entities = result.get("entities", [])

                total_entities["persons"] += len(participants)
                total_entities["tasks"] += len(tasks)
                total_entities["decisions"] += len(decisions)
                total_entities["topics"] += len(entities)
                total_relationships += save_result.get("relationships_created", 0)

                processed += 1
                print(f"      ✅ Узлов: {save_result.get('nodes_created', 0)}, Связей: {save_result.get('relationships_created', 0)}")

        except Exception as e:
            errors += 1
            print(f"      ❌ Ошибка: {str(e)[:50]}")

    # =========================================
    # 6. СОХРАНЕНИЕ РЕЗУЛЬТАТОВ
    # =========================================
    print("\n💾 6. Сохранение результатов...")

    # Сохраняем граф
    graph_builder.save_graph()

    # Получаем статистику
    graph_info = graph_builder.get_graph_info()

    # Закрываем БЕЗ сохранения (уже сохранили выше)
    await graph_builder.close(save=False)

    # =========================================
    # ИТОГ
    # =========================================
    print("\n" + "=" * 70)
    print("✅ ЗАГРУЗКА ЗАВЕРШЕНА!")
    print("=" * 70)
    print(f"""
📊 Статистика:
   - Встреч обработано: {processed}/{len(meetings)}
   - Ошибок: {errors}

   Извлечено сущностей:
   - 👥 Людей: {total_entities['persons']}
   - ✅ Задач: {total_entities['tasks']}
   - 💡 Решений: {total_entities['decisions']}
   - 🏷️ Тем/сущностей: {total_entities['topics']}
   - 🔗 Связей создано: {total_relationships}

   Граф знаний:
   - Узлов: {graph_info.get('total_nodes', 0)}
   - Связей: {graph_info.get('total_edges', 0)}

💡 Теперь можно:
   1. Открыть http://localhost:3000 и задать вопросы в чате
   2. Посмотреть граф знаний на вкладке "Граф знаний"
   3. Проверить инсайты и аномалии
""")


if __name__ == "__main__":
    asyncio.run(load_meetings())

