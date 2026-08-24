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
    test_project_id = '9e97cef4-e8de-4640-a803-b3c2602d90e9'

    # Получаем одну встречу со всеми полями
    meetings = await supabase._request(
        'GET', '/rest/v1/meetings',
        params={
            'user_id': f'eq.{user_id}',
            'project_id': f'eq.{test_project_id}',
            'status': 'eq.active',
            'transcription_status': 'eq.completed',
            'select': '*',
            'limit': '1'
        }
    )

    if meetings:
        m = meetings[0]
        print("Поля встречи:")
        for key, value in m.items():
            if value:
                val_str = str(value)[:100] if len(str(value)) > 100 else str(value)
                print(f"  {key}: {val_str}")

        # Проверяем есть ли transcription
        if m.get('transcription'):
            print(f"\n✅ Есть transcription: {len(m.get('transcription'))} символов")
        elif m.get('transcript'):
            print(f"\n✅ Есть transcript: {len(m.get('transcript'))} символов")
        else:
            print("\n❌ Нет transcription/transcript в данных")

        # Проверяем таблицу transcriptions
        print("\n\nПроверяю таблицу transcriptions...")
        try:
            transcriptions = await supabase._request(
                'GET', '/rest/v1/transcriptions',
                params={
                    'meeting_id': f"eq.{m.get('id')}",
                    'select': '*',
                    'limit': '1'
                }
            )
            if transcriptions:
                t = transcriptions[0]
                print("Поля transcription:")
                for key, value in t.items():
                    if value:
                        val_str = str(value)[:100] if len(str(value)) > 100 else str(value)
                        print(f"  {key}: {val_str}")
            else:
                print("  Нет записей в transcriptions для этой встречи")
        except Exception as e:
            print(f"  Ошибка: {e}")

asyncio.run(check())

