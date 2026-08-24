# -*- coding: utf-8 -*-
"""Публичные датасеты для прогонов: скачиваются, кэшируются, проверяются.

Зачем модуль. Часть харнессов читала данные из `/tmp` — файл туда клали
руками во время прогона, ни скачивания, ни записи «откуда взять» в коде не
было. Через месяц `/tmp` пустой: харнесс падает с FileNotFoundError, и
воспроизвести опубликованные числа нельзя. Именно так вышло с главным
сравнением — head-to-head с Mem0.

Правила:
- источник указан ссылкой на публичный датасет, а не «файл у меня был»;
- кэш переживает перезагрузку (`~/.cache/tessent-eval`, переопределяется
  EVAL_DATA_DIR) — /tmp кэшем не считается;
- уже лежащий рядом файл (в т.ч. старый путь в /tmp) используется как есть:
  прогон не должен качать заново то, что уже есть;
- нет сети — понятная ошибка с адресом и путём, куда положить файл, а не
  трассировка.

Использование:
    from backend.core.eval.datasets import load_dataset
    data = load_dataset("longmemeval_oracle")
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DatasetUnavailable(RuntimeError):
    """Датасет не найден и не скачался. Текст объясняет, что сделать."""


def cache_dir() -> str:
    path = os.environ.get("EVAL_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "tessent-eval")
    os.makedirs(path, exist_ok=True)
    return path


# name → (url, человеческое имя, запасные пути от прежних прогонов)
SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "longmemeval_oracle": (
        "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_oracle",
        "LongMemEval (oracle)",
        ("/tmp/longmemeval_oracle.json",),
    ),
    "longmemeval_s": (
        "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s",
        "LongMemEval (S — с отвлекающими сессиями)",
        ("/tmp/longmemeval_s.json",),
    ),
    "locomo10": (
        "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        "LoCoMo (10 диалогов)",
        ("/tmp/locomo10.json",),
    ),
}

_MIN_BYTES = 1000  # меньше — это страница ошибки, а не датасет


def dataset_path(name: str, *, allow_download: bool = True) -> str:
    """Путь к файлу датасета. Скачивает при необходимости.

    Порядок: кэш → прежние пути прогонов → скачивание. Скачивание можно
    запретить (`allow_download=False`) — это режим для сборки без сети.
    """
    if name not in SOURCES:
        raise DatasetUnavailable(
            f"неизвестный датасет {name!r}; известные: {sorted(SOURCES)}")
    url, title, legacy = SOURCES[name]
    target = os.path.join(cache_dir(), f"{name}.json")

    for candidate in (target, *legacy):
        try:
            if os.path.exists(candidate) and os.path.getsize(candidate) > _MIN_BYTES:
                return candidate
        except OSError:
            continue

    if not allow_download:
        raise DatasetUnavailable(
            f"{title}: файла нет, скачивание запрещено. Положите его в "
            f"{target} или задайте EVAL_DATA_DIR")

    tmp = target + ".part"
    try:
        logger.info("eval: качаю %s → %s", title, target)
        urllib.request.urlretrieve(url, tmp)
        if os.path.getsize(tmp) <= _MIN_BYTES:
            raise OSError(f"ответ подозрительно мал ({os.path.getsize(tmp)} байт)")
        os.replace(tmp, target)
        return target
    except Exception as exc:
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        raise DatasetUnavailable(
            f"{title} не скачался ({type(exc).__name__}: {exc}). "
            f"Источник: {url}. Скачайте вручную и положите в {target} "
            f"(или задайте EVAL_DATA_DIR)"
        ) from exc


def load_dataset(name: str, *, allow_download: bool = True) -> Any:
    """Разобранный JSON датасета."""
    path = dataset_path(name, allow_download=allow_download)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def available(name: str) -> bool:
    """Есть ли датасет локально — без попытки скачать."""
    try:
        dataset_path(name, allow_download=False)
        return True
    except DatasetUnavailable:
        return False


__all__ = [
    "DatasetUnavailable",
    "SOURCES",
    "available",
    "cache_dir",
    "dataset_path",
    "load_dataset",
]
