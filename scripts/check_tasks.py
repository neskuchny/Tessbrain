# -*- coding: utf-8 -*-
import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

async def check():
    url = os.getenv('SUPABASE_URL') + '/rest/v1/scheduled_tasks?select=*&order=created_at.desc&limit=10'
    headers = {
        'apikey': os.getenv('SUPABASE_KEY'),
        'Authorization': f"Bearer {os.getenv('SUPABASE_KEY')}"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        data = r.json()
        print("scheduled_tasks:")
        print(f"  Response: {data}")
        if isinstance(data, list):
            for task in data:
                print(f"  - {task.get('id')}: {task.get('action_type')} | {task.get('status')} | {task.get('execute_at')}")

    # Также проверим scheduled_automations
    url2 = os.getenv('SUPABASE_URL') + '/rest/v1/scheduled_automations?select=*&order=created_at.desc&limit=10'
    async with httpx.AsyncClient() as client:
        r2 = await client.get(url2, headers=headers)
        print("\nscheduled_automations:")
        for task in r2.json():
            print(f"  - {task.get('id')}: {task.get('name')} | {task.get('status')} | {task.get('execute_at')}")

asyncio.run(check())

