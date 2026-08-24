#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
End-to-end тест ШИНЫ ДАННЫХ (Data Bus).

Что проверяет:
  1. Создание потребителя по шаблону политики → выдачу API-ключа.
  2. Запрос к шине этим ключом (аутентификация + поиск по графу/векторам).
  3. Применение политики и редактирование конфиденциального (raw vs filtered).
  4. (опц.) Сравнение, что РАЗНЫЕ потребители видят РАЗНОЕ.
  5. Очистку (деактивация тест-потребителей).

Запуск (из tessent_brain, в venv):
  py scripts/test_data_bus.py --user-id <ВАШ_USER_ID> --query "статус проектов и KPI"
  py scripts/test_data_bus.py --user-id <ВАШ_USER_ID> --all-policies
  py scripts/test_data_bus.py --user-id <ВАШ_USER_ID> --keep   # не удалять потребителя

USER_ID — это tenant/владелец данных (тот же id, что в снапшотах/графе).
"""
import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("databus-test")
logger.setLevel(logging.INFO)


POLICIES = [
    ("investor_readonly", "investor", "Инвестор (тест)"),
    ("contractor_project", "contractor", "Подрядчик (тест)"),
    ("ai_agent_analysis", "ai_agent", "AI-агент (тест)"),
    ("internal_department", "internal_dept", "Внутр. отдел (тест)"),
    ("partner_public", "partner", "Партнёр (тест)"),
]


def _hr(title: str):
    logger.info("\n" + "=" * 64)
    logger.info(title)
    logger.info("=" * 64)


def _short(obj, n=600):
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        s = str(obj)
    return s if len(s) <= n else s[:n] + " …(обрезано)"


async def _run_one(service, user_id: str, policy_template: str, ctype: str,
                   name: str, query: str, keep: bool):
    _hr(f"ПОТРЕБИТЕЛЬ: {name}  |  политика: {policy_template}")

    # 1) Создаём потребителя
    created = await service.create_consumer(
        tenant_id=user_id,
        name=name,
        consumer_type=ctype,
        contact_email="test@example.com",
        policy_template=policy_template,
        metadata={"description": "Автотест шины данных"},
    )
    if not isinstance(created, dict) or "api_key" not in created:
        logger.info(f"❌ Не удалось создать потребителя: {_short(created)}")
        return False
    api_key = created["api_key"]
    consumer = created.get("consumer", {}) or {}
    consumer_id = consumer.get("id") or consumer.get("consumer_id", "")
    logger.info(f"✅ Создан. API-ключ: {api_key[:14]}…  consumer_id={consumer_id}")

    # 2) Запрос ОТ ИМЕНИ потребителя (полный путь: auth → policy → rate-limit → поиск)
    result = await service.query(
        api_key=api_key,
        query_text=query,
        user_id=user_id,
        response_format="both",
        search_depth="standard",
    )
    if result.get("error"):
        logger.info(f"⚠️ Ответ шины с ошибкой: {result.get('code')} {result.get('error')}")
    else:
        meta = result.get("meta") or {}
        ans = result.get("answer") or result.get("natural_answer") or ""
        data = result.get("data") or result.get("results") or []
        _count = meta.get("count")
        if _count is None:
            _count = len(data) if isinstance(data, list) else "—"
        logger.info(f"📊 Видно узлов: {_count}"
                    f" | отредактировано полей: {meta.get('redacted_fields', '—')}"
                    f" | политика: {meta.get('policy', '—')}")
        if ans:
            logger.info(f"🗣  Ответ: {str(ans)[:400]}")
        if isinstance(data, list) and data:
            logger.info(f"🔢 Структурно (первый узел): {_short(data[0], 400)}")

    # 3) Чистим
    if keep:
        logger.info(f"ℹ️  --keep: потребитель {consumer_id} оставлен.")
    elif consumer_id:
        try:
            await service.deactivate_consumer(consumer_id)
            logger.info(f"🧹 Потребитель {consumer_id} деактивирован.")
        except Exception as e:
            logger.info(f"⚠️ Не удалось деактивировать: {e}")
    return not bool(result.get("error"))


async def main():
    ap = argparse.ArgumentParser(description="E2E тест шины данных Tessbrain")
    ap.add_argument("--user-id", required=True, help="tenant/user_id (владелец данных)")
    ap.add_argument("--query", default="статус проектов, KPI и команда", help="текст запроса")
    ap.add_argument("--policy", default="investor_readonly", help="шаблон политики для одиночного теста")
    ap.add_argument("--all-policies", action="store_true", help="прогнать ВСЕ 5 политик")
    ap.add_argument("--keep", action="store_true", help="не удалять созданных потребителей")
    args = ap.parse_args()

    _hr("TESSENT DATA BUS — E2E ТЕСТ")
    logger.info(f"user_id: {args.user_id}")
    logger.info(f"query:   {args.query}")

    try:
        from backend.core.data_bus.bus_service import get_data_bus_service
    except Exception as e:
        logger.info(f"❌ Не удалось импортировать шину: {e}")
        sys.exit(1)

    service = get_data_bus_service()

    # storage backend (Supabase или in-memory fallback)
    try:
        sb = await service.storage._get_supabase()
        logger.info(f"storage:  {'Supabase' if sb else 'in-memory fallback'}")
    except Exception:
        logger.info("storage:  неизвестно")

    targets = POLICIES if args.all_policies else [
        next((p for p in POLICIES if p[0] == args.policy), POLICIES[0])
    ]

    ok = 0
    for policy_template, ctype, name in targets:
        try:
            if await _run_one(service, args.user_id, policy_template, ctype, name, args.query, args.keep):
                ok += 1
        except Exception as e:
            logger.info(f"❌ Тест {policy_template} упал: {e}")

    _hr("ИТОГ")
    logger.info(f"Успешно: {ok}/{len(targets)} политик отработали без ошибок шины.")
    logger.info("Если 'storage: in-memory fallback' — потребители НЕ сохранятся между запусками")
    logger.info("(это нормально для теста; для прод-хранения нужны таблицы data_bus в Supabase).")


if __name__ == "__main__":
    asyncio.run(main())
