# -*- coding: utf-8 -*-
"""Mandatory / team-based access (P5).

ПРОБЕЛ: схема (`store/schema.py`) кладёт на каждый узел `access_level`
(1..5: PUBLIC→TOP_SECRET) и `access_groups` (список групп/команд), но
`AccessControlService.filter_entities` фильтрует ТОЛЬКО по `project_id`.
То есть mandatory access control (MAC) описан, но не применяется —
RESTRICTED-узел с `access_groups=["team:finance"]` виден кому угодно
в проекте.

P5 вводит MAC поверх существующего RBAC/project-фильтра:

  • clearance по роли (VIEWER..ADMIN → max видимый access_level)
  • team-членство берётся из ОБЩЕЙ Supabase (отдельная таблица —
    Meetflow пишет команды туда; мы читаем напрямую) через
    ИНЪЕКТИРУЕМЫЙ fetcher → юнит-тест без сети
  • правило MAC: узел виден, если
      access_level <= clearance  И
      (access_groups пуст  ИЛИ  пересекается с team_ids пользователя)
    PUBLIC (level<=1) виден всегда.

Careful rollout (как P4): режим из env `MANDATORY_ACCESS_MODE`
(off|warn|strict), DEFAULT = off → нулевое изменение поведения, пока
явно не включат. warn = только лог «было бы скрыто» (наблюдаемость),
strict = реально фильтруем. Никогда не raises; сбой загрузки команд
fail-safe в off/warn, fail-closed для RESTRICTED в strict.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

# переиспользуем единый enum строгости (OFF/WARN/STRICT), без параллельного
from backend.core.ontology.sdk import EnforcementMode

logger = logging.getLogger(__name__)

# fetcher(user_id) -> list[{"team_id": str, "clearance": int|None}]
TeamFetcherFn = Callable[[str], Awaitable[list[dict]]]

# Роль → максимальный видимый access_level (clearance).
# 1 PUBLIC, 2 INTERNAL, 3 CONFIDENTIAL, 4 RESTRICTED, 5 TOP_SECRET.
ROLE_CLEARANCE: dict[str, int] = {
    "viewer": 2,
    "employee": 2,
    "manager": 3,
    "admin": 5,
}
_DEFAULT_CLEARANCE = 2  # неизвестная роль → INTERNAL


def mode_from_env() -> EnforcementMode:
    """`MANDATORY_ACCESS_MODE` env → режим.

    ВКЛЮЧЕНО (strict) по умолчанию 2026-08-13 — аудит бизнес-карты,
    раздел 16. Почему это безопасно как умолчание: узел без грифа считается
    INTERNAL (2), и допуск любой роли не ниже 2 — то есть на непомеченных
    данных включение НИЧЕГО не скрывает. Прятаться начинает только то, что
    кто-то явно пометил выше (конфиденциально/ограниченно) или ограничил
    группой («видно только финансам») — ровно ради этого слой и существует;
    выключенным он превращал проставленный гриф в пустую формальность.
    Выключение: MANDATORY_ACCESS_MODE=off; режим наблюдения: =warn.
    """
    raw = (os.getenv("MANDATORY_ACCESS_MODE", "strict") or "strict").strip().lower()
    if raw in ("strict", "enforce", "on"):
        return EnforcementMode.STRICT
    if raw in ("warn", "observe", "log"):
        return EnforcementMode.WARN
    return EnforcementMode.OFF


@dataclass
class TeamMembership:
    """Что пользователю разрешено по MAC."""
    user_id: str
    team_ids: frozenset[str] = field(default_factory=frozenset)
    clearance: int = _DEFAULT_CLEARANCE
    source: str = "role"  # role | role+teams | unavailable

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "team_ids": sorted(self.team_ids),
            "clearance": self.clearance,
            "source": self.source,
        }


def build_membership(
    user_id: str,
    role: str,
    team_rows: list[dict] | None,
) -> TeamMembership:
    """Чистая сборка членства из роли + строк Supabase. Не raises.

    Команда может поднимать clearance (row["clearance"]), но НИКОГДА
    не опускает ниже роли — берём максимум.
    """
    base = ROLE_CLEARANCE.get((role or "").strip().lower(), _DEFAULT_CLEARANCE)
    clearance = base
    teams: set[str] = set()
    src = "role"

    if team_rows:
        src = "role+teams"
        for row in team_rows:
            if not isinstance(row, dict):
                continue
            tid = row.get("team_id") or row.get("team") or row.get("id")
            if tid:
                teams.add(str(tid))
            rc = row.get("clearance")
            if isinstance(rc, (int, float)):
                clearance = max(clearance, int(rc))

    clearance = max(1, min(clearance, 5))
    return TeamMembership(
        user_id=user_id,
        team_ids=frozenset(teams),
        clearance=clearance,
        source=src,
    )


def can_see_node(
    props: dict,
    membership: TeamMembership,
    mode: EnforcementMode = EnforcementMode.OFF,
) -> tuple[bool, str]:
    """MAC-решение по одному узлу. Не raises. Возвращает (видно, причина).

    В WARN всегда видно=True, но причина заполняется, когда STRICT
    скрыл бы (наблюдаемость перед включением).
    """
    if mode == EnforcementMode.OFF:
        return True, ""

    try:
        level = int(props.get("access_level", 2))
    except (TypeError, ValueError):
        level = 2
    level = max(1, min(level, 5))

    raw_groups = props.get("access_groups")
    groups = (
        {str(g) for g in raw_groups}
        if isinstance(raw_groups, (list, tuple, set)) else set()
    )

    # PUBLIC виден всегда
    if level <= 1:
        return True, ""

    deny_reason = ""
    if level > membership.clearance:
        deny_reason = (
            f"clearance {membership.clearance} < access_level {level}"
        )
    elif groups and not (groups & membership.team_ids):
        deny_reason = f"not in any of access_groups {sorted(groups)}"

    if not deny_reason:
        return True, ""

    if mode == EnforcementMode.WARN:
        logger.info("MAC(observe): would hide node — %s", deny_reason)
        return True, deny_reason  # наблюдаемость, не режем

    return False, deny_reason


def filter_nodes(
    nodes: list[dict],
    membership: TeamMembership,
    mode: EnforcementMode = EnforcementMode.OFF,
) -> tuple[list[dict], int]:
    """Отфильтровать список узлов по MAC. Не raises. (видимые, скрыто_n)."""
    if mode == EnforcementMode.OFF or not isinstance(nodes, list):
        return (nodes if isinstance(nodes, list) else []), 0

    visible: list[dict] = []
    hidden = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        ok, _reason = can_see_node(n, membership, mode)
        if ok:
            visible.append(n)
        else:
            hidden += 1
    return visible, hidden


def make_supabase_team_fetcher() -> TeamFetcherFn:
    """Fetcher команд из ОБЩЕЙ Supabase (отдельная таблица).

    Конфиг env: SUPABASE_URL, SUPABASE_KEY (те же, что в
    automation_service), таблица `MEETFLOW_TEAMS_TABLE` (default
    `team_members`). Никогда не raises — при любой проблеме [].
    """
    base = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "") or ""
    table = os.getenv("MEETFLOW_TEAMS_TABLE", "team_members") or "team_members"

    async def _fetch(user_id: str) -> list[dict]:
        if not base or not key or not user_id:
            return []
        try:
            import httpx

            url = f"{base}/rest/v1/{table}"
            params = {
                "user_id": f"eq.{user_id}",
                "select": "team_id,clearance",
            }
            headers = {
                "apikey": key,
                "Authorization": f"Bearer {key}",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("MAC: team fetch failed (fail-safe): %s", exc)
            return []

    return _fetch


async def load_team_membership(
    user_id: str,
    role: str,
    *,
    fetcher: TeamFetcherFn | None = None,
    mode: EnforcementMode = EnforcementMode.OFF,
) -> TeamMembership:
    """Загрузить членство (роль + команды из Supabase). Не raises.

    fetcher инъектируем (тест без сети). При сбое/OFF — членство по
    роли с пометкой source. В STRICT при недоступности команд
    fail-closed обеспечивается тем, что team_ids пуст → узлы с
    непустыми access_groups скрываются.
    """
    if mode == EnforcementMode.OFF:
        return build_membership(user_id, role, None)

    fetch = fetcher or make_supabase_team_fetcher()
    rows: list[dict] = []
    try:
        rows = await fetch(user_id)
    except Exception as exc:  # fetcher обязан не бросать, но на всякий
        logger.warning("MAC: load membership failed (fail-safe): %s", exc)
        rows = []

    m = build_membership(user_id, role, rows or None)
    if not rows:
        m.source = "unavailable"
    return m


__all__ = [
    "ROLE_CLEARANCE",
    "EnforcementMode",
    "TeamFetcherFn",
    "TeamMembership",
    "build_membership",
    "can_see_node",
    "filter_nodes",
    "load_team_membership",
    "make_supabase_team_fetcher",
    "mode_from_env",
]
