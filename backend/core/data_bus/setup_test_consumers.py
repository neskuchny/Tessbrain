# -*- coding: utf-8 -*-
"""
Скрипт создания тестовых потребителей шины данных.

Запуск:
  cd tessent_brain
  python -m backend.core.data_bus.setup_test_consumers

Или импорт:
  from backend.core.data_bus.setup_test_consumers import create_test_consumers
  await create_test_consumers(tenant_id="your-user-id")

Создаёт 5 тестовых потребителей с разными политиками доступа:
1. НордИнвест (тест)        — investor      → investor_readonly
2. Дизайн-студия Арт (тест) — contractor    → contractor_project
3. ConsultBot AI (тест)      — ai_agent      → ai_agent_analysis
4. Отдел маркетинга (тест)   — internal_dept → internal_department
5. Партнёр-интегратор (тест) — partner       → partner_public
"""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Описания тестовых потребителей
TEST_CONSUMERS = [
    {
        "name": "НордИнвест (тест)",
        "consumer_type": "investor",
        "contact_email": "test-investor@example.com",
        "policy_template": "investor_readonly",
        "allowed_project_ids": None,  # Все проекты (public view)
        "metadata": {"description": "Тестовый инвестор. Видит статус проектов, вехи, публичные KPI."},
    },
    {
        "name": "Дизайн-студия Арт (тест)",
        "consumer_type": "contractor",
        "contact_email": "test-contractor@example.com",
        "policy_template": "contractor_project",
        "allowed_project_ids": ["project_alpha"],  # Только один проект
        "metadata": {"description": "Тестовый подрядчик. Видит задачи своего проекта."},
    },
    {
        "name": "ConsultBot AI (тест)",
        "consumer_type": "ai_agent",
        "contact_email": "test-ai@example.com",
        "policy_template": "ai_agent_analysis",
        "allowed_project_ids": None,
        "metadata": {"description": "Тестовый AI-агент. Анализирует структуру и KPI."},
    },
    {
        "name": "Отдел маркетинга (тест)",
        "consumer_type": "internal_dept",
        "contact_email": "test-marketing@example.com",
        "policy_template": "internal_department",
        "allowed_project_ids": None,
        "metadata": {"description": "Тестовый внутренний отдел. Видит всё до access_level=3."},
    },
    {
        "name": "Партнёр-интегратор (тест)",
        "consumer_type": "partner",
        "contact_email": "test-partner@example.com",
        "policy_template": "partner_public",
        "allowed_project_ids": None,
        "metadata": {"description": "Тестовый партнёр. Видит только public данные."},
    },
]


async def create_test_consumers(tenant_id: str) -> list:
    """
    Создать тестовые аккаунты шины данных.

    Args:
        tenant_id: ID пользователя/организации (владелец данных)

    Returns:
        Список словарей с данными созданных потребителей + API ключи
    """
    from backend.core.data_bus.bus_service import get_data_bus_service

    service = get_data_bus_service()
    results = []

    print()
    print("=" * 70)
    print("   ТЕСТОВЫЕ ДОСТУПЫ К ШИНЕ ДАННЫХ TESSENT")
    print("=" * 70)
    print(f"   Tenant ID: {tenant_id}")
    print("=" * 70)

    for consumer_config in TEST_CONSUMERS:
        try:
            result = await service.create_consumer(
                tenant_id=tenant_id,
                name=consumer_config["name"],
                consumer_type=consumer_config["consumer_type"],
                contact_email=consumer_config["contact_email"],
                policy_template=consumer_config["policy_template"],
                allowed_project_ids=consumer_config.get("allowed_project_ids"),
                metadata=consumer_config.get("metadata"),
            )

            results.append(result)

            consumer = result["consumer"]
            api_key = result["api_key"]
            policy = result["policy"]

            print()
            print(f"  {'─' * 60}")
            print(f"  Потребитель:  {consumer['name']}")
            print(f"  Тип:          {consumer['consumer_type']}")
            print(f"  ID:           {consumer['id']}")
            print(f"  Политика:     {policy['name']}")
            print(f"  API Key:      {api_key}")
            print(f"  Prefix:       {consumer['api_key_prefix']}")
            print(f"  Access Level: до {policy['max_access_level']}")
            print(f"  Лимиты:      {policy['max_queries_per_hour']}q/час, {policy['max_queries_per_day']}q/день")
            print(f"  Видит:        {', '.join(policy['allowed_node_types'][:5])}{'...' if len(policy['allowed_node_types']) > 5 else ''}")
            if policy['blocked_node_types']:
                print(f"  НЕ видит:     {', '.join(policy['blocked_node_types'][:5])}")
            print()
            print("  Пример запроса:")
            print(f"    curl -H 'X-API-Key: {api_key}' \\")
            print(f"         'http://localhost:8000/api/v1/data-bus/query?query=статус+проекта&user_id={tenant_id}'")

        except Exception as e:
            logger.error(f"  ERROR creating {consumer_config['name']}: {e}")

    print()
    print("=" * 70)
    print("  ⚠️  СОХРАНИТЕ API КЛЮЧИ! Они не будут показаны повторно.")
    print("=" * 70)
    print()
    print("  Управление шиной данных:")
    print(f"    GET  /api/v1/data-bus/admin/consumers?user_id={tenant_id}")
    print(f"    GET  /api/v1/data-bus/admin/audit?user_id={tenant_id}")
    print("    GET  /api/v1/data-bus/admin/policies")
    print("    GET  /api/v1/data-bus/health")
    print()

    return results


async def main():
    """Точка входа для запуска из командной строки."""
    # Tenant ID из аргумента или default
    if len(sys.argv) > 1:
        tenant_id = sys.argv[1]
    else:
        tenant_id = "test-tenant-001"
        print(f"  Используется тестовый tenant_id: {tenant_id}")
        print("  Для указания своего: python -m backend.core.data_bus.setup_test_consumers <your_user_id>")

    results = await create_test_consumers(tenant_id)

    print(f"\n  Создано {len(results)} тестовых потребителей шины данных.\n")


if __name__ == "__main__":
    asyncio.run(main())
