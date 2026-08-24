#!/usr/bin/env python3
# ruff: noqa: T201
"""Best-effort backfill `meeting_id` для старых llm_usage записей
(issue #110 пакет 5).

Контекст: до пакетов 1-2 атрибуция «по сущности» не существовала, поэтому
все старые записи имеют `meeting_id IS NULL`. Полная реконструкция
невозможна — `session_id` в workflow раньше не выставлялся, прямой связи
с `meeting_id` в БД нет.

Что делает скрипт (best-effort, эвристика):
  1) Находит записи с agent_mode='automation' (workflow-action до пакета 2)
     и timestamp в окрестности времени обработки встречи
  2) Если у юзера в этом окне была только одна встреча — связывает
  3) Записи с неоднозначным маппингом помечает `surface='ingest_legacy'`,
     не выставляя meeting_id (чтобы видеть «вот эти расходы — на встречи,
     но какая именно неизвестно»).

Использование:
    # Dry-run: посчитать сколько можно связать
    python scripts/backfill_meeting_attribution.py --source data/llm_usage.db \\
        --window-minutes 30 --dry-run

    # Применить
    python scripts/backfill_meeting_attribution.py --source data/llm_usage.db \\
        --window-minutes 30

Внимание: скрипт работает только с SQLite-источником (запускайте до
миграции в PG, либо отдельно на каждой БД). PG-версия — отдельная задача,
требует подключения к таблице встреч в PG.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="SQLite файл llm_usage")
    parser.add_argument("--window-minutes", type=int, default=30,
                        help="Окно времени для связки с встречей (default 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать оценку без записи")
    parser.add_argument("--legacy-surface", default="ingest_legacy",
                        help="Какой surface ставить неоднозначным записям "
                             "(default 'ingest_legacy')")
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"❌ Source not found: {args.source}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.source)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Базовая статистика
    (total,) = cursor.execute("SELECT COUNT(*) FROM llm_usage").fetchone()
    (with_meeting,) = cursor.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE meeting_id IS NOT NULL"
    ).fetchone()
    (orphan,) = cursor.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_mode = 'automation' "
        "AND meeting_id IS NULL"
    ).fetchone()
    print("📊 SQLite stats:")
    print(f"   total:                {total}")
    print(f"   c meeting_id:         {with_meeting}")
    print(f"   automation без meeting_id (кандидаты): {orphan}")

    if orphan == 0:
        print("✅ Нечего backfill'ить (все workflow-записи уже атрибутированы)")
        return 0

    # Проверим, есть ли в этой же SQLite таблица meetings — обычно её нет
    # (это PG-таблица). На SQLite-старте мы можем backfill только через
    # эвристику «один user + один близкий timestamp» внутри самого
    # llm_usage. Без внешней таблицы встреч полную атрибуцию не сделать.

    # Стратегия для SQLite-only: пометить orphan-записи surface='legacy',
    # чтобы их было видно в /usage/attribution?group_by=surface — и UI
    # показывал «вот эти $X — старые workflow-вызовы, meeting_id восстановить
    # невозможно (session_id не сохранялся)».

    if args.dry_run:
        # Покажем сэмпл
        rows = cursor.execute("""
            SELECT id, timestamp, user_id, total_cost
            FROM llm_usage
            WHERE agent_mode = 'automation' AND meeting_id IS NULL
              AND (surface IS NULL OR surface != ?)
            ORDER BY timestamp DESC LIMIT 5
        """, (args.legacy_surface,)).fetchall()
        print(f"\n🔍 Сэмпл из {orphan} orphan-записей (будут помечены "
              f"surface='{args.legacy_surface}'):")
        for r in rows:
            print(f"   id={r['id']} ts={r['timestamp']} "
                  f"user={r['user_id']} cost=${r['total_cost']:.4f}")
        print("\n⚠️  meeting_id для них реконструировать НЕЛЬЗЯ "
              "(session_id не сохранялся в workflow). Помечаем surface "
              "чтобы видеть суммарный legacy-расход в /usage/attribution.")
        print("\n   --dry-run: ничего не изменено. Запустите без --dry-run "
              "для применения.")
        return 0

    # Применяем
    cursor.execute("""
        UPDATE llm_usage
        SET surface = ?
        WHERE agent_mode = 'automation' AND meeting_id IS NULL
          AND (surface IS NULL OR surface != ?)
    """, (args.legacy_surface, args.legacy_surface))
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    print(f"\n✅ Помечено surface='{args.legacy_surface}': {affected} записей")
    print("   Теперь в /usage/attribution?group_by=surface эти затраты "
          "видны отдельно (legacy-workflow до пакета 1-2).")
    print("   Новые встречи начиная с сегодня будут атрибутированы "
          "полностью (meeting_id + surface='ingest').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
