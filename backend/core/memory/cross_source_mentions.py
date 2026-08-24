# -*- coding: utf-8 -*-
"""
CrossSourceMentions — мета-факт через РАЗНЫЕ ИСТОЧНИКИ
(MEMORY_DESIGN_PRINCIPLES §6 №5).

Зачем: твоё «история про одну сущность в разных встречах/документах/чатах».
Собирающий слой над уже сделанными примитивами:
- `frequency_tracker` — упоминания, но БЕЗ типизации источника.
- `event_log` (№4) — события, но НЕ про «упоминания одной сущности через разное».
- `supersede_store` (№3) — значения, не упоминания.

Этот модуль — единственный, который отвечает на вопрос: «где, когда и как часто
упоминали сущность X — по типам источников?» Один смысловой узел, усиливаемый
ИЗ РАЗНЫХ источников: 7 встреч + 3 документа + 2 чата = 12 упоминаний, и можно
посмотреть полный timeline через все источники.

Связь с шагом №1 (chat_ingestion) и индексацией встреч/документов:
- chat_ingestion пишет беседу в vector → каждый upsert = упоминание (source_type=chat).
- vector_indexer индексирует meeting/decision/task/entity → каждое попадание
  сущности = упоминание (source_type=meeting).
- При подключении (отдельным шагом) source_id ровно тот же, что в graph/vector
  payload (meeting_id, chat doc_id, document id) — швы стабильные.

Дизайн-инварианты (тестируется):
- Идемпотентность по (entity, source_type, source_id) → повторная запись = no-op.
- Никогда не удаляет.
- Per-entity и per-actor изоляция (entity_key несёт user namespace).
- Pure stdlib, optional JSON persistence (форма как у event_log/supersede_store).
- count считается БЫСТРО (counter), timeline — отсортирован.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Канонические типы источников. Любая строка валидна, но эти — рекомендованные,
# чтобы агрегация по типам была согласованной.
SOURCE_MEETING = "meeting"
SOURCE_DOCUMENT = "document"
SOURCE_CHAT = "chat"
SOURCE_KNOWN = {SOURCE_MEETING, SOURCE_DOCUMENT, SOURCE_CHAT}


@dataclass
class Mention:
    """Одно упоминание сущности в источнике. Никогда не удаляется."""
    mention_id: str           # детерминированный: f"{entity}::{source_type}::{source_id}"
    entity: str               # ключ сущности (рекомендуется "user_id::entity_name")
    source_type: str          # meeting | document | chat | <custom>
    source_id: str            # стабильный id источника (meeting_id, doc_id, chat doc_id)
    at: str                   # ISO-дата самого упоминания
    payload: dict = field(default_factory=dict)  # произвольная нагрузка (контекст)
    recorded_at: str = ""     # когда записали (служебное)

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _mention_id(entity: str, source_type: str, source_id: str) -> str:
    """Детерминированный id: повтор того же упоминания → no-op (см. record)."""
    return f"{entity}::{source_type}::{source_id}"


class CrossSourceMentions:
    """In-memory индекс упоминаний сущностей через РАЗНЫЕ типы источников.

    Использование (пример):
        m = CrossSourceMentions(persist_path=…)
        m.record("u1::Проект-Alpha", SOURCE_MEETING, "meeting_42", at="2024-03-01")
        m.record("u1::Проект-Alpha", SOURCE_DOCUMENT, "doc_7",     at="2024-03-05")
        m.record("u1::Проект-Alpha", SOURCE_CHAT,    "chat_xyz",   at="2024-03-10")
        m.sources_summary("u1::Проект-Alpha")
          → {"meeting": 1, "document": 1, "chat": 1}
        m.count("u1::Проект-Alpha") → 3
        m.timeline("u1::Проект-Alpha")
          → отсортированный по at список (cross-source history)
    """

    def __init__(self, persist_path: Optional[str] = None):
        self._mentions: list = []                       # append-only
        self._index: dict = {}                          # mention_id → index в _mentions
        # счётчики для O(1) count: (entity, source_type) и (entity, None)
        self._counts: dict = {}                         # (entity, source_type|None) → int
        self.persist_path = persist_path
        # M4: SQLite-бэкенд за флагом enable_sqlite_stores (см. event_log).
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
                                                 table="mentions")
                    self._db.migrate_from_json(
                        persist_path,
                        lambda d: (d["mention_id"], d.get("entity", ""),
                                   d.get("source_type", ""), d.get("at", ""), d))
            except Exception:
                self._db = None
        if self._db is None and persist_path and os.path.exists(persist_path):
            self._load()

    # -------- запись --------
    def record(self, entity: str, source_type: str, source_id: str, *,
               at: Optional[str] = None, payload: Optional[dict] = None) -> dict:
        """Зафиксировать упоминание сущности в источнике.

        Возвращает {action: "added"|"exists", mention_id, index}.
        Идемпотентность по (entity, source_type, source_id) → повтор = no-op
        (один и тот же ход чата/строка документа не должен считаться дважды).
        """
        if not entity or not source_type or not source_id:
            raise ValueError("record: entity, source_type, source_id обязательны")
        kw = dict(entity=entity, source_type=source_type, source_id=source_id,
                  at=at, payload=payload)
        if self._db is not None:
            at_v = at or _now_iso()
            mid = _mention_id(entity, source_type, source_id)
            m = Mention(mention_id=mid, entity=entity, source_type=source_type,
                        source_id=source_id, at=at_v, payload=payload or {},
                        recorded_at=_now_iso())
            action = self._db.add(mid, entity, source_type, at_v, m.to_dict())
            return {"action": action, "mention_id": mid, "index": None}
        if not self.persist_path:
            return self._apply_record(**kw)
        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._load()                      # M1: видим записи конкурентов
            r = self._apply_record(**kw)
            if r["action"] == "added":
                self._save()
            return r

    def _apply_record(self, entity: str, source_type: str, source_id: str, *,
                      at: Optional[str] = None, payload: Optional[dict] = None) -> dict:
        """Применить к in-memory (БЕЗ save/lock). Идемпотентно по mention_id."""
        at = at or _now_iso()
        payload = payload or {}
        mid = _mention_id(entity, source_type, source_id)
        if mid in self._index:
            return {"action": "exists", "mention_id": mid, "index": self._index[mid]}
        m = Mention(mention_id=mid, entity=entity, source_type=source_type,
                    source_id=source_id, at=at, payload=payload,
                    recorded_at=_now_iso())
        self._index[mid] = len(self._mentions)
        self._mentions.append(m)
        self._counts[(entity, source_type)] = self._counts.get((entity, source_type), 0) + 1
        self._counts[(entity, None)] = self._counts.get((entity, None), 0) + 1
        return {"action": "added", "mention_id": mid, "index": self._index[mid]}

    def record_many(self, items: list, *, source_type: str, source_id: str,
                    at: Optional[str] = None) -> dict:
        """Пакетно записать упоминания из ОДНОГО источника. M1: всё под одним
        локом + reload + один save. items: список entity-ключей. {added, existed}."""
        valid = [e for e in (items or []) if e]

        def _do() -> dict:
            added = existed = 0
            for entity in valid:
                r = self._apply_record(entity, source_type, source_id, at=at)
                if r["action"] == "added":
                    added += 1
                else:
                    existed += 1
            return {"added": added, "existed": existed}

        if self._db is not None:
            at_v = at or _now_iso()
            rows = []
            for entity in valid:
                mid = _mention_id(entity, source_type, source_id)
                m = Mention(mention_id=mid, entity=entity, source_type=source_type,
                            source_id=source_id, at=at_v, payload={},
                            recorded_at=_now_iso())
                rows.append((mid, entity, source_type, at_v, m.to_dict()))
            return self._db.add_many(rows)
        if not self.persist_path:
            return _do()
        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._load()
            res = _do()
            if res["added"]:
                self._save()
            return res

    # -------- запросы --------
    def _match(self, m: Mention, *, entity: Optional[str], source_type: Optional[str],
               since: Optional[datetime], until: Optional[datetime]) -> bool:
        if entity and m.entity != entity:
            return False
        if source_type and m.source_type != source_type:
            return False
        if since or until:
            t = _parse_iso(m.at)
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

    def mentions(self, entity: Optional[str] = None, *, source_type: Optional[str] = None,
                 since: Optional[str] = None, until: Optional[str] = None) -> list:
        """Список упоминаний с опциональными фильтрами (отсортирован по `at`)."""
        s = _parse_iso(since) if since else None
        u = _parse_iso(until) if until else None
        if self._db is not None:
            return self._date_filter(self._db.docs(k1=entity, k2=source_type), s, u)
        matched = [m for m in self._mentions
                   if self._match(m, entity=entity, source_type=source_type, since=s, until=u)]
        matched.sort(key=lambda x: x.at)
        return [x.to_dict() for x in matched]

    def count(self, entity: Optional[str] = None, *, source_type: Optional[str] = None,
              since: Optional[str] = None, until: Optional[str] = None) -> int:
        """Сколько упоминаний подходят под фильтр."""
        if self._db is not None:
            if not since and not until:
                return self._db.count(k1=entity, k2=source_type)
            return len(self.mentions(entity, source_type=source_type,
                                     since=since, until=until))
        # быстрый путь без time-фильтров
        if entity and not since and not until:
            return self._counts.get((entity, source_type), 0)
        # медленный путь — нужен фильтр по дате
        s = _parse_iso(since) if since else None
        u = _parse_iso(until) if until else None
        return sum(1 for m in self._mentions
                   if self._match(m, entity=entity, source_type=source_type, since=s, until=u))

    def timeline(self, entity: str, *, since: Optional[str] = None,
                 until: Optional[str] = None) -> list:
        """Полная хронология упоминаний сущности через ВСЕ источники (для UI/recall).
        Это и есть твоё «история про одну сущность в разных встречах/документах/чатах»."""
        return self.mentions(entity, since=since, until=until)

    def sources_summary(self, entity: str) -> dict:
        """Распределение упоминаний по типам источников: {meeting: N, document: M, chat: K}.
        Это «сколько раз в каком ИСТОЧНИКЕ говорили о сущности»."""
        if self._db is not None:
            return self._db.group_counts("k2", k1=entity)
        out: dict = {}
        for (ent, src), n in self._counts.items():
            if ent == entity and src is not None:
                out[src] = n
        return out

    def top_entities(self, k: int = 10, *, source_type: Optional[str] = None) -> list:
        """Сущности с наибольшим числом упоминаний (опционально в одном типе источника)."""
        if self._db is not None:
            counts = self._db.group_counts("k1", k2=source_type)
            return Counter(counts).most_common(k)
        c: Counter = Counter()
        for (ent, src), n in self._counts.items():
            if src is None:
                continue
            if source_type and src != source_type:
                continue
            c[ent] += n
        return c.most_common(k)

    # -------- persistence --------
    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            # M2/M5-fix: атомарная запись + fsync вместо рваного open("w")
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self.persist_path,
                              [m.to_dict() for m in self._mentions])
        except Exception:
            pass

    def _load(self) -> None:
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                arr = json.load(f)
            self._mentions = []
            self._index = {}
            self._counts = {}
            for d in arr:
                m = Mention(**d)
                self._index[m.mention_id] = len(self._mentions)
                self._mentions.append(m)
                self._counts[(m.entity, m.source_type)] = self._counts.get((m.entity, m.source_type), 0) + 1
                self._counts[(m.entity, None)] = self._counts.get((m.entity, None), 0) + 1
        except Exception:
            self._mentions = []
            self._index = {}
            self._counts = {}
