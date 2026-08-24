"""Тесты для validation + bundled templates Analysis Engine."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.analysis import bundled
from backend.core.analysis.models import AnalysisDimension, AnalysisPlaybook
from backend.core.analysis.validation import validate_playbook

# === validation ============================================================


def test_valid_playbook_no_errors():
    pb = AnalysisPlaybook(
        id="x", name="DD",
        dimensions=[
            AnalysisDimension(key="debt", label="Долг", kind="quantitative",
                              computation="debt / equity", inputs=["debt", "equity"],
                              benchmark={"direction": "higher_worse", "red_flag": 2.0}),
            AnalysisDimension(key="legal", label="Юр", kind="qualitative"),
        ],
    )
    errors, _ = validate_playbook(pb)
    assert errors == []


def test_missing_name_error():
    pb = AnalysisPlaybook(id="x", name="")
    errors, _ = validate_playbook(pb)
    assert any("name" in e for e in errors)


def test_duplicate_key_error():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[
            AnalysisDimension(key="dup", label="A"),
            AnalysisDimension(key="dup", label="B"),
        ],
    )
    errors, _ = validate_playbook(pb)
    assert any("дубликат" in e for e in errors)


def test_invalid_computation_error():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(
            key="bad", label="Bad", kind="quantitative",
            computation="__import__('os')", inputs=["x"],
        )],
    )
    errors, _ = validate_playbook(pb)
    assert any("computation" in e for e in errors)


def test_valid_multistep_computation_ok():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(
            key="margin", label="Маржа", kind="quantitative",
            computation="g = rev - cost\nm = g / rev\nm", inputs=["rev", "cost"],
        )],
    )
    errors, _ = validate_playbook(pb)
    assert errors == []


def test_invalid_benchmark_direction_error():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(
            key="d", label="D", kind="quantitative",
            computation="d", inputs=["d"],
            benchmark={"direction": "sideways", "red_flag": 1.0},
        )],
    )
    errors, _ = validate_playbook(pb)
    assert any("direction" in e for e in errors)


def test_benchmark_nonnumeric_threshold_error():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(
            key="d", label="D", kind="quantitative",
            computation="d", inputs=["d"],
            benchmark={"red_flag": "много"},
        )],
    )
    errors, _ = validate_playbook(pb)
    assert any("red_flag" in e for e in errors)


def test_access_level_out_of_range_error():
    pb = AnalysisPlaybook(id="x", name="P", access_level=9)
    errors, _ = validate_playbook(pb)
    assert any("access_level" in e for e in errors)


def test_qualitative_with_computation_warning():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(
            key="q", label="Q", kind="qualitative", computation="x + 1",
        )],
    )
    errors, warnings = validate_playbook(pb)
    assert errors == []  # не блокирует
    assert any("qualitative" in w for w in warnings)


def test_invalid_kind_error():
    pb = AnalysisPlaybook(
        id="x", name="P",
        dimensions=[AnalysisDimension(key="d", label="D", kind="weird")],
    )
    errors, _ = validate_playbook(pb)
    assert any("kind" in e for e in errors)


# === bundled templates =====================================================


def test_all_bundled_templates_valid():
    """REGRESSION: каждый готовый шаблон проходит валидацию без ошибок."""
    for tid, tpl in bundled.BUNDLED_PLAYBOOKS.items():
        pb = AnalysisPlaybook.from_dict({**tpl, "id": "x"})
        errors, _ = validate_playbook(pb)
        assert errors == [], f"template {tid} has validation errors: {errors}"


def test_list_templates():
    templates = bundled.list_templates()
    assert len(templates) >= 3
    ids = {t["template_id"] for t in templates}
    assert "due_diligence_standard" in ids
    assert "financial_quarter" in ids
    for t in templates:
        assert "name" in t and "dimensions_count" in t


def test_get_template():
    tpl = bundled.get_template("due_diligence_standard")
    assert tpl is not None
    assert tpl["name"] == "Due Diligence (стандарт)"
    assert len(tpl["dimensions"]) == 4
    # Возвращается копия (не мутируем оригинал).
    tpl["name"] = "MUTATED"
    assert bundled.get_template("due_diligence_standard")["name"] != "MUTATED"


def test_get_template_unknown():
    assert bundled.get_template("nope") is None


def test_financial_template_has_multistep():
    """Финансовый шаблон использует многошаговую формулу маржи."""
    tpl = bundled.get_template("financial_quarter")
    margin = next(d for d in tpl["dimensions"] if d["key"] == "margin")
    assert "\n" in margin["computation"]  # multistep
