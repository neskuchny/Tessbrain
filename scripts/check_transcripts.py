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

    # Проверяем транскрипты
    result = await supabase._request(
        'GET', '/rest/v1/meetings',
        params={
            'user_id': f'eq.{user_id}',
            'status': 'eq.active',
            'transcription_status': 'eq.completed',
            'select': 'id,title,transcription_status',
            'limit': '20'
        }
    )
    print(f'Встречи с транскриптами (completed): {len(result)}')
    for m in result[:10]:
        print(f'  - {m.get("title", "")[:50]}')

    # Проверяем все статусы
    result2 = await supabase._request(
        'GET', '/rest/v1/meetings',
        params={
            'user_id': f'eq.{user_id}',
            'status': 'eq.active',
            'select': 'id,title,transcription_status',
            'limit': '20'
        }
    )
    print(f'\nВсе встречи: {len(result2)}')
    statuses = {}
    for m in result2:
        s = m.get("transcription_status", "none")
        statuses[s] = statuses.get(s, 0) + 1
    print(f'Статусы: {statuses}')

asyncio.run(check())



