"""Тесты для ops-скриптов (A.1/A.3/A.4).

Проверяет критичную логику без запуска реальных Postgres/backend:
  - apply_migrations: discovery + сортировка по числовому префиксу
  - apply_migrations: sha256 stable
  - bootstrap_org: _is_uuid validation + реальное создание org через
    storage-слой (tmp DATA_ROOT)
  - post_deploy_smoke: SmokeResult аккумуляция
"""
from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"


# Force-load tenant_paths под каноническим именем и держим как "правильную"
# версию. Несколько тестов в общем suite подменяют sys.modules для
# backend.core.store.* — без restore наш monkeypatch на _DATA_ROOT
# цепляется не к тому module-объекту, и bootstrap пишет в РЕАЛЬНЫЙ data/.
_REAL_STORE: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL_STORE[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    _pkg = types.ModuleType("backend.core.store")
    _pkg.__path__ = [str(_ROOT / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = _pkg
    _REAL_STORE["backend.core.store"] = _pkg

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")


@pytest.fixture(autouse=True)
def _restore_store_modules():
    """Перед каждым тестом восстанавливаем «правильную» версию
    tenant_paths в sys.modules (защита от tamper'еров в общем suite)."""
    for name, mod in _REAL_STORE.items():
        sys.modules[name] = mod
    yield


def _load_script(name: str):
    """Загрузить scripts/{name}.py как модуль."""
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_ops_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_ops_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# apply_migrations
# =============================================================================


def test_migration_discovery_sorts_by_numeric_prefix():
    am = _load_script("apply_migrations")
    migrations = am._discover_migrations()
    names = [name for name, _ in migrations]

    # Должны быть отсортированы: 001 < 003 < 020 < 020b < 040 < 250
    # Найдём индексы ключевых миграций.
    def _idx(prefix):
        for i, n in enumerate(names):
            if n.startswith(prefix):
                return i
        return -1

    i003 = _idx("003_knowledge")
    i040 = _idx("040_audit")
    i250 = _idx("250_processed")

    assert i003 >= 0, "003 migration not found"
    assert i040 >= 0, "040 migration not found"
    assert i250 >= 0, "250 migration not found"
    # Порядок: 003 < 040 < 250
    assert i003 < i040 < i250, f"order wrong: 003={i003}, 040={i040}, 250={i250}"


def test_migration_020_before_020b():
    """020 (multitenant) должна идти перед 020b (strict)."""
    am = _load_script("apply_migrations")
    names = [name for name, _ in am._discover_migrations()]
    i020 = next((i for i, n in enumerate(names) if n.startswith("020_")), -1)
    i020b = next((i for i, n in enumerate(names) if n.startswith("020b")), -1)
    if i020 >= 0 and i020b >= 0:
        assert i020 < i020b


def test_migration_250_discovered():
    """Wave 1 миграция 250 должна быть в списке."""
    am = _load_script("apply_migrations")
    names = [name for name, _ in am._discover_migrations()]
    assert any("250_processed_meetings_org_dedup" in n for n in names)


def test_sha256_stable():
    am = _load_script("apply_migrations")
    assert am._sha256("hello") == am._sha256("hello")
    assert am._sha256("hello") != am._sha256("world")
    assert len(am._sha256("x")) == 64


# =============================================================================
# bootstrap_org
# =============================================================================


def test_bootstrap_is_uuid():
    bo = _load_script("bootstrap_org")
    assert bo._is_uuid("11111111-1111-1111-1111-111111111111")
    assert not bo._is_uuid("not-a-uuid")
    assert not bo._is_uuid("")
    assert not bo._is_uuid("11111111111111111111111111111111")  # без дефисов


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Изолированный DATA_ROOT для bootstrap storage.

    Патчим именно force-loaded _tp (которую _restore_store_modules
    положила в sys.modules) — иначе bootstrap резолвит другой
    module-объект и пишет в реальный data/.
    """
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    from backend.core.ingest import membership, org_store
    membership.clear_cache()
    org_store.clear_cache()
    return tmp_path


def _args(**kw):
    """Мок argparse.Namespace."""
    defaults = {
        "add_member": False,
        "org_id": None,
        "name": None,
        "founder_user_id": None,
        "member_user_id": None,
        "role": "employee",
        "dry_run": False,
    }
    defaults.update(kw)
    return type("Args", (), defaults)()


def test_bootstrap_creates_org_and_founder(tmp_data):
    bo = _load_script("bootstrap_org")
    from backend.core.ingest import membership, org_store

    org_id = str(uuid.uuid4())
    founder = str(uuid.uuid4())

    rc = bo._bootstrap_new_org(_args(
        org_id=org_id, name="Acme Test", founder_user_id=founder,
    ))
    assert rc == 0
    # Org создана
    meta = org_store.get_org(org_id)
    assert meta is not None
    assert meta["name"] == "Acme Test"
    # Founder добавлен
    assert membership.get_org_for_user(founder) == org_id
    assert membership.get_user_org_role(founder) == "founder"


def test_bootstrap_rejects_bad_uuid(tmp_data):
    bo = _load_script("bootstrap_org")
    rc = bo._bootstrap_new_org(_args(
        org_id="not-uuid", name="X", founder_user_id=str(uuid.uuid4()),
    ))
    assert rc == 2


def test_bootstrap_rejects_duplicate_org(tmp_data):
    bo = _load_script("bootstrap_org")
    org_id = str(uuid.uuid4())
    founder = str(uuid.uuid4())
    # Первый bootstrap OK.
    bo._bootstrap_new_org(_args(org_id=org_id, name="X", founder_user_id=founder))
    # Второй — отклонён.
    rc = bo._bootstrap_new_org(_args(
        org_id=org_id, name="Y", founder_user_id=str(uuid.uuid4()),
    ))
    assert rc == 1


def test_bootstrap_dry_run_writes_nothing(tmp_data):
    bo = _load_script("bootstrap_org")
    from backend.core.ingest import org_store

    org_id = str(uuid.uuid4())
    rc = bo._bootstrap_new_org(_args(
        org_id=org_id, name="X", founder_user_id=str(uuid.uuid4()),
        dry_run=True,
    ))
    assert rc == 0
    assert org_store.get_org(org_id) is None  # ничего не записано


def test_bootstrap_add_member(tmp_data):
    bo = _load_script("bootstrap_org")
    from backend.core.ingest import membership

    org_id = str(uuid.uuid4())
    founder = str(uuid.uuid4())
    bo._bootstrap_new_org(_args(org_id=org_id, name="X", founder_user_id=founder))

    employee = str(uuid.uuid4())
    rc = bo._add_member(_args(
        add_member=True, org_id=org_id, member_user_id=employee, role="employee",
    ))
    assert rc == 0
    assert membership.is_org_member(employee, org_id)


def test_bootstrap_add_member_to_nonexistent_org(tmp_data):
    bo = _load_script("bootstrap_org")
    rc = bo._add_member(_args(
        add_member=True, org_id=str(uuid.uuid4()),
        member_user_id=str(uuid.uuid4()), role="employee",
    ))
    assert rc == 1  # org не существует


def test_bootstrap_add_member_cross_org_blocked(tmp_data):
    bo = _load_script("bootstrap_org")
    from backend.core.ingest import membership

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    bo._bootstrap_new_org(_args(org_id=org_a, name="A", founder_user_id=str(uuid.uuid4())))
    bo._bootstrap_new_org(_args(org_id=org_b, name="B", founder_user_id=str(uuid.uuid4())))

    user = str(uuid.uuid4())
    membership.add_member(user, org_a, role="employee")
    # Попытка добавить в org_b → cross-org block.
    rc = bo._add_member(_args(
        add_member=True, org_id=org_b, member_user_id=user, role="employee",
    ))
    assert rc == 1


# =============================================================================
# post_deploy_smoke
# =============================================================================


def test_smoke_result_accumulation():
    smoke = _load_script("post_deploy_smoke")
    r = smoke.SmokeResult()
    r.ok("check1")
    r.ok("check2")
    r.fail("check3", "boom")
    assert len(r.passed) == 2
    assert len(r.failed) == 1
    # summary возвращает 1 при наличии failures
    assert r.summary() == 1


def test_smoke_result_all_pass():
    smoke = _load_script("post_deploy_smoke")
    r = smoke.SmokeResult()
    r.ok("check1")
    assert r.summary() == 0
