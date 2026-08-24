# -*- coding: utf-8 -*-
"""
Tenant-aware (per-user & per-org) storage paths.

Задача: обеспечить изоляцию данных не промптом, а на уровне путей/файлов.

Архитектура данных (3 уровня):
1. Personal Space — личные данные пользователя (data/graphs/{user_id}.json)
2. Organization Space — общий граф компании (data/orgs/{org_id}/graph.json)
3. Shared via access_level/access_groups — контроль видимости

При синхронизации: данные → личный граф → (promote) → организационный граф.
"""

from __future__ import annotations

import re
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Default user ID for anonymous/unauthenticated requests
DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000000"

# Важно: пути должны быть независимы от текущей рабочей директории процесса (cwd).
# Иначе при запуске backend из другого каталога данные пишутся в "чужой" ./data.
_TESSENT_BRAIN_ROOT = Path(__file__).resolve().parents[3]  # .../tessent_brain
_DATA_ROOT = _TESSENT_BRAIN_ROOT / "data"


def _require_uuid(value: str, field_name: str) -> str:
    if not value or not _UUID_RE.match(value):
        raise ValueError(f"Invalid {field_name}")
    return value


def graphs_dir() -> Path:
    d = _DATA_ROOT / "graphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def vectors_dir() -> Path:
    d = _DATA_ROOT / "vector_indexes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def graph_path_for_user(user_id: str) -> str:
    """Path to the per-user NetworkX graph JSON."""
    user_id = _require_uuid(user_id, "user_id")
    return str(graphs_dir() / f"{user_id}.json")


def vector_index_path_for_user(user_id: str) -> str:
    """Path to the per-user in-memory vector index JSON (fallback when Qdrant isn't used)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(vectors_dir() / f"{user_id}.json")


def snapshots_dir_for_user(user_id: str) -> Path:
    """Per-user snapshot directory. Раньше снапшоты писались в ОБЩУЮ
    data/snapshots — при консолидации нескольких юзеров последний затирал
    company/persons/... предыдущих (Пустовалова видела снапшоты Tessbrain).
    Изолируем по тенанту: data/snapshots_by_user/<user_id>/."""
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "snapshots_by_user" / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def mentions_dir() -> Path:
    d = _DATA_ROOT / "cross_source_mentions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cross_source_mentions_path_for_user(user_id: str) -> str:
    """Path to the per-user cross-source mentions store JSON
    (MEMORY_DESIGN_PRINCIPLES §6 №5 — упоминания сущности через встречи/документы/чаты)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(mentions_dir() / f"{user_id}.json")


def event_log_dir() -> Path:
    d = _DATA_ROOT / "event_log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def event_log_path_for_user(user_id: str) -> str:
    """Path to the per-user domain EventLog store JSON
    (MEMORY_DESIGN_PRINCIPLES §6 №4 — счёт доменных событий: решения/задачи/...)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(event_log_dir() / f"{user_id}.json")


def supersede_dir() -> Path:
    d = _DATA_ROOT / "supersede"
    d.mkdir(parents=True, exist_ok=True)
    return d


def supersede_store_path_for_user(user_id: str) -> str:
    """Path to the per-user SupersedeStore JSON
    (MEMORY_DESIGN_PRINCIPLES §6 №3 — недеструктивные версии фактов)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(supersede_dir() / f"{user_id}.json")


def insights_dir() -> Path:
    d = _DATA_ROOT / "insights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def insights_path_for_user(user_id: str) -> str:
    """Path to the per-user InsightStore JSON (история ночных инсайтов —
    docs/CAPABILITY_READINESS.md B6)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(insights_dir() / f"{user_id}.json")


def plan_memory_dir() -> Path:
    d = _DATA_ROOT / "plan_memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_memory_path_for_user(user_id: str) -> str:
    """Path to the per-user PlanMemory JSON («как мы делаем такие задачи» —
    память планов композитора, docs/CAPABILITY_READINESS.md)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(plan_memory_dir() / f"{user_id}.json")


def sima_kanon_dir() -> Path:
    d = _DATA_ROOT / "sima_kanon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sima_kanon_path_for_user(user_id: str) -> str:
    """Path to the per-user KanonStore JSON (вердикты tri-state приёмки
    SIMA-блоков и desync-статусы — docs/SIMA_KANON.md)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(sima_kanon_dir() / f"{user_id}.json")


def coding_credentials_dir() -> Path:
    d = _DATA_ROOT / "coding_credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def coding_credentials_path_for_user(user_id: str) -> str:
    """Path to the per-user coding-assistant credentials JSON (BYO-ключ
    помощника-сборщика: свой Anthropic/OpenAI токен, зашифрован Fernet)."""
    user_id = _require_uuid(user_id, "user_id")
    return str(coding_credentials_dir() / f"{user_id}.json")


def coding_home_dir_for_user(user_id: str) -> str:
    """Изолированная папка входа CLI-помощника для пользователя (BYO-подписка):
    свой `claude login` / `codex login` в отдельном каталоге. Внутри — подпапки
    по агенту (CLAUDE_CONFIG_DIR/CODEX_HOME указывают туда). Так подписки
    пользователей не смешиваются."""
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "coding_home" / user_id
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def sima_operator_profile_path_for_user(user_id: str) -> str:
    """Path to the per-user OperatorProfile JSON (типизированная память
    оператора: lessons/dont_use/always_use — docs/SIMA_KANON.md).
    Переживает проекты — следующий продукт стартует не с нуля."""
    user_id = _require_uuid(user_id, "user_id")
    return str(sima_kanon_dir() / f"{user_id}.operator.json")


def datasets_dir(user_id: str) -> Path:
    """Per-user каталог датасетов онтологии данных (docs/ru/DATA_ONTOLOGY.md):
    индекс + строки. Изоляция доступа — на уровне путей."""
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "datasets" / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def datasets_index_path_for_user(user_id: str) -> str:
    return str(datasets_dir(user_id) / "index.json")


def glossary_path_for_user(user_id: str) -> str:
    """Per-user корпоративный словарь синонимов/акронимов
    (docs/GLEAN_ADOPTION.md №3)."""
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "glossary"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


def verified_path_for_user(user_id: str) -> str:
    """Per-user отметки «проверено владельцем» на фактах
    (docs/GLEAN_ADOPTION.md №4). ОТДЕЛЬНЫЙ стор — supersede не трогается."""
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "verified"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


def bm25_indexes_dir() -> Path:
    """Directory for BM25 lexical search indexes."""
    d = _DATA_ROOT / "bm25_indexes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bm25_index_path_for_user(user_id: str) -> str:
    """Path to the per-user BM25 index pickle file."""
    user_id = _require_uuid(user_id, "user_id")
    return str(bm25_indexes_dir() / f"{user_id}.pkl")


def temporal_db_dir() -> Path:
    """Directory for temporal tracking SQLite databases."""
    d = _DATA_ROOT / "temporal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temporal_db_path_for_user(user_id: str) -> str:
    """Path to the per-user temporal tracking SQLite database."""
    user_id = _require_uuid(user_id, "user_id")
    return str(temporal_db_dir() / f"{user_id}.db")


# ============================================================================
# Organization-level paths (shared company data)
# ============================================================================

def orgs_dir() -> Path:
    """Directory for organization-level data."""
    d = _DATA_ROOT / "orgs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def org_graph_dir(org_id: str) -> Path:
    """
    Directory for one organization's shared data.

    Contains:
    - graph.json — shared knowledge graph
    - snapshots/ — company-wide snapshots
    - rule_books/ — company-level rule books
    """
    org_id = _require_uuid(org_id, "org_id")
    d = orgs_dir() / org_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def org_graph_path(org_id: str) -> str:
    """Path to the organization-level shared graph (NetworkX fallback)."""
    return str(org_graph_dir(org_id) / "graph.json")


def org_snapshots_dir(org_id: str) -> Path:
    """Directory for organization-level snapshots."""
    d = org_graph_dir(org_id) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def org_vector_path(org_id: str) -> str:
    """Path to the organization-level vector index."""
    return str(org_graph_dir(org_id) / "vectors.json")


# ============================================================================
# User-to-organization mapping
# ============================================================================

def user_org_mapping_path() -> str:
    """
    Path to the user→org mapping file.

    Format: {user_id: {org_id, role, access_level}}
    Used to determine which org a user belongs to.
    """
    return str(_DATA_ROOT / "user_org_mapping.json")

