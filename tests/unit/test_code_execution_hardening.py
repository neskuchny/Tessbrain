# -*- coding: utf-8 -*-
"""Исполнение кода на сервере: deny-by-default и закрытые пути ввода.

Найдено при сверке раздела «Цифры» бизнес-документа с кодом.

Проблема была составной, и каждая часть по отдельности выглядела безобидно:
  1. `ANALYSIS_CODE_EXEC` имел дефолт "on" — при том что собственный
     докстринг функции и docs/ANALYSIS_TAB.md говорили «по умолчанию OFF».
  2. Поле `AnalysisDimension.code` (произвольный Python) приходило прямо
     из тела `POST /analysis/playbooks` и не проверялось валидатором.
  3. Бутстрап раннера полагался на урезанные builtins, а из них выходят
     штатным `().__class__.__base__.__subclasses__()`.

Вместе это давало исполнение произвольного кода на сервере любому
аутентифицированному пользователю. Ни одна встроенная методика поле `code`
не использует — единственным его источником был HTTP-запрос.

Чиним двумя независимыми рубежами: дефолт OFF и отказ принимать `code` на
входе. Урезанные builtins защитой не считаем и не полагаемся на них.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str, pkgs):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in pkgs:
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ONTO_PKGS = ("backend", "backend.core", "backend.core.ontology")
_wb = _load("backend.core.ontology.workbook",
            "backend/core/ontology/workbook.py", _ONTO_PKGS)
deny_check = _wb._deny_check


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── 1. Deny-by-default ──────────────────────────────────────────────────

def test_analysis_code_exec_defaults_to_off():
    src = _src("backend/core/analysis/code_runner.py")
    assert '"ANALYSIS_CODE_EXEC", "off"' in src, (
        "исполнение произвольного кода обязано быть выключено по умолчанию"
    )
    assert '"ANALYSIS_CODE_EXEC", "on"' not in src
    print("✅ ANALYSIS_CODE_EXEC выключен по умолчанию")


def test_enabled_flag_respects_env():
    """Явное включение по-прежнему работает — это осознанное решение."""
    runner = _load("backend.core.analysis.code_runner",
                   "backend/core/analysis/code_runner.py",
                   ("backend", "backend.core", "backend.core.analysis"))
    prev = os.environ.get("ANALYSIS_CODE_EXEC")
    try:
        os.environ.pop("ANALYSIS_CODE_EXEC", None)
        assert runner.is_enabled() is False, "без флага — выключено"
        os.environ["ANALYSIS_CODE_EXEC"] = "on"
        assert runner.is_enabled() is True, "явное включение работает"
        os.environ["ANALYSIS_CODE_EXEC"] = "off"
        assert runner.is_enabled() is False
    finally:
        os.environ.pop("ANALYSIS_CODE_EXEC", None)
        if prev is not None:
            os.environ["ANALYSIS_CODE_EXEC"] = prev
    print("✅ флаг уважает явное включение и выключение")


# ── 2. Код не принимается на входе ──────────────────────────────────────

def test_validation_rejects_code_field():
    src = _src("backend/core/analysis/validation.py")
    assert 'getattr(d, "code", None)' in src, (
        "валидатор обязан отклонять произвольный код в методике"
    )
    start = src.index('getattr(d, "code", None)')
    block = src[start:start + 400]
    assert "errors.append" in block, "это ошибка, а не предупреждение"
    print("✅ валидатор отклоняет поле code")


def test_all_write_routes_validate():
    """Создание, из шаблона и правка — все проходят через валидатор."""
    src = _src("backend/api/routes/analysis.py")
    assert src.count("validate_playbook") >= 3
    for marker in ("create_playbook", "update_playbook"):
        assert marker in src
    print("✅ все пути записи методики валидируются")


def test_bundled_playbooks_never_use_code():
    """У поля не осталось ни одного легитимного источника."""
    src = _src("backend/core/analysis/bundled.py")
    assert '"code"' not in src and "code=" not in src, (
        "если встроенная методика начнёт использовать code — запрет надо "
        "пересматривать осознанно, а не обходить"
    )
    print("✅ встроенные методики поле code не используют")


def test_methodology_import_uses_allowlist():
    """Методика из документа собирается по белому списку полей — иначе
    промпт-инъекция в загруженном документе внесла бы code."""
    src = _src("backend/core/analysis/methodology_import.py")
    start = src.index("dims_out.append({")
    block = src[start:src.index("})", start)]
    assert '"code"' not in block, (
        "сборка размерности из ответа модели не должна пропускать code"
    )
    for field in ('"key"', '"label"', '"computation"'):
        assert field in block
    print("✅ методика из документа собирается по белому списку полей")


# ── 3. Песочница воркбука: класс запретов вместо поимённого списка ──────

def test_all_readers_denied():
    """Поимённый список читателей пропускал read_table/read_fwf/read_xml."""
    for code in ('pd.read_table("/etc/passwd")',
                 'pd.read_fwf("/etc/passwd")',
                 'pd.read_xml("http://evil")',
                 'pd.read_stata("/x")',
                 'pd.read_feather("/x")',
                 'pd.read_csv("/x")'):
        assert deny_check(code) is not None, f"пропущен читатель: {code}"
    print("✅ запрещён весь класс read_*, а не перечисленные имена")


def test_file_writing_exports_denied():
    """to_json/to_html/to_xml/to_stata писали произвольные файлы."""
    for code in ('df.to_json("/tmp/x")', 'df.to_html("/tmp/x")',
                 'df.to_xml("/tmp/x")', 'df.to_stata("/tmp/x")',
                 'df.to_csv("/tmp/x")', 'df.to_feather("/tmp/x")'):
        assert deny_check(code) is not None, f"пропущен экспорт: {code}"
    print("✅ экспорт в файлы запрещён белым списком to_*")


def test_in_memory_conversions_still_allowed():
    """Белый список не должен ломать обычную аналитику."""
    for code in ('result = df["Выручка"].sum()',
                 'result = df.groupby("Месяц")["Сумма"].sum().to_dict()',
                 'result = df["x"].to_numpy().mean()',
                 'result = pd.to_numeric(df["a"]).sum()',
                 'result = df.head(3).to_markdown()'):
        assert deny_check(code) is None, f"заблокирован легитимный код: {code}"
    print("✅ преобразования в памяти по-прежнему работают")


def test_module_traversal_denied():
    """Обход в numpy через внутренности pandas."""
    for code in ('pd.core.frame.np.fromfile("/etc/passwd")',
                 'pd.io.common.get_handle("/x")',
                 'pd.compat.os.system("id")',
                 'pd._libs.lib.item_from_zerodim(1)'):
        assert deny_check(code) is not None, f"пропущен обход: {code}"
    print("✅ обход через внутренние модули pandas запрещён")


def test_classic_escapes_still_denied():
    for code in ("import os", 'open("/etc/passwd")', "__builtins__",
                 'eval("1")', 'getattr(pd, "read_csv")'):
        assert deny_check(code) is not None, f"пропущен классический побег: {code}"
    print("✅ классические побеги по-прежнему запрещены")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты защиты исполнения кода прошли.")
