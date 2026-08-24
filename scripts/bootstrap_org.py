# -*- coding: utf-8 -*-
"""Bootstrap первой организации + founder (A.1).

Проблема курицы-и-яйца: `POST /orgs` требует system-admin JWT, но
до создания первой org у вас нет ни org'а, ни admin'а. Этот скрипт
создаёт seed-org напрямую через storage-слой (org_store + membership),
минуя REST-auth.

Использование:
    python scripts/bootstrap_org.py \\
        --org-id 11111111-1111-1111-1111-111111111111 \\
        --name "Acme Inc" \\
        --founder-user-id <supabase-user-uuid>

    # Dry-run:
    python scripts/bootstrap_org.py --org-id ... --name ... \\
        --founder-user-id ... --dry-run

    # Добавить ещё членов в существующую org:
    python scripts/bootstrap_org.py --add-member \\
        --org-id <existing> --member-user-id <uuid> --role employee

После bootstrap:
  - org появится в data/orgs_metadata.json
  - founder появится в data/user_org_mapping.json с role=founder
  - founder теперь может приглашать через POST /orgs/{id}/invites
  - его meetings начнут promote'иться в org-graph (Wave 1)

ВАЖНО: founder-user-id должен быть РЕАЛЬНЫМ Supabase user_id (claim
`sub` из JWT). Узнать можно через scripts/get_user_id.py или из
Supabase Auth dashboard.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


def _bootstrap_new_org(args) -> int:
    from backend.core.ingest import membership, org_store

    org_id = args.org_id
    name = args.name
    founder = args.founder_user_id

    if not _is_uuid(org_id):
        print(f"❌ --org-id должен быть UUID, получено: {org_id}")
        return 2
    if not _is_uuid(founder):
        print(f"❌ --founder-user-id должен быть UUID (Supabase sub), получено: {founder}")
        return 2
    if not name:
        print("❌ --name обязателен")
        return 2

    # Проверка: org уже есть?
    existing = org_store.get_org(org_id)
    if existing:
        print(f"⚠️  Org {org_id} уже существует: {existing.get('name')}")
        print("   Используйте --add-member чтобы добавить участников.")
        return 1

    # Проверка: founder уже в другой org?
    existing_org = membership.get_org_for_user(founder)
    if existing_org and existing_org != org_id:
        print(f"⚠️  Founder {founder} уже в org {existing_org}")
        print("   Один user = одна org. Сначала удалите из текущей.")
        return 1

    print("📋 Bootstrap plan:")
    print(f"    org_id  : {org_id}")
    print(f"    name    : {name}")
    print(f"    founder : {founder} (role=founder)")
    print()

    if args.dry_run:
        print("🟡 DRY RUN — ничего не записано")
        return 0

    # 1. Создать org metadata.
    try:
        org_meta = org_store.create_org(org_id, name=name, created_by=founder)
        print(f"✅ Org created: {org_meta['name']}")
    except ValueError as e:
        print(f"❌ create_org failed: {e}")
        return 1

    # 2. Добавить founder в membership.
    try:
        member = membership.add_member(founder, org_id, role=membership.ROLE_FOUNDER)
        print(f"✅ Founder added: role={member['role']}, joined_at={member['joined_at']}")
    except ValueError as e:
        print(f"❌ add_member failed: {e}")
        print("   (org создана, но founder не добавлен — добавьте вручную через --add-member)")
        return 1

    print("\n🎉 Bootstrap complete!")
    print(f"   Founder {founder} теперь может:")
    print(f"   - приглашать: POST /api/v1/orgs/{org_id}/invites")
    print(f"   - его meetings promote'ятся в org-graph автоматически")
    print(f"   - команда увидит данные через federated read (Wave 2)")
    return 0


def _add_member(args) -> int:
    from backend.core.ingest import membership, org_store

    org_id = args.org_id
    member = args.member_user_id
    role = args.role

    if not _is_uuid(org_id):
        print(f"❌ --org-id должен быть UUID: {org_id}")
        return 2
    if not _is_uuid(member):
        print(f"❌ --member-user-id должен быть UUID: {member}")
        return 2
    if role not in membership.ALL_ROLES:
        print(f"❌ --role должен быть один из: {sorted(membership.ALL_ROLES)}")
        return 2

    # Org существует?
    if not org_store.get_org(org_id):
        print(f"❌ Org {org_id} не существует. Сначала bootstrap'ните её.")
        return 1

    existing_org = membership.get_org_for_user(member)
    if existing_org and existing_org != org_id:
        print(f"⚠️  User {member} уже в org {existing_org} (cross-org block)")
        return 1

    print(f"📋 Add member: {member} → org {org_id} as {role}")
    if args.dry_run:
        print("🟡 DRY RUN — ничего не записано")
        return 0

    try:
        m = membership.add_member(member, org_id, role=role)
        print(f"✅ Member added: {m['user_id']} (role={m['role']})")
        return 0
    except ValueError as e:
        print(f"❌ add_member failed: {e}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap seed org + founder")
    parser.add_argument("--add-member", action="store_true",
                        help="режим добавления члена в существующую org")
    parser.add_argument("--org-id", type=str, required=True)
    parser.add_argument("--name", type=str, default=None,
                        help="имя org (для создания новой)")
    parser.add_argument("--founder-user-id", type=str, default=None,
                        help="Supabase user_id founder'а (для создания новой)")
    parser.add_argument("--member-user-id", type=str, default=None,
                        help="user_id члена (для --add-member)")
    parser.add_argument("--role", type=str, default="employee",
                        help="роль члена (для --add-member): founder|admin|manager|employee|contractor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.add_member:
        return _add_member(args)
    return _bootstrap_new_org(args)


if __name__ == "__main__":
    sys.exit(main())
