"""Канонизация транскрипта и content_hash для дедупа.

Цель: дать стабильный отпечаток `(source, external_id, content_hash)` для
эпизода — независимо от косметических различий (whitespace, регистр, BOM,
порядок отметок таймстампов на одном и том же файле).

Намеренно НЕ нормализуем семантику (имена участников, переводы) — это уже
анализ. Здесь только синтаксис: чтобы повторная загрузка того же mp3 → тот
же hash.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Optional

# Что считаем "тем же транскриптом":
#   - игнор всех пустых строк и trailing whitespace
#   - схлопывание подряд идущих пробелов в один
#   - NFKC normalization (склеивает разные unicode-формы той же буквы)
#   - lowercase (Zoom иногда меняет регистр в speaker labels)
#   - вычищение таймштампов вида [00:01:23] / 00:01:23,456 → пустота
#     (один и тот же текст с разной частотой sample'ов даст разные таймштампы)

_TS_PATTERNS = [
    re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]"),   # [00:01:23.456]
    re.compile(r"\(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\)"),   # (00:01:23)
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}[.,]\d{3}\b"),           # 00:01:23,456 (SRT)
]
_WS_RE = re.compile(r"\s+")


def normalize_transcript(text: str) -> str:
    """Канонизировать транскрипт для хэширования.

    Принципы:
        - детерминирована: одинаковый вход → одинаковый выход
        - устойчива к whitespace/регистру/таймштампам
        - НЕ устойчива к семантическим правкам (это уже разные эпизоды)
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    for ts in _TS_PATTERNS:
        s = ts.sub(" ", s)
    s = s.lower().strip()
    s = _WS_RE.sub(" ", s)
    return s


def content_hash(text: str) -> str:
    """sha256 от нормализованного транскрипта. Hex, 64 символа."""
    norm = normalize_transcript(text)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Извлечение source / external_id из meeting payload
# ---------------------------------------------------------------------------
# Meeting payload приходит из разных мест (Supabase meetings, MeetFlow webhook,
# analyze/ingest). Один универсальный extractor — чтобы не разбрасывать логику
# по ingest-точкам.

_SUPABASE_PROVIDER_FIELDS = (
    "provider",          # 'zoom' / 'gmeet' / 'teams' / 'meetflow' / 'manual'
    "source",            # legacy
    "integration_type",  # alternative
)

_EXTERNAL_ID_FIELDS = (
    "external_id",
    "provider_meeting_id",
    "zoom_meeting_id",
    "gcal_event_id",
    "teams_meeting_id",
    "meetflow_event_id",
)


def extract_source(meeting: dict[str, Any]) -> Optional[str]:
    """Достать имя источника из meeting payload. None если нечего — fallback на 'manual'."""
    for f in _SUPABASE_PROVIDER_FIELDS:
        v = meeting.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def extract_external_id(meeting: dict[str, Any]) -> Optional[str]:
    """Достать external ID. None для manual upload."""
    for f in _EXTERNAL_ID_FIELDS:
        v = meeting.get(f)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def build_dedup_key(meeting: dict[str, Any], transcript: str) -> dict[str, Optional[str]]:
    """Собрать полный ключ дедупа для одного meeting payload.

    Возвращает все 4 поля что нужны ledger'у. None допустим — partial unique
    index в SQL включается только когда заполнены все 3 (org_id, source,
    external_id); content_hash работает как независимый fallback.
    """
    return {
        "source": extract_source(meeting),
        "external_id": extract_external_id(meeting),
        "content_hash": content_hash(transcript) if transcript else None,
    }
