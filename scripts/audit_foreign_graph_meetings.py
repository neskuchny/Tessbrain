# -*- coding: utf-8 -*-
"""Аудит и чистка ЧУЖИХ/ОСИРОТЕВШИХ встреч в графе пользователя.

Зачем: на общем Neo4j в граф аккаунта прошлыми импортами могли попасть Meeting-
узлы, которых НЕТ в таблице meetings этого пользователя (чужие/устаревшие). Они
всплывали в истории сотрудника («встреча из другого аккаунта»).

Что делает:
  1) берёт Meeting-узлы, ЗАШТАМПОВАННЫЕ этим тенантом (tenant_id=user);
  2) сверяет их id/meeting_id с таблицей meetings этого пользователя в Supabase;
  3) те, что заштампованы как «мои», но в таблице ОТСУТСТВУЮТ → ОСИРОТЕВШИЕ
     (устаревшие/ошибочно помеченные) — их безопасно удалить;
  4) по умолчанию только ПЕЧАТАЕТ отчёт; удаляет ТОЛЬКО с флагом --apply.

Осторожно: узлы ДРУГИХ тенантов скрипт НЕ трогает (они принадлежат другим
аккаунтам — на общем графе их удаление затронуло бы других). Убираем лишь то,
что помечено как ваше, но у вас реально не существует.

Запуск (из tessent_brain/):
    python -m scripts.audit_foreign_graph_meetings <user_id>            # только отчёт
    python -m scripts.audit_foreign_graph_meetings <user_id> --apply    # + удалить осиротевшие
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Standalone-скрипт сам грузит .env (API это делает на старте, а мы — нет):
# без него Supabase/граф не сконфигурированы («Database connection not configured»).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    load_dotenv()  # плюс .env из текущей папки, если запуск из tessent_brain/
except Exception:
    pass


def _strip_prefix(node_id: str) -> str:
    s = str(node_id or "").strip()
    return s[len("meeting_"):] if s.startswith("meeting_") else s


def _meeting_key(node: dict) -> str:
    """Ключ сверки с таблицей meetings. Узлы создаёт knowledge_sync как
    meeting_{meeting_id} И кладёт свойство meeting_id — берём СВОЙСТВО (надёжнее,
    переживёт смену схемы id), срез префикса — только фолбэк. Важно для --apply:
    ошибка ключа здесь означала бы удаление живого узла."""
    prop = str((node or {}).get("meeting_id") or "").strip()
    return prop or _strip_prefix((node or {}).get("id") or "")


async def _owned_ids(user_id: str, cands: set) -> set:
    """id/meeting_id встреч, реально принадлежащих пользователю (Supabase)."""
    import re
    if not cands:
        return set()
    uuid_re = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                         r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    uuids = [c for c in cands if uuid_re.fullmatch(c)]
    from backend.db.supabase_client import get_supabase_client
    clauses = []
    if uuids:
        clauses.append(f"id.in.({','.join(uuids)})")
    clauses.append(f"meeting_id.in.({','.join(cands)})")
    rows = await get_supabase_client()._request(
        "GET", "/rest/v1/meetings",
        params={"user_id": f"eq.{user_id}", "or": f"({','.join(clauses)})",
                "select": "id,meeting_id", "limit": "5000"})
    owned = set()
    for r in (rows or []):
        for k in ("id", "meeting_id"):
            v = str(r.get(k) or "").strip()
            if v:
                owned.add(v)
    return owned


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m scripts.audit_foreign_graph_meetings <user_id> [--apply]")
        return 2
    user_id = sys.argv[1].strip()
    apply = "--apply" in sys.argv[2:]

    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user

    gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
    await gb.connect()
    if not gb.connected:
        print("❌ граф недоступен (проверьте Neo4j/окружение)")
        return 1
    try:
        # Только узлы, ЗАШТАМПОВАННЫЕ этим тенантом (strict — без legacy-NULL,
        # чтобы не трогать неоднозначные без штампа).
        meetings = await gb.get_all_nodes_async(label="Meeting", limit=10000,
                                                tenant_id=user_id, strict_tenant=True)
        print(f"📊 Meeting-узлов, помеченных вашим тенантом: {len(meetings)}")
        by_key = {}
        for m in meetings:
            key = _meeting_key(m)
            if key:
                by_key.setdefault(key, []).append(m)

        owned = await _owned_ids(user_id, set(by_key.keys()))
        orphans = [(k, ms) for k, ms in by_key.items() if k not in owned]

        print(f"✅ Существуют в вашей таблице meetings: {len(by_key) - len(orphans)}")
        print(f"🗑  ОСИРОТЕВШИЕ (помечены как ваши, но в таблице НЕТ): {len(orphans)}")
        for k, ms in orphans[:200]:
            for m in ms:
                title = m.get("title") or m.get("name") or "—"
                date = m.get("date") or m.get("meeting_date") or ""
                print(f"   • {m.get('id')}  «{title}»  {str(date)[:10]}")

        if not orphans:
            print("👍 осиротевших встреч нет — чистить нечего")
            return 0
        if not apply:
            print("\nℹ️  Это только отчёт. Чтобы удалить осиротевшие — перезапустите с --apply")
            return 0

        removed = 0
        for _k, ms in orphans:
            for m in ms:
                nid = m.get("id")
                if nid and await gb.remove_node(nid):
                    removed += 1
        # Neo4j пишет сразу; networkx-бэкенд требует save.
        try:
            await gb.close(save=True)
        except Exception:
            pass
        print(f"\n🗑  Удалено осиротевших встреч: {removed}")
        return 0
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
