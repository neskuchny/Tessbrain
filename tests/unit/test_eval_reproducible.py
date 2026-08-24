# -*- coding: utf-8 -*-
"""Воспроизводимость прогонов: числа должно быть чем повторить.

Аудит раздела «Доказательства вместо обещаний» нашёл разрыв: результаты
прогонов лежат в репозитории, а три харнесса — включая главное сравнение
с Mem0 — читали данные из /tmp. Файл клали руками; через месяц /tmp пуст,
и повторить опубликованные числа нельзя.

Контракты под проверкой:
  1. ни один харнесс не читает датасет из жёсткого пути в /tmp;
  2. у каждого датасета есть публичный источник ссылкой;
  3. кэш переживает перезагрузку (не /tmp);
  4. нет сети — понятная ошибка с адресом и путём, а не трассировка;
  5. уже лежащий файл используется как есть, в том числе по старому пути;
  6. раннер офлайн-тестов не считает успехом файл, который ничего не
     выполнил: «нет самозапуска» → пропущен, а не пройден;
  7. пропуск виден в отчёте и не смешивается с успехом.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.eval"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ds = _load("backend.core.eval.datasets", "backend/core/eval/datasets.py")
_runner = _load("offline_runner", "scripts/run_offline_tests.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def _code_lines(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


_HARNESSES = (
    "backend/core/eval/benchmark_mem0_h2h.py",
    "backend/core/eval/benchmark_locomo.py",
    "backend/core/eval/benchmark_analysis_mode.py",
    "backend/core/eval/benchmark_longmemeval.py",
)


# ── 1-2. Источник данных ────────────────────────────────────────────────

def test_no_harness_reads_dataset_from_tmp():
    for path in _HARNESSES:
        code = _code_lines(_src(path))
        for bad in ('open("/tmp/', "open('/tmp/", '"/tmp/locomo', '"/tmp/longmemeval'):
            assert bad not in code, f"{path}: датасет из жёсткого пути {bad}"
    print("✅ ни один харнесс не читает датасет из /tmp")


def test_harnesses_use_shared_loader():
    for path in _HARNESSES[:3]:
        code = _code_lines(_src(path))
        assert "from backend.core.eval.datasets import load_dataset" in code, path
    print("✅ харнессы берут данные через общий загрузчик")


def test_every_dataset_has_public_source():
    assert _ds.SOURCES, "список источников не должен быть пустым"
    for name, (url, title, _legacy) in _ds.SOURCES.items():
        assert url.startswith("https://"), f"{name}: источник обязан быть ссылкой"
        assert title, f"{name}: у датасета нет человеческого имени"
    print("✅ у каждого датасета публичный источник ссылкой")


def test_cache_is_not_tmp():
    old = os.environ.pop("EVAL_DATA_DIR", None)
    try:
        path = _ds.cache_dir()
        assert not path.startswith("/tmp"), (
            "кэш в /tmp — ровно та причина, по которой числа переставали "
            "воспроизводиться"
        )
    finally:
        if old is not None:
            os.environ["EVAL_DATA_DIR"] = old
    print("✅ кэш датасетов переживает перезагрузку")


# ── 3-5. Поведение загрузчика ───────────────────────────────────────────

def test_missing_dataset_explains_what_to_do():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("EVAL_DATA_DIR")
        os.environ["EVAL_DATA_DIR"] = tmp
        try:
            try:
                _ds.dataset_path("locomo10", allow_download=False)
                assert False, "должно было отказать: файла нет"
            except _ds.DatasetUnavailable as exc:
                text = str(exc)
                assert tmp in text, "в ошибке обязан быть путь, куда положить"
                assert "LoCoMo" in text, "в ошибке обязано быть имя датасета"
        finally:
            if old is None:
                os.environ.pop("EVAL_DATA_DIR", None)
            else:
                os.environ["EVAL_DATA_DIR"] = old
    print("✅ без сети — понятная ошибка с адресом и путём")


def test_unknown_dataset_named_explicitly():
    try:
        _ds.dataset_path("нет-такого")
        assert False, "неизвестный датасет обязан отказать"
    except _ds.DatasetUnavailable as exc:
        assert "неизвестный датасет" in str(exc)
    print("✅ неизвестный датасет отвергается по имени")


def test_existing_file_is_reused_without_download():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("EVAL_DATA_DIR")
        os.environ["EVAL_DATA_DIR"] = tmp
        try:
            target = os.path.join(tmp, "locomo10.json")
            with open(target, "w", encoding="utf-8") as f:
                json.dump([{"x": "y" * 2000}], f)
            # allow_download=False докажет, что скачивание не понадобилось.
            assert _ds.dataset_path("locomo10", allow_download=False) == target
            assert _ds.available("locomo10")
        finally:
            if old is None:
                os.environ.pop("EVAL_DATA_DIR", None)
            else:
                os.environ["EVAL_DATA_DIR"] = old
    print("✅ уже скачанный датасет переиспользуется")


def test_legacy_tmp_path_still_accepted():
    """У кого файл уже лежит по старому пути — прогон не должен качать заново."""
    _url, _title, legacy = _ds.SOURCES["longmemeval_oracle"]
    assert "/tmp/longmemeval_oracle.json" in legacy
    print("✅ прежний путь прогонов остаётся рабочим")


def test_truncated_download_is_not_accepted():
    """Страница ошибки вместо датасета не должна стать «данными»."""
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("EVAL_DATA_DIR")
        os.environ["EVAL_DATA_DIR"] = tmp
        try:
            with open(os.path.join(tmp, "locomo10.json"), "w") as f:
                f.write("404 not found")
            assert not _ds.available("locomo10"), (
                "подозрительно маленький файл не считается датасетом"
            )
        finally:
            if old is None:
                os.environ.pop("EVAL_DATA_DIR", None)
            else:
                os.environ["EVAL_DATA_DIR"] = old
    print("✅ обрезанный файл не принимается за датасет")


# ── 6-7. Раннер не выдаёт пустоту за успех ──────────────────────────────

def test_file_without_self_run_is_skipped_not_passed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test_nothing.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("def test_x():\n    assert False\n")  # никогда не вызовется
        category, reason, _ = _runner.run_file(path, timeout=30)
        assert category == _runner.SKIPPED, (
            "файл без самозапуска ничего не выполняет — это не успех"
        )
        assert "самозапуск" in reason
    print("✅ «ничего не выполнили» не считается пройденным")


def test_real_failure_is_reported_as_failure():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test_broken.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write('if __name__ == "__main__":\n    assert 1 == 2, "сломано"\n')
        category, reason, _ = _runner.run_file(path, timeout=30)
        assert category == _runner.FAILED and reason, (
            f"провал обязан быть провалом, получено: {category}"
        )
    print("✅ настоящий провал виден как провал")


def test_missing_dependency_is_skipped_with_reason():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test_needs_dep.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("import совсем_нет_такого_модуля\n"
                    'if __name__ == "__main__":\n    pass\n')
        category, reason, _ = _runner.run_file(path, timeout=30)
        assert category == _runner.SKIPPED and "ModuleNotFound" in reason
    print("✅ нехватка зависимости — пропуск с причиной, а не провал")


def test_passing_file_is_passed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test_ok.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write('if __name__ == "__main__":\n    assert True\n    print("ok")\n')
        category, _reason, _ = _runner.run_file(path, timeout=30)
        assert category == _runner.PASSED
    print("✅ настоящий успех виден как успех")


def test_ci_runs_the_runner_and_the_gate():
    # .github ищем и рядом с кодом, и уровнем выше. В монорепо код лежит
    # в подкаталоге tessent_brain/, а сборки — в корне над ним; в
    # собранном публичном репозитории код сам себе корень, и .github
    # лежит рядом. Жёсткое «уровнем выше» ломало тест ровно при
    # публикации — то есть там, где он нужнее всего.
    candidates = [
        os.path.join(ROOT, ".github", "workflows", "offline-checks.yml"),
        os.path.join(os.path.dirname(ROOT), ".github", "workflows",
                     "offline-checks.yml"),
    ]
    ci_path = next((p for p in candidates if os.path.exists(p)), None)
    assert ci_path, ("сборка обязана существовать в репозитории; искали: "
                     + ", ".join(candidates))
    with open(ci_path, encoding="utf-8") as f:
        ci = f.read()
    assert "scripts/run_offline_tests.py" in ci and "backend.core.eval" in ci
    print("✅ сборка гоняет контрактные тесты и проводку гейта")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты воспроизводимости прошли.")
