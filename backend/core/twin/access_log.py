# -*- coding: utf-8 -*-
"""Журнал обращений к слепку человека — «кто спрашивал мою цифровую копию».

Зачем: слепок отвечает от лица человека, которого в комнате нет. Пока
обращения нигде не фиксируются, человек физически не может узнать, что его
копию о чём-то спрашивали — и единственная защита от неуместных вопросов
это добрая воля спрашивающего. Видимый владельцу журнал работает как
сдерживающий фактор: спрашивают аккуратнее, когда знают, что это видно.

Что пишем и чего НЕ пишем:
  - пишем: когда, кто спросил, о чьём слепке, в каком тенанте, длину вопроса;
  - НЕ пишем текст вопроса. Это защита второй стороны: спрашивающий часто
    формулирует чувствительное («как он относится к моему переводу в другой
    отдел»), и складывать это в доступный субъекту слепка журнал — значит
    менять одну проблему приватности на другую. Длины хватает, чтобы отличить
    односложное «привет» от развёрнутого разбора.

Ключ файла — нормализованный person_id, поэтому журнал находится и когда
слепок звали по id узла, и когда по имени (снапшоты умеют оба). Хранилище —
append-only jsonl рядом с остальными примитивами памяти; never-raise на
чтении и на записи: журнал не имеет права уронить сам ответ слепка.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Сколько записей отдаём максимум за раз (файл читается с конца).
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000
# Ограничение на размер читаемого хвоста, чтобы длинный журнал не тянул память.
_TAIL_BYTES = 512 * 1024


def _dir() -> Path:
    return Path(os.environ.get("TWIN_ACCESS_LOG_DIR", "").strip()
                or "data/twin_access")


def normalize_person_key(person_id: str) -> str:
    """Ключ журнала: одинаковый для «p:ivan-petrov», «Иван Петров» и «иван  петров».

    Слепок можно звать и по id узла, и по имени — если бы ключ различался,
    владелец видел бы только половину обращений к себе."""
    s = str(person_id or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _path(person_id: str) -> Path:
    key = normalize_person_key(person_id)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return _dir() / f"{digest}.jsonl"


def record(*, person_id: str, asker_uid: str, tenant_uid: str,
           person_name: str = "", org_id: Optional[str] = None,
           question_chars: int = 0, granted: bool = True,
           reason: str = "") -> None:
    """Записать обращение. Пишем и отказы: «кто пытался» — тоже сигнал.

    Никогда не поднимает исключение — журнал не должен ломать ответ слепка."""
    try:
        pid = str(person_id or "").strip()
        if not pid:
            return
        rec = {
            "ts": int(time.time()),
            "person_id": pid,
            "person_name": str(person_name or "")[:200],
            "asker_uid": str(asker_uid or "")[:100],
            "tenant_uid": str(tenant_uid or "")[:100],
            "org_id": str(org_id or "")[:100] or None,
            "question_chars": max(0, int(question_chars or 0)),
            "granted": bool(granted),
            "reason": str(reason or "")[:200],
        }
        p = _path(pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("twin access_log: запись не удалась", exc_info=True)


def _read_rows(person_id: str) -> List[Dict[str, Any]]:
    try:
        p = _path(person_id)
        if not p.exists():
            return []
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # выбрасываем обрезанную посередине строку
            raw = fh.read().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("twin access_log: чтение не удалось", exc_info=True)
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
    return rows


def list_for_person(person_id: str, *, limit: int = _DEFAULT_LIMIT,
                    aliases: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Обращения к слепку, свежие сверху.

    aliases — другие написания того же человека (id узла и имя), их журналы
    сливаются: человека могли звать и так, и так."""
    keys = [person_id] + list(aliases or [])
    seen_keys = set()
    rows: List[Dict[str, Any]] = []
    for k in keys:
        nk = normalize_person_key(k)
        if not nk or nk in seen_keys:
            continue
        seen_keys.add(nk)
        rows.extend(_read_rows(k))

    rows.sort(key=lambda r: int(r.get("ts") or 0), reverse=True)
    lim = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    return rows[:lim]


def summary_for_person(person_id: str, *,
                       aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    """Сводка для карточки: сколько всего, сколько за неделю, кто чаще всех."""
    rows = list_for_person(person_id, limit=_MAX_LIMIT, aliases=aliases)
    if not rows:
        return {"total": 0, "last_7d": 0, "denied": 0,
                "askers": [], "last_ts": None}

    week_ago = int(time.time()) - 7 * 86400
    by_asker: Dict[str, int] = {}
    denied = 0
    last_7d = 0
    for r in rows:
        if int(r.get("ts") or 0) >= week_ago:
            last_7d += 1
        if not r.get("granted", True):
            denied += 1
        a = str(r.get("asker_uid") or "")
        if a:
            by_asker[a] = by_asker.get(a, 0) + 1

    askers = sorted(({"asker_uid": k, "count": v} for k, v in by_asker.items()),
                    key=lambda x: x["count"], reverse=True)[:10]
    return {
        "total": len(rows),
        "last_7d": last_7d,
        "denied": denied,
        "askers": askers,
        "last_ts": int(rows[0].get("ts") or 0) or None,
    }
