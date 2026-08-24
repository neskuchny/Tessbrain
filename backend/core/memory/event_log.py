# -*- coding: utf-8 -*-
"""
EventLog — мета-факт через ИСТОРИЮ ДАТИРОВАННЫХ СОБЫТИЙ
(MEMORY_DESIGN_PRINCIPLES §3a, роадмап §6 №4).

Зачем (диагноз MEM0_STUDY §«5 сессий»): `frequency_tracker` считает УПОМИНАНИЯ
сущности (mentions), а счётные вопросы требуют числа доменных СОБЫТИЙ
(посещений/ревизий/обсуждений). 5 посещений группы поддержки ≠ 20 упоминаний
группы в тексте. Нужен слой, где **событие — first-class объект** с датой и типом.

Связь со supersede_store (роадмап №3):
- supersede: «значение факта менялось» (бюджет 400 → 500).
- event_log: «событие случилось» (посетил сессию 1-го / 15-го / 22-го).
Это два разных вопроса; этот модуль закрывает второй.

Связь с BWEDecisionEvent:
- BWE моделирует УЗКИЕ доменные события (решения, исходы) с типами.
- event_log — общий примитив для ЛЮБОГО кастомного типа события (ингрес чата
  «закрыл задачу X», ревизия документа, посещение, упоминание клиента).
- Совместимая форма: actor / kind / at / payload — нет конфликта.

Дизайн-инварианты (тестируется):
- Идемпотентность по event_id (повтор записи того же события ≠ дубль).
- Никогда не удаляет (только append; для коррекций → запись «отменено» отдельным
  событием, как двойная бухгалтерия).
- Per-user через `actor` в ключе/фильтре.
- Pure stdlib, in-memory + JSON-persistable (та же форма, что у supersede_store).
- O(N) счёт окей для размеров «события одного пользователя за период».
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class DomainEvent:
    """Одно датированное событие. Никогда не удаляется."""
    event_id: str                     # идемпотентный id (хеш actor+kind+at+payload)
    actor: str                        # кто/чьё (обычно user_id)
    kind: str                         # тип события («attended_session», «revised_doc»)
    at: str                           # ISO-дата события (когда оно случилось)
    source: Optional[str] = None      # откуда узнали (meeting_id, chat, doc)
    payload: dict = field(default_factory=dict)  # произвольная нагрузка
    recorded_at: str = ""             # когда записали (служебное)

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    """Парсим ISO-дату в aware-datetime (UTC если без TZ). None если не получилось."""
    if not s:
        return None
    try:
        # допускаем 'Z' (Python <3.11 в isoformat читает только +00:00)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _stable_event_id(actor: str, kind: str, at: str, payload: dict) -> str:
    """Детерминированный id: одинаковый вход → одинаковый id (идемпотентность)."""
    canon = json.dumps({"a": actor, "k": kind, "t": at, "p": payload or {}},
                       sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(canon.encode("utf-8", errors="ignore")).hexdigest()[:16]


class EventLog:
    """In-memory лог доменных событий с опциональной JSON-персистенцией.

    Использование (пример):
        log = EventLog(persist_path=…)
        log.record(actor="u1", kind="attended_support_group", at="2024-02-15")
        log.record(actor="u1", kind="attended_support_group", at="2024-02-22")
        log.count(actor="u1", kind="attended_support_group")  # → 2
    """

    def __init__(self, persist_path: Optional[str] = None):
        self._events: list = []          # list[DomainEvent], append-only
        self._index: dict = {}           # event_id → index (для идемпотентности)
        self.persist_path = persist_path
        # M4: SQLite-бэкенд (за флагом enable_sqlite_stores) — O(1) запись
        # вместо пересериализации всего JSON; конкурентность через WAL,
        # file_lock не нужен. Флаг OFF → прежний JSON-путь байт-в-байт.
        self._db = None
        if persist_path:
            try:
                from backend.core.store.sqlite_records import (
                    SqliteRecordStore,
                    sqlite_path_for,
                    sqlite_stores_enabled,
                )
                if sqlite_stores_enabled():
                    self._db = SqliteRecordStore(sqlite_path_for(persist_path),
                                                 table="events")
                    self._db.migrate_from_json(
                        persist_path,
                        lambda d: (d["event_id"], d.get("actor", ""),
                                   d.get("kind", ""), d.get("at", ""), d))
            except Exception:
                self._db = None  # деградация в JSON-режим
        if self._db is None and persist_path and os.path.exists(persist_path):
            self._load()

    # -------- запись --------
    def _apply_record(self, *, actor: str, kind: str, at: Optional[str] = None,
                      source: Optional[str] = None, payload: Optional[dict] = None,
                      event_id: Optional[str] = None) -> dict:
        """Применить запись к in-memory (БЕЗ save/lock). Идемпотентно по event_id."""
        at = at or _now_iso()
        payload = payload or {}
        eid = event_id or _stable_event_id(actor, kind, at, payload)
        if eid in self._index:
            return {"action": "exists", "event_id": eid, "index": self._index[eid]}
        ev = DomainEvent(event_id=eid, actor=actor, kind=kind, at=at,
                         source=source, payload=payload, recorded_at=_now_iso())
        self._index[eid] = len(self._events)
        self._events.append(ev)
        return {"action": "added", "event_id": eid, "index": self._index[eid]}

    def record(self, *, actor: str, kind: str, at: Optional[str] = None,
               source: Optional[str] = None, payload: Optional[dict] = None,
               event_id: Optional[str] = None) -> dict:
        """Зафиксировать событие. Возвращает {action, event_id, index}.

        M1: при persist_path запись идёт ПОД ЛОКОМ с reload — конкурентный
        писатель (worker+api на одном томе) не теряет данные.
        action: "added" | "exists" (идемпотентность по event_id).
        """
        if not actor or not kind:
            raise ValueError("EventLog.record: actor и kind обязательны")
        kw = dict(actor=actor, kind=kind, at=at, source=source,
                  payload=payload, event_id=event_id)
        if self._db is not None:
            at_v = at or _now_iso()
            payload_v = payload or {}
            eid = event_id or _stable_event_id(actor, kind, at_v, payload_v)
            ev = DomainEvent(event_id=eid, actor=actor, kind=kind, at=at_v,
                             source=source, payload=payload_v,
                             recorded_at=_now_iso())
            action = self._db.add(eid, actor, kind, at_v, ev.to_dict())
            return {"action": action, "event_id": eid, "index": None}
        if not self.persist_path:
            return self._apply_record(**kw)
        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._load()                 # видим записи конкурентов
            r = self._apply_record(**kw)
            if r["action"] == "added":
                self._save()             # atomic (без вложенного лока)
            return r

    def record_many(self, items: list) -> dict:
        """Пакетно записать события (каждый item — dict с kwargs для record).
        M1: всё под ОДНИМ локом + reload + один save в конце. {added, existed}."""
        valid = [it for it in (items or []) if isinstance(it, dict)]
        if self._db is not None:
            rows = []
            for it in valid:
                try:
                    actor = it["actor"]; kind = it["kind"]
                    at_v = it.get("at") or _now_iso()
                    payload_v = it.get("payload") or {}
                    eid = it.get("event_id") or _stable_event_id(actor, kind, at_v, payload_v)
                    ev = DomainEvent(event_id=eid, actor=actor, kind=kind, at=at_v,
                                     source=it.get("source"), payload=payload_v,
                                     recorded_at=_now_iso())
                    rows.append((eid, actor, kind, at_v, ev.to_dict()))
                except Exception:
                    continue
            return self._db.add_many(rows)
        if not self.persist_path:
            added = existed = 0
            for it in valid:
                try:
                    r = self._apply_record(**it)
                except Exception:
                    continue
                added += 1 if r["action"] == "added" else 0
                existed += 1 if r["action"] == "exists" else 0
            return {"added": added, "existed": existed}

        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._load()
            added = existed = 0
            for it in valid:
                try:
                    r = self._apply_record(**it)
                except Exception:
                    continue
                added += 1 if r["action"] == "added" else 0
                existed += 1 if r["action"] == "exists" else 0
            if added:
                self._save()
            return {"added": added, "existed": existed}

    # -------- запросы --------
    def _match(self, ev: DomainEvent, *, actor: Optional[str], kind: Optional[str],
               since: Optional[datetime], until: Optional[datetime]) -> bool:
        if actor and ev.actor != actor:
            return False
        if kind and ev.kind != kind:
            return False
        if since or until:
            t = _parse_iso(ev.at)
            if t is None:
                return False
            if since and t < since:
                return False
            if until and t > until:
                return False
        return True

    @staticmethod
    def _date_filter(docs: list, since: Optional[datetime],
                     until: Optional[datetime]) -> list:
        """Фильтр по диапазону дат для dict-записей (семантика как в _match:
        непарсящийся `at` исключается, когда задан диапазон)."""
        if not since and not until:
            return docs
        out = []
        for d in docs:
            t = _parse_iso(d.get("at", ""))
            if t is None:
                continue
            if since and t < since:
                continue
            if until and t > until:
                continue
            out.append(d)
        return out

    def events(self, *, actor: Optional[str] = None, kind: Optional[str] = None,
               since: Optional[str] = None, until: Optional[str] = None) -> list:
        """Список событий с опциональным фильтром (отсортирован по `at`)."""
        s = _parse_iso(since) if since else None
        u = _parse_iso(until) if until else None
        if self._db is not None:
            return self._date_filter(self._db.docs(k1=actor, k2=kind), s, u)
        matched = [ev for ev in self._events
                   if self._match(ev, actor=actor, kind=kind, since=s, until=u)]
        matched.sort(key=lambda e: e.at)
        return [e.to_dict() for e in matched]

    def count(self, *, actor: Optional[str] = None, kind: Optional[str] = None,
              since: Optional[str] = None, until: Optional[str] = None) -> int:
        """Сколько событий подходят под фильтр. Это и есть «сколько раз X»."""
        if self._db is not None:
            if not since and not until:
                return self._db.count(k1=actor, k2=kind)   # SQL COUNT, O(индекс)
            return len(self.events(actor=actor, kind=kind, since=since, until=until))
        s = _parse_iso(since) if since else None
        u = _parse_iso(until) if until else None
        return sum(1 for ev in self._events
                   if self._match(ev, actor=actor, kind=kind, since=s, until=u))

    def kinds(self, *, actor: Optional[str] = None) -> dict:
        """Распределение по типам событий (для актора, если задан): {kind: count}."""
        if self._db is not None:
            return self._db.group_counts("k2", k1=actor)
        out: dict = {}
        for ev in self._events:
            if actor and ev.actor != actor:
                continue
            out[ev.kind] = out.get(ev.kind, 0) + 1
        return out

    # -------- persistence --------
    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            # M2/M5-fix: атомарная запись + fsync вместо рваного open("w")
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self.persist_path,
                              [ev.to_dict() for ev in self._events])
        except Exception:
            pass

    def _load(self) -> None:
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                arr = json.load(f)
            self._events = []
            self._index = {}
            for d in arr:
                ev = DomainEvent(**d)
                self._index[ev.event_id] = len(self._events)
                self._events.append(ev)
        except Exception:
            self._events = []
            self._index = {}
