# -*- coding: utf-8 -*-
"""Миграция tenant-владения: проставить tenant_id на существующих Neo4j-узлах.

ЗАЧЕМ
=====
Изоляция данных в системе работает через RLS-фильтр по `tenant_id`. Но
solo-юзеры (без организации) исторически ингестились с `tenant_id = NULL`,
и все они делили общий NULL-бакет — видели снэпшоты/граф/чат друг друга.

Этот скрипт привязывает существующие узлы к их владельцу:
  1. Meeting-узлы получают tenant_id = owner (через meetings.user_id из
     Supabase). Для solo-юзера tenant_id == его user_id.
  2. 1-hop соседи каждого Meeting (Person/Decision/Task/KPI/Entity/Chunk/…),
     у которых СЕЙЧАС tenant_id IS NULL и которые связаны ровно с ОДНИМ
     владельцем, получают tenant_id этого владельца.
  3. Узлы, связанные с несколькими владельцами (реально общие сущности),
     остаются NULL — их трогать опасно (это были бы cross-tenant связи).

После миграции read-фильтр `tenant_id = $scope` отдаёт юзеру только его
данные (плюс остаточные NULL — см. отчёт скрипта, если их много —
запустите повторно или разбирайте вручную).

БЕЗОПАСНОСТЬ
============
  * По умолчанию DRY-RUN: только считает и печатает, НЕ пишет.
  * Запись только с флагом --apply.
  * Не трогает узлы, у которых tenant_id уже выставлен.
  * Перед --apply сделайте дамп Neo4j (neo4j-admin dump) на всякий случай.

ЗАПУСК
======
  python scripts/migrate_tenant_ownership.py            # dry-run, отчёт
  python scripts/migrate_tenant_ownership.py --apply    # реально пишет

ENV: NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD + SUPABASE_* (как у backend).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Делаем backend импортируемым при запуске из scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Подхватываем .env как это делает backend.api.app — иначе standalone
# скрипт не видит SUPABASE_URL/SUPABASE_KEY/NEO4J_* и падает на первом
# же _request с 'Database connection not configured'.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate_tenant")


async def _load_meeting_owners(supabase) -> dict[str, str]:
    """Вернуть {meeting_key -> user_id} для всех встреч (id и meeting_id)."""
    rows = await supabase._request(
        "GET", "/rest/v1/meetings",
        params={"select": "id,meeting_id,user_id"},
    )
    owners: dict[str, str] = {}
    for r in rows or []:
        uid = r.get("user_id")
        if not uid:
            continue
        for key in (r.get("id"), r.get("meeting_id")):
            if key:
                owners[str(key)] = str(uid)
    return owners


async def run_claim_all(apply: bool, user_id: str) -> int:
    """Режим --claim-all-for: пометить ВСЕ узлы с tenant_id IS NULL заданным
    user_id. Для single-tenant-деплоя, где Meeting-маппинг слишком разрежен
    (большинство Person/Decision/Chunk не привязаны к Meeting'у напрямую).

    После этого read-фильтр tenant_id=<user_id> отдаёт юзеру весь его
    (бывший NULL) граф, а будущие другие аккаунты (со СВОИМ tenant_id) его
    не увидят. ВНИМАНИЕ: если в NULL-бакете СЕЙЧАС есть данные другого
    аккаунта — они станут принадлежать <user_id>. Применяйте только если
    это фактически один реальный аккаунт.
    """
    from backend.core.store.graph_builder import GraphBuilder

    gb = GraphBuilder(use_networkx=False)
    ok = await gb.connect()
    if not ok or not gb.driver:
        logger.error("❌ Не удалось подключиться к Neo4j. Проверьте NEO4J_* env.")
        return 2

    async with gb.driver.session() as session:
        # NULL ИЛИ пустая строка: create_node ставит tenant_id='' когда
        # user_id отсутствовал — такие узлы тоже «ничейное» легаси.
        cnt_res = await session.run(
            "MATCH (n) WHERE n.tenant_id IS NULL OR n.tenant_id = '' "
            "RETURN count(n) AS c")
        total = (await cnt_res.single())["c"]
        if apply:
            await session.run(
                "MATCH (n) WHERE n.tenant_id IS NULL OR n.tenant_id = '' "
                "SET n.tenant_id = $uid",
                {"uid": user_id})
    await gb.close(save=False)

    mode = "ПРИМЕНЕНО" if apply else "DRY-RUN (ничего не записано)"
    logger.info("─" * 50)
    logger.info("CLAIM-ALL [%s]: NULL-узлов под пометку = %d → tenant_id=%s",
                mode, total, user_id)
    if not apply:
        logger.info("Для записи добавьте --apply (сделайте дамп Neo4j заранее).")
    return 0


async def _resolve_user_ref(ref: str) -> str:
    """uid как есть; email → uid через public.users."""
    import re as _re
    if _re.match(r"^[0-9a-fA-F-]{36}$", ref or ""):
        return ref
    from backend.db.supabase_client import get_supabase_client
    supabase = get_supabase_client()
    rows = await supabase._request(
        "GET", "/rest/v1/users",
        params={"select": "id,email", "email": f"eq.{(ref or '').strip()}"})
    if rows:
        return str(rows[0]["id"])
    raise SystemExit(f"❌ Пользователь «{ref}» не найден в public.users — "
                     "передайте uid напрямую")


# Supabase-таблицы, где владение задаётся колонкой user_id.
_MOVE_TABLES = ("meetings", "knowledge_sync_subscriptions",
                "processed_meetings", "automations")


async def run_move(from_ref: str, to_ref: str, apply: bool) -> int:
    """Полный перенос данных одного аккаунта в другой (single-tenant fix:
    «данные КПД оказались не в том аккаунте»).

    Переносит:
      1. Neo4j: все узлы с tenant_id/user_id = FROM → TO (рёбра владения
         не имеют — поедут вместе с узлами).
      2. Файловые per-user сторы data/*: каждый файл/каталог, чьё имя
         начинается с FROM-uid, переименовывается в TO-uid (граф NetworkX,
         снапшоты, векторы, инсайты, tombstones, ресурсы, datasets, …).
      3. Supabase: user_id в таблицах %s.

    После переноса залогиньтесь в TO-аккаунт — синк и автоматизации
    продолжат писать уже в него. DRY-RUN по умолчанию.
    """ % (", ".join(_MOVE_TABLES),)
    from_uid = await _resolve_user_ref(from_ref)
    to_uid = await _resolve_user_ref(to_ref)
    if from_uid == to_uid:
        logger.error("❌ FROM и TO — один и тот же аккаунт (%s)", from_uid)
        return 2
    mode = "ПРИМЕНЕНО" if apply else "DRY-RUN (ничего не записано)"
    logger.info("Перенос %s… → %s… [%s]", from_uid[:8], to_uid[:8], mode)

    # ── 1. Neo4j ──
    try:
        from backend.core.store.graph_builder import GraphBuilder
        gb = GraphBuilder(use_networkx=False)
        ok = await gb.connect()
        if ok and gb.driver:
            async with gb.driver.session() as session:
                res = await session.run(
                    "MATCH (n) WHERE n.tenant_id = $a OR n.user_id = $a "
                    "RETURN count(n) AS c", {"a": from_uid})
                cnt = (await res.single())["c"]
                if apply and cnt:
                    await session.run(
                        "MATCH (n) WHERE n.tenant_id = $a OR n.user_id = $a "
                        "SET n.tenant_id = $b, n.user_id = $b",
                        {"a": from_uid, "b": to_uid})
                logger.info("  Neo4j: узлов к переносу = %d", cnt)
            await gb.close(save=False)
        else:
            logger.info("  Neo4j: недоступен — пропущен (NetworkX-режим?)")
    except Exception as e:
        logger.warning("  Neo4j: перенос не удался: %s", e)

    # ── 2. Файловые сторы: data/*/<from_uid>* → <to_uid>* ──
    import shutil
    data_root = Path(__file__).resolve().parents[1] / "data"
    moved, conflicts = 0, 0
    if data_root.exists():
        for store_dir in sorted(data_root.iterdir()):
            if not store_dir.is_dir():
                continue
            for entry in sorted(store_dir.iterdir()):
                if not entry.name.startswith(from_uid):
                    continue
                target = store_dir / entry.name.replace(from_uid, to_uid, 1)
                if target.exists():
                    if entry.is_dir() and target.is_dir():
                        # каталог уже есть у TO — доливаем неконфликтующее
                        for child in sorted(entry.iterdir()):
                            tchild = target / child.name
                            if tchild.exists():
                                conflicts += 1
                                logger.info("    конфликт (оставлен у TO): %s",
                                            tchild.relative_to(data_root))
                            elif apply:
                                shutil.move(str(child), str(tchild))
                                moved += 1
                            else:
                                moved += 1
                    else:
                        conflicts += 1
                        logger.info("    конфликт (не перезаписан): %s",
                                    target.relative_to(data_root))
                    continue
                if apply:
                    shutil.move(str(entry), str(target))
                moved += 1
                logger.info("  файл/каталог: %s → %s",
                            entry.relative_to(data_root),
                            target.relative_to(data_root))
    logger.info("  Файловых сторов к переносу = %d (конфликтов: %d)",
                moved, conflicts)

    # ── 3. Supabase ──
    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()
        for table in _MOVE_TABLES:
            try:
                rows = await supabase._request(
                    "GET", f"/rest/v1/{table}",
                    params={"select": "id", "user_id": f"eq.{from_uid}",
                            "limit": "10000"})
                n = len(rows or [])
                if apply and n:
                    await supabase._request(
                        "PATCH", f"/rest/v1/{table}",
                        params={"user_id": f"eq.{from_uid}"},
                        json_data={"user_id": to_uid})
                logger.info("  Supabase %s: строк к переносу = %d", table, n)
            except Exception as e:
                logger.warning("  Supabase %s: пропущено (%s)", table, e)
    except Exception as e:
        logger.warning("  Supabase: недоступен: %s", e)

    logger.info("─" * 50)
    logger.info("Итог [%s]: Neo4j+файлы+Supabase см. выше.", mode)
    if not apply:
        logger.info("Для реальной записи добавьте --apply "
                    "(сделайте бэкап data/ и дамп Neo4j заранее).")
    else:
        logger.info("Готово. Перезапустите backend и залогиньтесь в "
                    "TO-аккаунт — данные там.")
    return 0


async def run(apply: bool) -> int:
    from backend.core.store.graph_builder import GraphBuilder
    from backend.db.supabase_client import get_supabase_client

    gb = GraphBuilder(use_networkx=False)
    ok = await gb.connect()
    if not ok or not gb.driver:
        logger.error("❌ Не удалось подключиться к Neo4j. Проверьте NEO4J_* env.")
        return 2

    supabase = get_supabase_client()
    owners = await _load_meeting_owners(supabase)
    logger.info("📂 Загружено владельцев встреч (ключей): %d", len(owners))
    if not owners:
        logger.error("❌ Нет meetings в Supabase — нечего привязывать.")
        return 2

    set_meetings = 0
    set_neighbors = 0
    skipped_multi = 0

    async with gb.driver.session() as session:
        # 1) Meeting-узлы → tenant_id владельца (только где сейчас NULL).
        res = await session.run(
            "MATCH (m:Meeting) WHERE m.tenant_id IS NULL "
            "RETURN m.meeting_id AS meeting_id, m.id AS id, id(m) AS nid"
        )
        meeting_rows = await res.data()
        # nid → owner
        meeting_owner: dict[int, str] = {}
        for row in meeting_rows:
            key = row.get("meeting_id") or row.get("id")
            owner = owners.get(str(key)) if key else None
            if owner:
                meeting_owner[row["nid"]] = owner

        logger.info("📌 Meeting-узлов с владельцем под миграцию: %d (из %d NULL)",
                    len(meeting_owner), len(meeting_rows))

        if apply:
            for nid, owner in meeting_owner.items():
                await session.run(
                    "MATCH (m) WHERE id(m) = $nid SET m.tenant_id = $owner",
                    {"nid": nid, "owner": owner},
                )
                set_meetings += 1
        else:
            set_meetings = len(meeting_owner)

        # 2) 1-hop соседи: считаем владельцев через связанные Meeting'и.
        #    Берём NULL-узлы (не Meeting), у которых ровно один owner среди
        #    соседних мигрированных Meeting'ов.
        res2 = await session.run(
            """
            MATCH (n)-[]-(m:Meeting)
            WHERE n.tenant_id IS NULL AND NOT n:Meeting AND m.tenant_id IS NOT NULL
            RETURN id(n) AS nid, collect(DISTINCT m.tenant_id) AS owners
            """
        )
        neigh_rows = await res2.data()
        for row in neigh_rows:
            owner_list = [o for o in (row.get("owners") or []) if o]
            if len(set(owner_list)) == 1:
                if apply:
                    await session.run(
                        "MATCH (n) WHERE id(n) = $nid SET n.tenant_id = $owner",
                        {"nid": row["nid"], "owner": owner_list[0]},
                    )
                set_neighbors += 1
            elif len(set(owner_list)) > 1:
                skipped_multi += 1

    await gb.close(save=False)

    mode = "ПРИМЕНЕНО" if apply else "DRY-RUN (ничего не записано)"
    logger.info("─" * 50)
    logger.info("Итог [%s]:", mode)
    logger.info("  Meeting-узлов размечено:      %d", set_meetings)
    logger.info("  Соседних узлов размечено:     %d", set_neighbors)
    logger.info("  Пропущено (мультивладелец):   %d", skipped_multi)
    if not apply:
        logger.info("\nЭто dry-run. Для реальной записи: --apply "
                    "(сделайте дамп Neo4j заранее).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Миграция tenant_id на Neo4j-узлах")
    ap.add_argument("--apply", action="store_true",
                    help="Реально записать (по умолчанию dry-run)")
    ap.add_argument("--claim-all-for", metavar="USER_ID", default=None,
                    help="Пометить ВСЕ NULL-узлы этим user_id (single-tenant). "
                         "Иначе — миграция по Meeting-владельцу из Supabase.")
    ap.add_argument("--move-from", metavar="UID_OR_EMAIL", default=None,
                    help="Полный перенос данных аккаунта: источник")
    ap.add_argument("--move-to", metavar="UID_OR_EMAIL", default=None,
                    help="Полный перенос данных аккаунта: получатель")
    args = ap.parse_args()
    if bool(args.move_from) != bool(args.move_to):
        ap.error("--move-from и --move-to задаются вместе")
    if args.move_from:
        return asyncio.run(run_move(args.move_from, args.move_to,
                                    apply=args.apply))
    if args.claim_all_for:
        return asyncio.run(run_claim_all(apply=args.apply,
                                         user_id=args.claim_all_for))
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
