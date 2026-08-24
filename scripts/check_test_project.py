# -*- coding: utf-8 -*-
import asyncio

from dotenv import load_dotenv

load_dotenv()

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.supabase_client import SupabaseClient, set_user_context


async def check():
    supabase = SupabaseClient()
    user_id = '7b16a506-a775-411a-96a7-46cbbaa4c7e2'
    set_user_context(user_id=user_id)

    # Находим проект "Тестовые данные"
    projects = await supabase._request(
        'GET', '/rest/v1/projects',
        params={
            'user_id': f'eq.{user_id}',
            'status': 'eq.active',
            'select': 'project_id,name',
        }
    )

    print("Проекты:")
    test_project_id = None
    for p in projects:
        print(f"  - {p.get('name')} ({p.get('project_id')[:8]}...)")
        if 'тестовые' in p.get('name', '').lower() or 'test' in p.get('name', '').lower():
            test_project_id = p.get('project_id')

    if test_project_id:
        print(f"\n✅ Найден проект 'Тестовые данные': {test_project_id}")

        # Получаем встречи из этого проекта
        meetings = await supabase._request(
            'GET', '/rest/v1/meetings',
            params={
                'user_id': f'eq.{user_id}',
                'project_id': f'eq.{test_project_id}',
                'status': 'eq.active',
                'transcription_status': 'eq.completed',
                'select': 'id,meeting_id,title,transcription_status,created_at',
                'order': 'created_at.desc',
                'limit': '50'
            }
        )

        print(f"\nВстречи в проекте с транскриптами: {len(meetings)}")
        for m in meetings[:15]:
            print(f"  - {m.get('title', 'Без названия')[:50]} | {m.get('meeting_id')[:8]}...")

        # Проверим один транскрипт
        if meetings:
            first_meeting = meetings[0]
            print(f"\nПроверяю транскрипт для: {first_meeting.get('title')}")

            transcript = await supabase.get_meeting_transcript(first_meeting.get('meeting_id'))
            if transcript:
                print(f"  ✅ Транскрипт: {len(transcript)} символов")
                print(f"  Начало: {transcript[:200]}...")
            else:
                print("  ❌ Транскрипт не найден")

asyncio.run(check())

