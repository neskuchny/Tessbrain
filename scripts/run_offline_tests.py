#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон контрактных тестов, которым не нужны ни ключи, ни сеть.

Зачем. Проверки в репозитории были, но запускать их было нечем: полные
прогоны бенчмарков требуют ключей и денег, pytest тянет тяжёлые зависимости,
а непрерывной сборки не было вовсе. В итоге «проверки есть» держалось на
памяти того, кто их писал.

Этот раннер даёт одну команду и честный отчёт:

    python scripts/run_offline_tests.py

Каждый файл запускается отдельным процессом и попадает в одну из трёх
категорий — и это важнее, чем зелёная строчка в конце:

  ПРОШЁЛ    — отработал, утверждения выполнены;
  УПАЛ      — утверждение не выполнено; выход с кодом 1;
  ПРОПУЩЕН  — не хватает зависимости (pytest/numpy/litestar) или теста
              просят ключи. Пропуск НЕ считается успехом и виден в отчёте:
              «нет доказательств ≠ прошло».

Опции:
  --dir PATH     каталог с тестами (по умолчанию tests/unit)
  --timeout SEC  предел на файл (по умолчанию 120)
  --quiet        только итог
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASSED, FAILED, SKIPPED = "прошёл", "упал", "пропущен"

# Признаки «не хватает окружения», а не «сломан код».
_MISSING_DEP_MARKERS = (
    "ModuleNotFoundError",
    "SKIP: deps missing",
    "ImportError: cannot import name",
)
# Признаки «тест хочет ключи/сеть» — такие в офлайн-прогоне не считаем.
_NEEDS_SECRETS_MARKERS = (
    "OPENAI_API_KEY", "GOOGLE_API_KEY", "CI-only test",
)


def classify(code: int, out: str, err: str) -> tuple[str, str]:
    """Код возврата + вывод → (категория, короткая причина)."""
    if code == 0:
        return PASSED, ""
    blob = f"{out}\n{err}"
    for marker in _MISSING_DEP_MARKERS:
        if marker in blob:
            line = next((ln.strip() for ln in blob.splitlines()
                         if marker in ln), marker)
            return SKIPPED, line[:120]
    for marker in _NEEDS_SECRETS_MARKERS:
        if marker in blob:
            return SKIPPED, f"нужны ключи ({marker})"
    tail = [ln.strip() for ln in blob.strip().splitlines() if ln.strip()]
    return FAILED, (tail[-1][:200] if tail else f"код возврата {code}")


def has_self_run(path: str) -> bool:
    """Есть ли у файла блок самозапуска.

    Без него `python tests/unit/test_x.py` просто объявляет функции и выходит
    с кодом 0 — НИ ОДНА проверка не выполняется. Считать такой прогон
    успешным нельзя: это ровно то «зелено, потому что ничего не делали»,
    против чего вся эта работа. Такие файлы идут в пропущенные.
    """
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return False
    return '__name__ == "__main__"' in src or "__name__ == '__main__'" in src


def run_file(path: str, timeout: int) -> tuple[str, str, float]:
    started = time.time()
    if not has_self_run(path):
        return (SKIPPED, "нет самозапуска — выполняется только под pytest",
                time.time() - started)
    env = dict(os.environ)
    # Корень репозитория в путях импорта: часть тестов ходит в backend.*
    # и без этого падала «нет модуля backend», хотя код в порядке.
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # Windows: без UTF-8 дочерний процесс падает на print() эмодзи в cp1251,
    # а вывод здесь не декодируется. На Linux строка — no-op.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, path], cwd=ROOT, timeout=timeout,
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return FAILED, f"превышен предел {timeout}с", time.time() - started
    category, reason = classify(proc.returncode, proc.stdout, proc.stderr)
    return category, reason, time.time() - started


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join("tests", "unit"))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    tests_dir = os.path.join(ROOT, args.dir)
    if not os.path.isdir(tests_dir):
        print(f"каталог не найден: {tests_dir}", file=sys.stderr)
        return 2

    files = sorted(
        os.path.join(tests_dir, f) for f in os.listdir(tests_dir)
        if f.startswith("test_") and f.endswith(".py")
    )
    if not files:
        print("тестов не найдено — это не успех", file=sys.stderr)
        return 2

    results: dict[str, list[tuple[str, str]]] = {PASSED: [], FAILED: [], SKIPPED: []}
    started = time.time()
    for path in files:
        name = os.path.basename(path)
        category, reason, elapsed = run_file(path, args.timeout)
        results[category].append((name, reason))
        if not args.quiet:
            mark = {PASSED: "✅", FAILED: "❌", SKIPPED: "⏭ "}[category]
            suffix = f" — {reason}" if reason else ""
            print(f"{mark} {name} ({elapsed:.1f}с){suffix}")

    print("\n" + "─" * 60)
    print(f"прошли: {len(results[PASSED])}   "
          f"упали: {len(results[FAILED])}   "
          f"пропущены: {len(results[SKIPPED])}   "
          f"за {time.time() - started:.0f}с")

    if results[SKIPPED]:
        print("\nПропущены (нет зависимостей или нужны ключи) — это не успех:")
        for name, reason in results[SKIPPED]:
            print(f"  ⏭  {name}: {reason}")
    if results[FAILED]:
        print("\nУпали:")
        for name, reason in results[FAILED]:
            print(f"  ❌ {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
