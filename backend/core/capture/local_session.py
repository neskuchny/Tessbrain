# -*- coding: utf-8 -*-
"""Сборка транскрипта из локального захвата: несколько дорожек → одна встреча.

Приёмная часть для десктоп-агента, который пишет звук на устройстве
сотрудника без бота-участника в звонке. Само приложение — отдельный трек;
здесь то, куда оно шлёт результат.

Главное отличие от бота и причина, почему атрибуция здесь точнее.
Бот получает один смешанный поток и вынужден разбирать его целиком.
Локальный агент получает ДВА раздельных источника:

  - микрофон (`own`) — это гарантированно владелец устройства. Автор
    известен из аккаунта, разделение голосов для этих реплик не нужно
    вообще;
  - системный звук (`system`) — все остальные. Только их и надо
    разбирать диаризацией, и только у них появляются «Спикер 1/2/3».

Отсюда следствие, которого у бота быть не может: **чем больше участников
встречи пользуются приложением, тем точнее становится расшифровка**. Три
коллеги на звонке — три `own`-дорожки с точно известными авторами;
разбирать остаётся только внешних.

Но у этого есть цена, которую надо платить явно: если встречу пишут
двое, системная дорожка первого содержит голос второго, а у второго тот
же кусок лежит в `own`. Без дедупликации реплика попадёт в транскрипт
дважды — один раз с именем, один раз как «Спикер 2». Поэтому при слиянии
`own` всегда побеждает: он чище по звуку и точен по автору, а
пересекающиеся с ним чужие `system`-сегменты отбрасываются.

Имена берутся из смысла разговора, а не из голоса. Голосовые отпечатки —
это биометрические персональные данные (152-ФЗ), отдельный юридический
контур с письменным согласием каждого, включая внешних участников;
сюда мы не идём. Вместо этого: автор `own`-дорожки известен из аккаунта,
остальные — через `transcripts.speaker_resolver` (само-представления,
обращения по имени) и список участников из календаря как замкнутый набор
кандидатов.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

TRACK_OWN = "own"
TRACK_SYSTEM = "system"

# Насколько сегменты должны пересечься по времени, чтобы считать их одной
# и той же репликой, услышанной с двух устройств. Доля от короткого
# сегмента: половина — заметное совпадение, но не требующее идеального
# выравнивания часов между машинами.
_OVERLAP_RATIO = 0.5

# Допуск на расхождение часов между устройствами, секунды. Даже при NTP
# машины расходятся, а старт записи нажимают в разное время.
_CLOCK_SKEW = 2.0


@dataclass
class Segment:
    """Одна реплика."""
    start: float                       # секунды от начала сессии
    end: float
    text: str
    speaker_label: str = ""            # «Speaker 1» от диаризации, если есть
    author_name: str = ""              # разрешённое имя, если известно
    author_user_id: str = ""           # аккаунт, если дорожка своя
    track_kind: str = TRACK_SYSTEM
    source_device: str = ""            # с какого устройства пришло
    confidence: float = 0.0            # уверенность в авторстве, 0..1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": round(self.start, 2), "end": round(self.end, 2),
            "text": self.text, "speaker_label": self.speaker_label,
            "author_name": self.author_name,
            "author_user_id": self.author_user_id,
            "track_kind": self.track_kind, "source_device": self.source_device,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class Track:
    """Дорожка одного источника на одном устройстве."""
    kind: str = TRACK_SYSTEM
    owner_user_id: str = ""            # заполнен только для own
    owner_name: str = ""
    device_id: str = ""
    segments: List[Segment] = field(default_factory=list)


def parse_session(payload: Dict[str, Any]) -> List[Track]:
    """Разобрать то, что прислал агент. Мусор пропускаем, не падаем.

    Ожидаемая форма:
      {"tracks": [{"kind": "own", "owner_user_id": "...", "device_id": "...",
                   "segments": [{"start": 0.0, "end": 2.1, "text": "..."}]}]}
    """
    tracks: List[Track] = []
    for raw in (payload or {}).get("tracks") or []:
        if not isinstance(raw, dict):
            continue
        kind = TRACK_OWN if str(raw.get("kind") or "") == TRACK_OWN \
            else TRACK_SYSTEM
        track = Track(
            kind=kind,
            owner_user_id=str(raw.get("owner_user_id") or "")[:100],
            owner_name=str(raw.get("owner_name") or "")[:200],
            device_id=str(raw.get("device_id") or "")[:100],
        )
        for seg in raw.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(seg.get("start") or 0.0)
                end = float(seg.get("end") or start)
            except (TypeError, ValueError):
                continue
            if end < start:
                start, end = end, start
            track.segments.append(Segment(
                start=start, end=end, text=text,
                speaker_label=str(seg.get("speaker") or
                                  seg.get("speaker_label") or "")[:50],
                track_kind=kind, source_device=track.device_id,
            ))
        if track.segments:
            tracks.append(track)
    return tracks


def _overlaps(a: Segment, b: Segment) -> bool:
    """Достаточно ли два сегмента пересекаются, чтобы быть одной репликой."""
    start = max(a.start, b.start) - _CLOCK_SKEW
    end = min(a.end, b.end) + _CLOCK_SKEW
    overlap = end - start
    if overlap <= 0:
        return False
    shorter = min(a.duration, b.duration)
    if shorter <= 0:
        return True
    return (overlap / shorter) >= _OVERLAP_RATIO


def _texts_similar(a: str, b: str) -> bool:
    """Грубая проверка «это одна и та же фраза».

    Точное совпадение недостижимо: два устройства слышат по-разному, и
    распознавание даст разные варианты. Сравниваем набор слов — этого
    хватает, чтобы отличить эхо одной реплики от двух разных."""
    wa = {w for w in a.lower().split() if len(w) > 3}
    wb = {w for w in b.lower().split() if len(w) > 3}
    if not wa or not wb:
        return False
    common = len(wa & wb)
    return common / min(len(wa), len(wb)) >= 0.6


def merge_tracks(tracks: Sequence[Track]) -> List[Segment]:
    """Слить дорожки в один хронологический транскрипт.

    `own`-сегменты имеют приоритет: автор известен точно, звук чище.
    Системные сегменты, которые пересекаются с чужим `own` по времени и
    похожи по тексту, отбрасываются как та же реплика, услышанная со
    стороны, — иначе при записи встречи с двух устройств половина реплик
    задвоилась бы."""
    own: List[Segment] = []
    system: List[Segment] = []

    for track in tracks:
        for seg in track.segments:
            if track.kind == TRACK_OWN:
                seg.author_user_id = track.owner_user_id
                seg.author_name = track.owner_name
                # Владелец устройства говорит в свой микрофон — сомнений в
                # авторстве нет, и это единственное место с полной
                # уверенностью.
                seg.confidence = 1.0
                own.append(seg)
            else:
                system.append(seg)

    kept_system: List[Segment] = []
    dropped = 0
    for seg in system:
        echo = any(
            _overlaps(seg, o)
            and (seg.source_device != o.source_device or not seg.source_device)
            and _texts_similar(seg.text, o.text)
            for o in own
        )
        if echo:
            dropped += 1
            continue
        kept_system.append(seg)

    if dropped:
        logger.info("local capture: отброшено %d дублей системной дорожки "
                    "(та же реплика с другого устройства)", dropped)

    merged = own + kept_system
    merged.sort(key=lambda s: (s.start, s.end))
    return merged


def resolve_names(segments: List[Segment],
                  attendee_names: Optional[List[str]] = None) -> List[Segment]:
    """Проставить имена спикерам системной дорожки.

    Без биометрии: имена вытаскиваются из самого разговора
    (само-представления, обращения по имени), а список участников встречи
    работает фильтром — сужает кандидатов до тех, кто реально был."""
    labelled = [s for s in segments if s.speaker_label and not s.author_name]
    if not labelled:
        return segments

    try:
        from backend.core.transcripts.speaker_resolver import (
            extract_speaker_introductions,
        )
        text = "\n".join(f"{s.speaker_label}: {s.text}" for s in segments
                         if s.speaker_label)
        mapping = extract_speaker_introductions(text) or {}
    except Exception:
        logger.debug("local capture: резолв имён недоступен", exc_info=True)
        return segments

    known = {n.strip().lower() for n in (attendee_names or []) if n}
    for seg in labelled:
        hit = mapping.get(seg.speaker_label)
        if not hit:
            continue
        name = str(hit.get("name") or "").strip()
        if not name:
            continue
        # Участники встречи известны, а имя среди них не значится — скорее
        # ошибка разбора, чем новый человек. Молча приписывать не будем.
        if known and name.lower() not in known:
            continue
        seg.author_name = name
        seg.confidence = float(hit.get("confidence") or 0.5)
    return segments


def to_transcript_text(segments: Sequence[Segment]) -> str:
    """Транскрипт в том же виде, что ждёт остальной пайплайн: «Имя: текст».

    Неопознанный спикер остаётся «Speaker N» — так честнее, чем подставить
    правдоподобное имя: дальше по цепочке это отличие видно, и факт не
    припишут не тому человеку."""
    lines = []
    for s in segments:
        who = s.author_name or s.speaker_label or "Speaker ?"
        lines.append(f"{who}: {s.text}")
    return "\n".join(lines)


def build_session(payload: Dict[str, Any],
                  attendee_names: Optional[List[str]] = None
                  ) -> Dict[str, Any]:
    """Полный разбор: сырые дорожки → транскрипт с авторами и статистикой.

    Статистика возвращается наружу намеренно: доля точно атрибутированных
    реплик — это то, что стоит показывать пользователю, а не прятать.
    Она же объясняет, зачем ставить приложение остальным участникам."""
    tracks = parse_session(payload)
    segments = resolve_names(merge_tracks(tracks), attendee_names)

    total = len(segments)
    certain = sum(1 for s in segments if s.confidence >= 1.0)
    named = sum(1 for s in segments if s.author_name)
    unknown = total - named

    return {
        "status": "success",
        "segments": [s.to_dict() for s in segments],
        "transcript": to_transcript_text(segments),
        "stats": {
            "segments": total,
            "own_tracks": sum(1 for t in tracks if t.kind == TRACK_OWN),
            "attributed_certain": certain,
            "attributed_named": named,
            "unknown_speakers": unknown,
            "attribution_rate": round(named / total, 3) if total else 0.0,
        },
    }
