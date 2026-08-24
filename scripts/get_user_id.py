# -*- coding: utf-8 -*-
"""
Скрипт для получения user_id из Supabase.

Использование:
    python scripts/get_user_id.py
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.supabase_client import SupabaseClient


async def get_users():
    """Получить список пользователей из Supabase."""
    supabase = SupabaseClient()

    print("\n📋 Получаю список пользователей из Supabase...")

    try:
        # Получаем уникальных пользователей из projects
        projects = await supabase._request(
            'GET', '/rest/v1/projects',
            params={
                'select': 'user_id,name,created_at',
                'status': 'eq.active',
                'order': 'created_at.desc',
                'limit': '100'
            }
        )

        # Группируем по user_id
        users = {}
        for p in projects:
            uid = p.get('user_id')
            if uid:
                if uid not in users:
                    users[uid] = {
                        'user_id': uid,
                        'projects_count': 0,
                        'latest_project': None
                    }
                users[uid]['projects_count'] += 1
                if not users[uid]['latest_project']:
                    users[uid]['latest_project'] = p.get('name')

        print(f"\n✅ Найдено {len(users)} уникальных пользователей:\n")

        for i, (uid, info) in enumerate(users.items(), 1):
            print(f"  {i}. user_id: {uid}")
            print(f"     Проектов: {info['projects_count']}")
            print(f"     Последний проект: {info['latest_project']}")
            print()

        if users:
            # Возвращаем первого пользователя с наибольшим количеством проектов
            main_user = max(users.items(), key=lambda x: x[1]['projects_count'])
            print("\n🎯 Рекомендуемый user_id (больше всего проектов):")
            print(f"   {main_user[0]}")
            return main_user[0]

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return None

    finally:
        await supabase.close()


if __name__ == "__main__":
    user_id = asyncio.run(get_users())
    if user_id:
        print("\n💡 Для обогащения графа используйте:")
        print(f"   python scripts/enrich_graph_from_meetings.py --user-id {user_id} --limit 20")



