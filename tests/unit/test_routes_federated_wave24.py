"""Wave 2.4 — federated read в snapshots/templates/lineage/metrics.

Тесты ВЕРИФИЦИРУЮТ что каждый из 4 routes теперь использует
merged_graph_view_for_user вместо прямого GraphBuilder. Это критично:
если кто-то откатит Wave 2.4 (или забудет про federated при добавлении
нового route), команда снова перестанет видеть org-данные.

Стратегия: для каждого файла проверяем, что
  - merged_graph_view_for_user импортирован/доступен
  - GraphBuilder больше не создаётся напрямую (за исключением legacy
    путей, которые мы намеренно оставили)
  - graph_path_for_user больше не вызывается в файле
Это static-check, не запуск handler'ов (handlers требуют LLMRouter,
Postgres, full ASGI — отдельный E2E уровень).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROUTES_DIR = Path(__file__).resolve().parents[2] / "backend" / "api" / "routes"


def _read(name: str) -> str:
    return (_ROUTES_DIR / name).read_text(encoding="utf-8")


def _count_calls(source: str, callable_name: str) -> int:
    """Подсчитать сколько раз callable_name вызывается в source."""
    tree = ast.parse(source)
    cnt = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == callable_name:
                cnt += 1
            elif isinstance(func, ast.Attribute) and func.attr == callable_name:
                cnt += 1
    return cnt


# =============================================================================
# Все 4 файла — merged_graph_view_for_user импортирован и используется
# =============================================================================


@pytest.mark.parametrize("filename", [
    "snapshots.py",
    "templates.py",
    "lineage.py",
    "metrics.py",
])
def test_route_imports_merged_graph_view(filename):
    src = _read(filename)
    assert "merged_graph_view_for_user" in src, (
        f"{filename} не импортирует merged_graph_view_for_user — "
        "Wave 2.4 откачен. Команда не видит org-данные через этот route."
    )


@pytest.mark.parametrize("filename,expected_min_calls", [
    ("snapshots.py", 6),   # 5 read sites + 1 legacy site (всё переделано)
    ("templates.py", 2),
    ("lineage.py", 2),
    ("metrics.py", 1),
])
def test_route_calls_merged_view(filename, expected_min_calls):
    """Каждый replaced site должен вызывать merged_graph_view_for_user."""
    src = _read(filename)
    cnt = _count_calls(src, "merged_graph_view_for_user")
    assert cnt >= expected_min_calls, (
        f"{filename}: ожидалось хотя бы {expected_min_calls} вызовов "
        f"merged_graph_view_for_user, найдено {cnt}. "
        "Возможно, кто-то откатил часть Wave 2.4."
    )


# =============================================================================
# Не должно остаться прямых GraphBuilder(...) для personal-graph
# =============================================================================


@pytest.mark.parametrize("filename", [
    "snapshots.py",
    "templates.py",
    "lineage.py",
    "metrics.py",
])
def test_no_direct_graph_builder_with_personal_path(filename):
    """В файле не должно быть `GraphBuilder(graph_storage_path=graph_path_for_user(...))`
    — это паттерн до Wave 2.4 (читает только personal-граф)."""
    src = _read(filename)
    # Простейшая эвристика: если в файле есть И GraphBuilder, И
    # graph_path_for_user, и они расположены близко — это, вероятно,
    # старый паттерн. Точечно: ищем graph_path_for_user в любом виде.
    assert "graph_path_for_user" not in src, (
        f"{filename}: graph_path_for_user всё ещё используется. "
        "Это значит часть read sites не перешла на federated view."
    )


# =============================================================================
# corrections.py — НЕ должен использовать merged view (это writer-flow)
# =============================================================================


def test_corrections_route_does_NOT_use_merged_view():
    """REGRESSION: corrections.py — это writer (two-phase correction
    manager). merged_graph_view_for_user блокирует save → корректировки
    были бы тихо потеряны. Этот test охраняет от случайной "помощи"
    coverage-script'а, который заменит все routes без анализа write-flow.
    """
    src = _read("corrections.py")
    assert "merged_graph_view_for_user" not in src, (
        "corrections.py — writer; merged view (read-only) не подходит. "
        "Если Wave 2.4 решил расширить покрытие, нужен отдельный паттерн "
        "для write-flow (RW org-graph, не read-only merged view)."
    )
