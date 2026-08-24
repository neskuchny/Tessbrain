"""Unit-тесты для backend.db.migrate (W10 production hygiene).

Проверяем pure-logic части: discovery + sorting + checksum + skip-rules.
Реальный `migrate()` против БД — в integration-suite (нужен живой Postgres).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "db" / "migrate.py"
)
# Загружаем под изолированным namespace, чтобы обойти backend.db.__init__
# (он подтягивает sqlalchemy/asyncpg которых может не быть в sandbox).
_spec = importlib.util.spec_from_file_location("_migrate_isolated", _PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

Migration = _module.Migration
discover_migrations = _module.discover_migrations
_SKIP_NAMES = _module._SKIP_NAMES
_SKIP_SUFFIXES = _module._SKIP_SUFFIXES


def test_discover_returns_real_migrations() -> None:
    """Базовая проверка — функция находит наши настоящие миграции."""
    out = discover_migrations()
    assert len(out) > 0
    versions = [m.version for m in out]
    # Должны увидеть знакомые имена.
    assert "030_user_profiles_v2" in versions
    assert "040_audit_log" in versions


def test_discover_sorts_lexicographically() -> None:
    out = discover_migrations()
    versions = [m.version for m in out]
    assert versions == sorted(versions)


def test_discover_skips_template_files(tmp_path: pathlib.Path) -> None:
    """Файлы .sql.template и .sql.example не считаются миграциями."""
    (tmp_path / "001_real.sql").write_text("SELECT 1;")
    (tmp_path / "002_template.sql.template").write_text("SELECT 2;")
    (tmp_path / "003_example.sql.example").write_text("SELECT 3;")

    out = discover_migrations(tmp_path)
    versions = [m.version for m in out]
    assert versions == ["001_real"]


def test_discover_skips_non_sql(tmp_path: pathlib.Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("# docs")
    (tmp_path / "002_b.txt").write_text("not a migration")
    out = discover_migrations(tmp_path)
    assert [m.version for m in out] == ["001_a"]


def test_discover_skips_legacy_filenames(tmp_path: pathlib.Path) -> None:
    """bwe_schema.sql — legacy, применяется отдельно."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "bwe_schema.sql").write_text("SELECT 2;")
    out = discover_migrations(tmp_path)
    assert [m.version for m in out] == ["001_a"]


def test_discover_skips_subdirectories(tmp_path: pathlib.Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "002_b.sql").write_text("SELECT 2;")
    out = discover_migrations(tmp_path)
    assert [m.version for m in out] == ["001_a"]


def test_checksum_deterministic(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "001_x.sql"
    p.write_text("CREATE TABLE x (id INT);")
    out = discover_migrations(tmp_path)
    assert len(out) == 1
    h1 = out[0].checksum
    out2 = discover_migrations(tmp_path)
    assert out2[0].checksum == h1


def test_checksum_changes_on_content_change(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "001_x.sql"
    p.write_text("CREATE TABLE x (id INT);")
    h1 = discover_migrations(tmp_path)[0].checksum
    p.write_text("CREATE TABLE x (id BIGINT);")  # одну строчку поменяли
    h2 = discover_migrations(tmp_path)[0].checksum
    assert h1 != h2


def test_migration_dataclass_immutable() -> None:
    """Migration — frozen dataclass; защита от случайных мутаций."""
    m = Migration(version="001_test", path=pathlib.Path("/tmp/x"), sql="SELECT 1;")
    import dataclasses
    assert dataclasses.is_dataclass(m)
    try:
        m.version = "999_hacked"  # type: ignore[misc]
    except (AttributeError, dataclasses.FrozenInstanceError):
        pass
    else:
        raise AssertionError("Migration should be frozen")


def test_empty_directory_returns_empty_list(tmp_path: pathlib.Path) -> None:
    out = discover_migrations(tmp_path)
    assert out == []


def test_nonexistent_directory_returns_empty_list(tmp_path: pathlib.Path) -> None:
    out = discover_migrations(tmp_path / "does-not-exist")
    assert out == []


def test_skip_lists_are_frozen() -> None:
    """Sanity: константы должны быть immutable."""
    assert isinstance(_SKIP_NAMES, frozenset)
    assert isinstance(_SKIP_SUFFIXES, tuple)
