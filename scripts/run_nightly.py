#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Скрипт для запуска ночной консолидации.

Использование:
    python scripts/run_nightly.py
    python scripts/run_nightly.py --verbose
    python scripts/run_nightly.py --dry-run

Cron (запуск в 3:00 каждый день):
    0 3 * * * cd /path/to/tessent_brain && python scripts/run_nightly.py >> logs/nightly.log 2>&1
"""
import os
import sys

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import logging
from datetime import datetime


def setup_logging(verbose: bool = False):
    """Настройка логирования."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )


async def _print_dry_run_plan(args, logger):
    """Показать, ЧТО выполнит ночной прогон: каких юзеров и какие шаги
    (с учётом флагов), без внесения изменений."""
    from backend.core.corrections import NightlyConsolidationService

    service = NightlyConsolidationService(users_data_path=args.users_path)

    # 1) Кого обрабатываем
    try:
        users = await service._get_users()
    except Exception as e:
        users = []
        logger.warning(f"Не удалось перечислить юзеров: {e}")

    try:
        from backend.core.store.tenant_paths import graphs_dir
        gdir = str(graphs_dir())
    except Exception:
        gdir = "data/graphs"

    backend = "Neo4j" if getattr(service, "_neo4j_active", False) else "NetworkX (json-файлы)"
    logger.info("")
    logger.info(f"Бэкенд графа: {backend}")
    logger.info(f"Источник юзеров: {gdir}/*.json + DISTINCT user_id/tenant_id из Neo4j")
    logger.info(f"Найдено юзеров: {len(users)}")
    for u in users[:20]:
        logger.info(f"   • {u}")
    if not users:
        logger.warning("   ⚠️  Юзеров не найдено — ночной прогон ничего не сделает.")
        logger.warning("       Если граф в Neo4j, а per-user json-файлов нет —")
        logger.warning(f"       проверьте, что в {gdir} есть <user_id>.json.")

    # 2) Какие шаги и их флаги
    def _gate(source, name):
        try:
            if source == "flag":
                from backend.core.config import flags as _f
                if not hasattr(_f, name):
                    return "?"
                return "ON" if getattr(_f, name) else "off"
            else:
                from backend.config import settings as _s
                return "ON" if getattr(_s, name, False) else "off"
        except Exception:
            return "?"

    steps = [
        ("1.  Применить pending-исправления", _gate("flag", "enable_two_phase_corrections")),
        ("2.  Temporal decay (типо-зависимый, +Neo4j)", _gate("flag", "enable_temporal_decay")),
        ("2.5 Авто-склейка дублей (fuzzy≥0.95 + LLM 0.80–0.94)", "ВСЕГДА"),
        ("3.  Консолидация памяти агентов", _gate("flag", "enable_agent_memory")),
        ("4.  Dream-consolidation памяти", "ВСЕГДА (best-effort)"),
        ("5.  Обновление снапшотов", _gate("flag", "enable_enhanced_snapshots")),
        ("6.  Извлечение предпочтений из чатов", "ВСЕГДА (best-effort)"),
        ("7.  Синтез мудрости (с доказательной базой)", "ВСЕГДА"),
        ("8.  Курирование профиля (W7)", "ВСЕГДА (best-effort)"),
        ("8.5 Rule Book (регламенты → правила)", "ВСЕГДА"),
        ("9.  Доставка ночных инсайтов", "ВСЕГДА (gate по нагрузке)"),
        ("10. BWE crystal maintenance", _gate("setting", "crystal_enrichment_enabled")),
        ("11. Проактивный скан слепых зон/десинков (+Neo4j)", _gate("setting", "proactive_scan_enabled")),
        ("    └─ LLM-валидация десинков (E)", _gate("setting", "conflict_llm_validation_enabled")),
        ("(в конце) Retention cleanup — раз на весь цикл", "ВСЕГДА"),
    ]

    logger.info("")
    logger.info("Шаги на каждого юзера (флаг → выполнится?):")
    for name, state in steps:
        logger.info(f"   [{state:>20}]  {name}")
    logger.info("")
    logger.info("Запустить по-настоящему: убрать --dry-run")


async def main():
    parser = argparse.ArgumentParser(
        description="Tessbrain - Nightly Consolidation"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Подробный вывод"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать что будет сделано"
    )
    parser.add_argument(
        "--users-path",
        type=str,
        default="data/users",
        help="Путь к данным пользователей"
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger("nightly")

    logger.info("="*60)
    logger.info("TESSENT BRAIN - NIGHTLY CONSOLIDATION")
    logger.info(f"Started at: {datetime.now().isoformat()}")
    logger.info("="*60)

    if args.dry_run:
        logger.info("DRY RUN MODE - показываю план, изменений НЕ будет")
        try:
            await _print_dry_run_plan(args, logger)
        except Exception as e:
            logger.error(f"Не удалось построить план: {e}")
        return

    try:
        from backend.core.corrections import NightlyConsolidationService

        service = NightlyConsolidationService(
            users_data_path=args.users_path
        )

        stats = await service.run()

        # Выводим результаты
        logger.info("")
        logger.info("="*60)
        logger.info("RESULTS:")
        logger.info(f"  Users processed: {stats['users_processed']}")
        logger.info(f"  Corrections applied: {stats['corrections_applied']}")
        logger.info(f"  Edges decayed: {stats['edges_decayed']}")
        logger.info(f"  Memories consolidated: {stats['memories_consolidated']}")
        logger.info(f"  Snapshots updated: {stats['snapshots_updated']}")
        logger.info(f"  Errors: {len(stats['errors'])}")
        logger.info("="*60)

        if stats['errors']:
            logger.warning("Errors occurred:")
            for err in stats['errors']:
                logger.warning(f"  {err}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())


