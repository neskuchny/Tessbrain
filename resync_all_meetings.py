# -*- coding: utf-8 -*-
"""
Скрипт для пересинхронизации всех встреч пользователя.
Это необходимо для того, чтобы текст сохранился в payload векторного индекса.

Запуск:
    TESSBRAIN_USER_ID=<uuid> python resync_all_meetings.py
    python resync_all_meetings.py <uuid>
"""
import asyncio
import os
import sys

# Добавляем путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _resolve_user_id() -> str:
    """Чей граф пересобираем — из аргумента или окружения.

    Раньше здесь стоял конкретный uuid владельца. В приватном репозитории
    это просто неудобно (скрипт работал ровно для одного человека), а при
    публикации становится утечкой: идентификатор связывает открытый код с
    живым аккаунтом, и отозвать его после публикации нельзя.
    """
    user_id = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or \
        (os.getenv("TESSBRAIN_USER_ID") or "").strip()
    if not user_id:
        print("Не указан пользователь. Передайте uuid аргументом или в "
              "TESSBRAIN_USER_ID:\n"
              "    TESSBRAIN_USER_ID=<uuid> python resync_all_meetings.py",
              file=sys.stderr)
        raise SystemExit(2)
    return user_id


async def main():
    from backend.core.knowledge_sync import full_meeting_indexing
    from backend.db.supabase_client import get_supabase_client

    user_id = _resolve_user_id()
    # Получаем все встречи
    supabase = await get_supabase_client()
    meetings = await supabase.get_meetings(user_id)


    # Синхронизируем каждую встречу
    for _i, meeting in enumerate(meetings):
        meeting_id = meeting.get("id")
        meeting.get("title", "Untitled")


        try:
            await full_meeting_indexing(
                meeting_id=meeting_id,
                user_id=user_id,
                force=True  # Принудительная пересинхронизация
            )
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

