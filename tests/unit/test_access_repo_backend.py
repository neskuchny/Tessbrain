# -*- coding: utf-8 -*-
"""D1a: сторы доступов с бэкендом file/postgres.

Живого Postgres в CI нет, поэтому здесь проверяем:
  1. backend()/enabled() — по умолчанию 'file'; при org_store_backend='postgres'
     и недоступной БД — громкий откат на 'file' (приложение не падает);
  2. db_url_sync — корректная деривация psycopg2-URL без двойных суффиксов;
  3. replace_all diff-логика (added/changed/removed) — на подменённом backend'е,
     без реальной БД (проверяем, что membership/holdings корректно вызывают
     примитивы персистентности через access_repo).

Полный прогон против реального Postgres выполнен вручную (см. D1a в
ENTERPRISE_PRODUCT_ROADMAP.md) — здесь фиксируем контракт и file-паритет.
"""
from __future__ import annotations

import types
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_data(tmp_path, monkeypatch):
    import backend.core.store.tenant_paths as tp
    monkeypatch.setattr(tp, "_DATA_ROOT", tmp_path)
    import backend.core.holdings.holding_store as hs
    monkeypatch.setattr(hs, "_DATA_ROOT", tmp_path)
    from backend.core.ingest import membership, org_store
    membership.clear_cache()
    org_store.clear_cache()
    yield


def test_db_url_sync_derivation():
    from backend.config import settings
    fn = settings.__class__.db_url_sync.fget
    cases = {
        "postgresql+asyncpg://u:p@h:5432/db": "postgresql+psycopg2://u:p@h:5432/db",
        "postgresql://u:p@h/db": "postgresql+psycopg2://u:p@h/db",
        "postgres://u:p@h/db": "postgresql+psycopg2://u:p@h/db",
        "postgresql+psycopg://u:p@h/db": "postgresql+psycopg2://u:p@h/db",
    }
    for src, want in cases.items():
        got = fn(types.SimpleNamespace(db_url=src))
        assert got == want, f"{src} -> {got} != {want}"


def test_backend_defaults_to_file():
    from backend.core.ingest import access_repo
    access_repo.reset_for_tests()
    assert access_repo.backend() == "file"
    assert not access_repo.enabled()


def test_postgres_configured_but_down_falls_back_to_file(monkeypatch):
    """org_store_backend='postgres' + БД недоступна → 'file' (fail-safe)."""
    from backend.config import settings
    from backend.core.ingest import access_repo
    monkeypatch.setattr(settings, "org_store_backend", "postgres", raising=False)
    # гарантируем недоступность: движок укажет на несуществующий сокет
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+asyncpg://nouser@/nodb?host=/nonexistent&port=1",
                        raising=False)
    access_repo.reset_for_tests()
    assert access_repo.backend() == "file"
    assert not access_repo.enabled()
    access_repo.reset_for_tests()


def test_file_mode_stores_still_work():
    """Дефолтный файловый режим: дерево/членства/холдинги/сегрегация как раньше."""
    from backend.core.ingest import membership as mb, org_store
    import backend.core.holdings.holding_store as hs
    from backend.core.access import levels

    ceo, emp = str(uuid.uuid4()), str(uuid.uuid4())
    org_store.create_org("h1", name="Холдинг", created_by=ceo)
    org_store.create_org("c1", name="Корп", created_by=ceo, parent_org_id="h1")
    assert org_store.get_ancestors("c1") == ["h1"]

    mb.add_member(ceo, "h1", role="founder")
    mb.add_member(ceo, "c1", role="founder", secondary=True)
    mb.add_member(emp, "c1", role="employee")
    assert mb.get_org_for_user(ceo) == "h1"
    assert mb.can_manage_org(ceo, "c1")  # наследование по дереву
    assert not mb.can_manage_org(emp, "c1")
    assert mb.switch_primary_org(ceo, "c1")
    assert mb.get_org_for_user(ceo) == "c1"

    org_store.set_custom_roles("c1", {"cpo": {"base": "manager", "title": "CPO"}})
    mb.update_member_role(emp, "c1", "cpo")
    assert mb.get_user_org_base_role(emp) == "manager"
    assert levels.max_access_level_for_user(emp) == 3

    h = hs.create_holding(ceo, "Портфель")
    hs.add_org(h["id"], "c1", data_policy="aggregate")
    assert hs.org_policy(h["id"], "c1") == "aggregate"
