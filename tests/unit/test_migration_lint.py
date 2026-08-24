"""Unit-тесты для backend.db.migration_lint (W41)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

# Загружаем модуль напрямую (backend/db/__init__.py тянет тяжёлые deps).
_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "db" / "migration_lint.py"
)
_spec = importlib.util.spec_from_file_location("migration_lint", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

lint_sql = _mod.lint_sql
split_statements = _mod.split_statements
SEVERITY_ERROR = _mod.SEVERITY_ERROR
SEVERITY_WARNING = _mod.SEVERITY_WARNING


# === split_statements / strip_comments ================================

def test_split_simple() -> None:
    out = split_statements("CREATE TABLE x (id INT); CREATE INDEX i ON x(id);")
    assert len(out) == 2


def test_split_empty() -> None:
    assert split_statements("") == []
    assert split_statements(";;;") == []


def test_split_strips_line_comments() -> None:
    sql = """
    -- DROP TABLE foo;
    CREATE TABLE x (id INT);
    """
    out = split_statements(sql)
    # `-- DROP TABLE foo` strip'ается, остаётся только CREATE.
    assert len(out) == 1
    assert "DROP" not in out[0][1]


def test_split_strips_block_comments() -> None:
    sql = "/* DROP TABLE foo; */ CREATE TABLE x (id INT);"
    out = split_statements(sql)
    assert len(out) == 1
    assert "DROP" not in out[0][1]


def test_split_keeps_dollar_quoted() -> None:
    """$$...$$ блоки не split'аются по `;` внутри."""
    sql = """
    CREATE FUNCTION foo() RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'no; way';
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
    out = split_statements(sql)
    assert len(out) == 1


def test_split_preserves_line_numbers() -> None:
    sql = "\n\nCREATE TABLE x (id INT);\n\nCREATE INDEX i ON x(id);"
    out = split_statements(sql)
    assert out[0][0] == 3   # CREATE TABLE на строке 3
    assert out[1][0] == 5   # CREATE INDEX на строке 5


# === Rules: D001 DROP TABLE ===========================================

def test_d001_drop_table_flagged() -> None:
    findings = lint_sql("DROP TABLE foo;")
    assert any(f.code == "D001" for f in findings)
    f = next(x for x in findings if x.code == "D001")
    assert f.severity == SEVERITY_ERROR


def test_d001_commented_drop_table_not_flagged() -> None:
    """Закомментированный DROP в rollback-блоке — не ошибка."""
    findings = lint_sql("-- DROP TABLE foo;\nCREATE TABLE foo (id INT);")
    assert not any(f.code == "D001" for f in findings)


def test_d001_lint_ignore_works() -> None:
    sql = """
    -- LINT-IGNORE: D001 — table только что добавлена, никто её ещё не использует
    DROP TABLE staging_temp;
    """
    findings = lint_sql(sql)
    assert not any(f.code == "D001" for f in findings)


# === D002 DROP COLUMN =================================================

def test_d002_drop_column_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo DROP COLUMN bar;")
    assert any(f.code == "D002" for f in findings)


# === D003 ALTER COLUMN TYPE ===========================================

def test_d003_alter_column_type_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo ALTER COLUMN bar TYPE BIGINT;")
    assert any(f.code == "D003" for f in findings)


def test_d003_set_data_type_variant() -> None:
    findings = lint_sql("ALTER TABLE foo ALTER COLUMN bar SET DATA TYPE BIGINT;")
    assert any(f.code == "D003" for f in findings)


# === D004 ADD COLUMN NOT NULL без DEFAULT =============================

def test_d004_add_column_not_null_without_default_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo ADD COLUMN bar TEXT NOT NULL;")
    codes = [f.code for f in findings]
    assert "D004" in codes


def test_d004_with_default_not_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo ADD COLUMN bar TEXT NOT NULL DEFAULT '';")
    assert not any(f.code == "D004" for f in findings)


# === D005 SET NOT NULL ================================================

def test_d005_set_not_null_warning() -> None:
    findings = lint_sql("ALTER TABLE foo ALTER COLUMN bar SET NOT NULL;")
    f = next((x for x in findings if x.code == "D005"), None)
    assert f is not None
    assert f.severity == SEVERITY_WARNING


# === D006 CREATE INDEX without CONCURRENTLY ===========================

def test_d006_create_index_warning() -> None:
    findings = lint_sql("CREATE INDEX idx_foo ON foo(bar);")
    assert any(f.code == "D006" for f in findings)


def test_d006_concurrently_skipped() -> None:
    findings = lint_sql("CREATE INDEX CONCURRENTLY idx_foo ON foo(bar);")
    assert not any(f.code == "D006" for f in findings)


def test_d006_if_not_exists_skipped() -> None:
    """CREATE INDEX IF NOT EXISTS обычно в инстал-скрипте, не в migration."""
    findings = lint_sql("CREATE INDEX IF NOT EXISTS idx_foo ON foo(bar);")
    assert not any(f.code == "D006" for f in findings)


# === D007 RENAME ======================================================

def test_d007_rename_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo RENAME TO bar;")
    assert any(f.code == "D007" for f in findings)


def test_d007_rename_column_flagged() -> None:
    findings = lint_sql("ALTER TABLE foo RENAME COLUMN x TO y;")
    assert any(f.code == "D007" for f in findings)


# === D008 TRUNCATE ====================================================

def test_d008_truncate_warning() -> None:
    findings = lint_sql("TRUNCATE TABLE foo;")
    assert any(f.code == "D008" for f in findings)


# === Multi-rule + clean migrations ====================================

def test_safe_migration_no_findings() -> None:
    """Pattern: CREATE INDEX CONCURRENTLY + ADD COLUMN с DEFAULT — clean."""
    sql = """
    CREATE TABLE IF NOT EXISTS public.new_table (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    ALTER TABLE public.existing_table ADD COLUMN new_col TEXT NOT NULL DEFAULT '';
    CREATE INDEX CONCURRENTLY idx_new ON public.new_table (id);
    """
    findings = lint_sql(sql)
    assert findings == [], f"unexpected findings: {[f.code for f in findings]}"


def test_multi_rule_violations() -> None:
    sql = """
    DROP TABLE old_thing;
    ALTER TABLE foo ADD COLUMN bar TEXT NOT NULL;
    CREATE INDEX idx_foo ON foo(bar);
    """
    findings = lint_sql(sql)
    codes = {f.code for f in findings}
    assert {"D001", "D004", "D006"}.issubset(codes)


def test_finding_format_includes_file_and_line() -> None:
    findings = lint_sql("DROP TABLE foo;")
    f = findings[0]
    out = f.format(pathlib.Path("test.sql"))
    assert "test.sql" in out
    assert f.code in out
    assert "ERROR" in out or "WARNING" in out
