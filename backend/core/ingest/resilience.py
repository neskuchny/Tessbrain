# -*- coding: utf-8 -*-
"""Повторы и журнал неудач для сбора данных в память компании.

Зачем: пайплайн сбора (переписки, встречи, файлы) ходит по сети к чужим
API. Сеть моргнула, Slack отдал 429, провайдер перезагрузился — и кусок
памяти компании не доехал. До этого модуля такой сбой ловился одним
`except` с записью в debug-лог: в проде не видно вообще, повтора нет,
следа нет. Пользователь узнавал об этом никогда — просто в мозге чего-то
не оказывалось, и списать это было не на что.

Два инструмента, оба намеренно простые:

  1. `with_retries` — повтор с экспоненциальной задержкой и джиттером.
     Повторяем ТОЛЬКО то, что имеет шанс пройти со второй попытки:
     сеть, таймаут, 429, 5xx. На 401/403/404 повтор бессмысленен —
     токен не станет валидным от того, что мы попробуем ещё три раза,
     а лишние попытки только удлиняют цикл и жгут лимиты.

  2. `record_failure` / `list_failures` — журнал того, что всё-таки не
     доехало: источник, стадия, ошибка, сколько попыток сделали. Чтобы
     это было видно человеку и можно было перезапустить руками.

Хранилище — jsonl рядом с остальными примитивами (data/ingest_failures/),
never-raise: журнал не имеет права уронить сам сбор.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Коды, при которых повтор осмысленен: нас притормозили или у той стороны
# временно плохо. Всё остальное (права, отсутствие объекта, кривой запрос)
# со второй попытки не починится.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_MAX_FAILURES_KEPT = 500     # хвост журнала на источник-пользователя
_TAIL_BYTES = 512 * 1024


def default_attempts() -> int:
    """Сколько всего попыток на шаг. 1 = поведение как было (без повторов)."""
    try:
        n = int(os.environ.get("INGEST_RETRY_ATTEMPTS", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, 8))


def default_base_delay() -> float:
    try:
        d = float(os.environ.get("INGEST_RETRY_BASE_DELAY", "1.5"))
    except ValueError:
        d = 1.5
    return max(0.0, min(d, 30.0))


def _status_of(exc: BaseException) -> Optional[int]:
    """HTTP-код из исключения, если он там есть.

    Клиенты разных библиотек кладут его по-разному, поэтому смотрим
    несколько мест, а не завязываемся на конкретный httpx/aiohttp."""
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v <= 599:
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            v = getattr(resp, attr, None)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
    return None


def is_retryable(exc: BaseException) -> bool:
    """Стоит ли пробовать ещё раз."""
    # Отмена задачи — не сбой сети, повторять нельзя ни в коем случае.
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError,
                        OSError)):
        return True

    st = _status_of(exc)
    if st is not None:
        return st in _RETRYABLE_STATUS

    # Библиотеки HTTP-клиентов часто не наследуются от OSError; опираемся на
    # имя класса как на последний признак, не притаскивая их в зависимости.
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connect", "network", "socket",
                               "temporar", "unavailable", "ratelimit")):
        return True
    return False


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    label: str = "",
) -> T:
    """Выполнить fn с повторами. Возвращает результат или поднимает
    последнее исключение — решение, что делать с провалом, остаётся
    вызывающему.

    Задержка растёт экспоненциально и размывается джиттером: если все
    источники упали одновременно (провайдер лёг), их повторы не должны
    выстроиться в один синхронный залп."""
    total = attempts if attempts is not None else default_attempts()
    delay = base_delay if base_delay is not None else default_base_delay()
    last: BaseException

    for i in range(1, total + 1):
        try:
            return await fn()
        except BaseException as exc:      # noqa: BLE001 — решаем ниже
            last = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            if i >= total or not is_retryable(exc):
                raise
            pause = delay * (2 ** (i - 1)) + random.uniform(0, delay / 2)
            logger.info("ingest retry %s: попытка %d/%d не удалась (%s), "
                        "повтор через %.1fs", label or "step", i, total,
                        type(exc).__name__, pause)
            await asyncio.sleep(pause)

    raise last  # недостижимо, но делает контракт явным для линтера


# ── журнал неудач ──────────────────────────────────────────────────


def _dir() -> Path:
    return Path(os.environ.get("INGEST_FAILURES_DIR", "").strip()
                or "data/ingest_failures")


def _path(user_id: str) -> Path:
    safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:80]
    return _dir() / f"{safe or 'unknown'}.jsonl"


def record_failure(user_id: str, *, source_key: str, stage: str,
                   error: BaseException | str, attempts: int = 1,
                   platform: str = "") -> None:
    """Записать, что кусок данных не доехал. Никогда не поднимает."""
    try:
        msg = (f"{type(error).__name__}: {error}"
               if isinstance(error, BaseException) else str(error))
        rec = {
            "ts": int(time.time()),
            "source_key": str(source_key or "")[:200],
            "platform": str(platform or "")[:40],
            "stage": str(stage or "")[:40],
            "error": msg[:500],
            "attempts": int(attempts or 1),
        }
        p = _path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("ingest failures: запись не удалась", exc_info=True)


def list_failures(user_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    """Неудачи, свежие сверху."""
    try:
        p = _path(user_id)
        if not p.exists():
            return []
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()
            raw = fh.read().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("ingest failures: чтение не удалось", exc_info=True)
        return []

    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    rows.sort(key=lambda r: int(r.get("ts") or 0), reverse=True)
    return rows[:max(1, min(int(limit or 100), _MAX_FAILURES_KEPT))]


def failures_summary(user_id: str) -> Dict[str, Any]:
    """Сводка: сколько всего, за сутки, по каким источникам и стадиям."""
    rows = list_failures(user_id, limit=_MAX_FAILURES_KEPT)
    if not rows:
        return {"total": 0, "last_24h": 0, "sources": [], "stages": [],
                "last_ts": None}

    day_ago = int(time.time()) - 86400
    by_source: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    last_24h = 0
    for r in rows:
        if int(r.get("ts") or 0) >= day_ago:
            last_24h += 1
        s = str(r.get("source_key") or "?")
        by_source[s] = by_source.get(s, 0) + 1
        st = str(r.get("stage") or "?")
        by_stage[st] = by_stage.get(st, 0) + 1

    top = lambda d, k: sorted(  # noqa: E731 — локальный однострочник
        ({k: n, "count": c} for n, c in d.items()),
        key=lambda x: x["count"], reverse=True)[:10]
    return {
        "total": len(rows),
        "last_24h": last_24h,
        "sources": top(by_source, "source_key"),
        "stages": top(by_stage, "stage"),
        "last_ts": int(rows[0].get("ts") or 0) or None,
    }


def clear_failures(user_id: str) -> int:
    """Очистить журнал (после разбора). Возвращает, сколько записей было."""
    try:
        p = _path(user_id)
        if not p.exists():
            return 0
        n = len(list_failures(user_id, limit=_MAX_FAILURES_KEPT))
        p.unlink()
        return n
    except Exception:
        logger.debug("ingest failures: очистка не удалась", exc_info=True)
        return 0
