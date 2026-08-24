# -*- coding: utf-8 -*-
"""Post-deploy smoke test для Tessbrain (A.3).

Прогоняет минимальный happy-path против running backend, чтобы убедиться
что deploy не сломал критичные flow. Запускать ПОСЛЕ каждого деплоя.

Использование:
    # Базовый smoke (health + auth-free endpoints):
    python scripts/post_deploy_smoke.py --base-url http://localhost:8000

    # Полный smoke с org-flow (требует system-admin JWT):
    python scripts/post_deploy_smoke.py \\
        --base-url https://api.tessent.example \\
        --admin-token "eyJ..." \\
        --full

Exit codes:
    0 — все проверки прошли
    1 — одна или больше проверок упали
    2 — backend недоступен / неверная конфигурация

Что проверяет (--full):
    1. GET  /ping, /livez, /readyz, /healthz       — backend жив
    2. GET  /info                                   — версия/конфиг
    3. POST /api/v1/orgs  (admin)                   — создание org
    4. POST /api/v1/orgs/{id}/invites (admin)       — создание invite
    5. GET  /api/v1/invites/{token}                 — landing-info
    6. GET  /api/v1/orgs/{id}/members (admin)       — список членов
    7. DELETE /api/v1/orgs/{id}/invites/{iid}       — revoke (cleanup)

Без --full делает только шаги 1-2 (health checks, не требуют auth).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any, Optional

try:
    import httpx
except ImportError:
    print("❌ httpx not installed: pip install httpx")
    sys.exit(2)


class SmokeResult:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str):
        self.passed.append(name)
        print(f"  ✅ {name}")

    def fail(self, name: str, reason: str):
        self.failed.append((name, reason))
        print(f"  ❌ {name}: {reason}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'='*50}")
        print(f"Smoke test: {len(self.passed)}/{total} passed")
        if self.failed:
            print(f"\n❌ FAILURES ({len(self.failed)}):")
            for name, reason in self.failed:
                print(f"    - {name}: {reason}")
            return 1
        print("✅ ALL CHECKS PASSED")
        return 0


async def _check_health(client: httpx.AsyncClient, base: str, r: SmokeResult) -> None:
    """Шаги 1-2: health endpoints (без auth)."""
    for path in ["/ping", "/livez", "/readyz", "/healthz"]:
        try:
            resp = await client.get(f"{base}{path}", timeout=10.0)
            if resp.status_code in (200, 204):
                r.ok(f"GET {path} → {resp.status_code}")
            else:
                r.fail(f"GET {path}", f"status {resp.status_code}")
        except Exception as e:
            r.fail(f"GET {path}", str(e)[:120])

    try:
        resp = await client.get(f"{base}/info", timeout=10.0)
        if resp.status_code == 200:
            info = resp.json()
            ver = info.get("version") or info.get("app_version") or "?"
            r.ok(f"GET /info (version={ver})")
        else:
            r.fail("GET /info", f"status {resp.status_code}")
    except Exception as e:
        r.fail("GET /info", str(e)[:120])


async def _check_org_flow(
    client: httpx.AsyncClient, base: str, admin_token: str, r: SmokeResult,
) -> None:
    """Шаги 3-7: org + invite flow (требует admin JWT)."""
    api = f"{base}/api/v1"
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Уникальный org_id для smoke — потом не чистим org (нет DELETE /orgs),
    # но invite revoke'аем. Org останется как smoke-артефакт (это ок,
    # помечен именем).
    org_id = str(uuid.uuid4())
    org_name = f"smoke-test-{org_id[:8]}"

    # 3. Create org
    try:
        resp = await client.post(
            f"{api}/orgs", headers=headers,
            json={"org_id": org_id, "name": org_name},
            timeout=15.0,
        )
        if resp.status_code in (200, 201):
            r.ok(f"POST /orgs → {resp.status_code}")
        else:
            r.fail("POST /orgs", f"status {resp.status_code}: {resp.text[:160]}")
            return  # без org дальше нет смысла
    except Exception as e:
        r.fail("POST /orgs", str(e)[:120])
        return

    # 4. Create invite
    invite_id: Optional[str] = None
    invite_token: Optional[str] = None
    try:
        resp = await client.post(
            f"{api}/orgs/{org_id}/invites", headers=headers,
            json={"role": "employee"}, timeout=15.0,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            invite_id = data.get("invite_id")
            invite_token = data.get("token")
            if invite_token:
                r.ok(f"POST /orgs/{{id}}/invites (got token)")
            else:
                r.fail("POST invites", "no token in response")
        else:
            r.fail("POST invites", f"status {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        r.fail("POST invites", str(e)[:120])

    # 5. Landing-info по токену (public)
    if invite_token:
        try:
            resp = await client.get(f"{api}/invites/{invite_token}", timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("org_name") == org_name:
                    r.ok("GET /invites/{token} (org_name matches)")
                else:
                    r.fail("GET invite info", f"org_name mismatch: {data.get('org_name')}")
            else:
                r.fail("GET invite info", f"status {resp.status_code}")
        except Exception as e:
            r.fail("GET invite info", str(e)[:120])

    # 6. List members (должен быть 1 founder)
    try:
        resp = await client.get(
            f"{api}/orgs/{org_id}/members", headers=headers, timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            count = data.get("count", 0)
            if count >= 1:
                r.ok(f"GET /orgs/{{id}}/members (count={count})")
            else:
                r.fail("GET members", f"expected >=1, got {count}")
        else:
            r.fail("GET members", f"status {resp.status_code}")
    except Exception as e:
        r.fail("GET members", str(e)[:120])

    # 7. Revoke invite (cleanup)
    if invite_id:
        try:
            resp = await client.request(
                "DELETE", f"{api}/orgs/{org_id}/invites/{invite_id}",
                headers=headers, timeout=10.0,
            )
            if resp.status_code == 200:
                r.ok("DELETE /orgs/{id}/invites/{iid} (cleanup)")
            else:
                r.fail("DELETE invite", f"status {resp.status_code}")
        except Exception as e:
            r.fail("DELETE invite", str(e)[:120])

    print(f"\n  ℹ️  Smoke-org {org_id} ({org_name}) остаётся в системе")
    print(f"     (нет DELETE /orgs endpoint; почистите вручную если нужно)")


async def run(args) -> int:
    base = args.base_url.rstrip("/")
    r = SmokeResult()

    print(f"🔍 Smoke test against {base}\n")
    print("Health checks:")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Reachability probe.
        try:
            await client.get(f"{base}/ping", timeout=10.0)
        except Exception as e:
            print(f"❌ Backend unreachable at {base}: {e}")
            return 2

        await _check_health(client, base, r)

        if args.full:
            if not args.admin_token:
                print("\n⚠️  --full requires --admin-token (system-admin JWT)")
                print("    Skipping org-flow checks.")
            else:
                print("\nOrg flow checks:")
                await _check_org_flow(client, base, args.admin_token, r)

    return r.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description="Tessbrain post-deploy smoke test")
    parser.add_argument("--base-url", type=str, default="http://localhost:8000",
                        help="базовый URL backend'а")
    parser.add_argument("--admin-token", type=str, default=None,
                        help="system-admin JWT для --full org-flow")
    parser.add_argument("--full", action="store_true",
                        help="включить org + invite flow checks")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
