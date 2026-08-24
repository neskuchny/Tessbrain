# -*- coding: utf-8 -*-
import asyncio

from dotenv import load_dotenv

load_dotenv()

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.supabase_client import SupabaseClient


async def check():
    supabase = SupabaseClient()
    user_id = '7b16a506-a775-411a-96a7-46cbbaa4c7e2'

    # Ищем проект Тестовые данные
    projects = await supabase._request(
        'GET', '/rest/v1/projects',
        params={
            'user_id': f'eq.{user_id}',
            'status': 'eq.active',
            'select': 'project_id,name'
        }
    )
    print('Проекты:')
    test_project_id = None
    for p in projects:
        name = p.get("name", "")
        pid = p.get("project_id", "")[:8]
        print(f'  - {name} ({pid}...)')
        if 'тестов' in name.lower() or 'test' in name.lower():
            test_project_id = p['project_id']

    if test_project_id:
        print(f'\n>>> Проект "Тестовые данные": {test_project_id}')

        # Получаем встречи из этого проекта
        meetings = await supabase._request(
            'GET', '/rest/v1/meetings',
            params={
                'project_id': f'eq.{test_project_id}',
                'status': 'eq.active',
                'select': 'id,meeting_id,title,transcription_status',
                'order': 'created_at.desc',
                'limit': '30'
            }
        )
        print(f'\nВстречи в проекте: {len(meetings)}')

        with_transcripts = 0
        for m in meetings[:20]:
            status = m.get('transcription_status', 'none')
            title = m.get("title", "")[:45]
            if status == 'completed':
                with_transcripts += 1
            print(f'  - {title} [{status}]')

        print(f'\n>>> С транскриптами: {with_transcripts}')
    else:
        print('\n>>> Проект "Тестовые данные" не найден')

asyncio.run(check())



