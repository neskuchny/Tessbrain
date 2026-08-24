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

    # Получаем одну встречу с полными данными
    meeting_id = "3d4ee8c2-8403-43c6-b9f7-bec40319dfef"  # Н.Йонова_13.10.2025

    # Получаем полные данные встречи
    result = await supabase._request(
        'GET', '/rest/v1/meetings',
        params={
            'id': f'eq.{meeting_id}',
            'select': '*'
        }
    )

    if result:
        meeting = result[0]
        print("Поля встречи:")
        for key, value in meeting.items():
            if isinstance(value, str) and len(value) > 100:
                print(f"  {key}: [{len(value)} символов] {value[:100]}...")
            else:
                print(f"  {key}: {value}")
    else:
        print("Встреча не найдена")

asyncio.run(check())



