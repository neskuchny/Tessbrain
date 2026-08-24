# -*- coding: utf-8 -*-
"""
tenant_io — безопасная запись per-user JSON-сторов (аудит M2/M5 + M1).

ПРОБЛЕМА (из прод-аудита): event_log/cross_source_mentions/supersede писали
через прямой open(path,"w") — НЕ атомарно. Краш/OOM-kill в момент записи →
усечённый/битый JSON → при следующем _load() блок `except: =[]` МОЛЧА стирал
всю историю пользователя без следа. Плюс «атомарные» plan_memory/insight_store
не делали fsync. И главное (M1): 2 granian + taskiq на одном томе писали без
блокировок → last-writer-wins → молчаливая потеря конкурентных записей.

ЭТОТ МОДУЛЬ даёт:
- atomic_write_json (M2/M5): tempfile → fsync → os.replace → fsync директории.
  После возврата на диске ЛИБО старый целый файл, ЛИБО новый — не усечённый.
- file_lock (M1): межпроцессная эксклюзивная блокировка (fcntl.flock) по
  sidecar `{path}.lock`. Сериализует конкурентных писателей одного файла.
- read_modify_write (M1): паттерн lock→reload→merge→atomic_write — каждый
  писатель ПОД ЛОКОМ перечитывает файл (видит запись конкурента), применяет
  свою дельту и атомарно пишет. Закрывает last-writer-wins без потери данных.

Чистый stdlib.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any, Callable, Optional


def atomic_write_json(path: str, obj: Any, *, indent: int | None = 1) -> None:
    """Атомарно записать obj как JSON в path (fsync файла и директории).

    После возврата файл на диске целый. Бросает исключение при сбое записи —
    вызывающий best-effort код решает, ронять ли (но старый файл цел в любом случае).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())          # M5: данные точно на диске до replace
        os.replace(tmp, path)             # M2: атомарная замена
        # fsync директории — чтобы сама запись о замене дошла до диска
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass                          # не все ФС поддерживают dir-fsync
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def file_lock(path: str, *, timeout: float = 10.0):
    """Межпроцессная эксклюзивная блокировка по sidecar `{path}.lock` (M1).

    fcntl.flock на Unix; на платформах без fcntl (Windows-dev) — no-op
    (одно-процессный dev не страдает гонкой). timeout — макс. ожидание лока.
    """
    try:
        import fcntl
    except Exception:
        # нет fcntl (не-Unix) — деградируем в no-op (dev), прод на Linux
        yield
        return

    import time as _time
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = _time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if _time.monotonic() >= deadline:
                    raise TimeoutError(f"file_lock timeout: {lock_path}")
                _time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_modify_write(
    path: str,
    merge: Callable[[Any], Any],
    *,
    default: Any = None,
    indent: int | None = 1,
    timeout: float = 10.0,
) -> Any:
    """ПОД ЛОКОМ: перечитать файл с диска → merge(текущее)->новое → атомарно
    записать (M1 — без last-writer-wins).

    merge получает АКТУАЛЬНОЕ с диска состояние (видит запись конкурента) и
    возвращает новое состояние для записи. Возвращает записанное состояние.
    Если merge вернул None — запись пропускается (no-op), возвращается текущее.
    """
    with file_lock(path, timeout=timeout):
        current = _read_json(path, default)
        new_state = merge(current)
        if new_state is None:
            return current
        atomic_write_json(path, new_state, indent=indent)
        return new_state
