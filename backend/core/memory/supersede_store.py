# -*- coding: utf-8 -*-
"""
Supersede-стор фактов — недеструктивное обновление знаний
(MEMORY_DESIGN_PRINCIPLES.md §3, §7; роадмап §6 №3).

Зачем: Mem0-A.U.D.N разрушительен (DELETE/UPDATE стирают старое); по решению §7 —
не берём. Берём `supersede`: хранить ВСЕ датированные версии факта, помечать
текущую, никогда не удалять. Это и есть мета-факт с историей (§3).

Что хранит: для каждого `fact_key` — упорядоченный по времени список версий
(`value`, `as_of`, `source`, `superseded_at`, `superseded_by`). Запросы:
- `current(key)` — последняя НЕ помеченная как superseded;
- `history(key)` — вся цепочка (для счёта/прослеживания);
- `record(key, value, ...)` — добавить новую версию; при изменении значения
  предыдущая помечается `superseded_at` + `superseded_by` указывает на новую.

Форма версии совместима с BWE `evolution_log` (список датированных строк):
конвертер `to_evolution_log` (для зеркалирования в BWE-паттернах).

Дизайн-инварианты (тестируется):
- Никогда не удаляет историю (даже после нескольких изменений).
- Идемпотентность по содержимому: повтор того же значения → НЕ создаёт новую
  версию (мет-факт «5 раз слышали то же» → счёт через `len(mentions)`, а не дубли).
- Per-user изоляция через user_id в ключе.
- Pure stdlib (никаких deps), in-memory + JSON-persistable.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class FactVersion:
    """Одна датированная версия факта. Никогда не удаляется."""
    value: str                              # значение факта (текст)
    as_of: str                              # ISO-дата, когда зафиксировано
    source: Optional[str] = None            # откуда узнали (meeting_id, chat, doc)
    superseded_at: Optional[str] = None     # ISO-дата, когда заменено новой версией
    superseded_by: Optional[int] = None     # индекс заменившей версии (в history)
    mentions: int = 1                       # сколько раз слышали ровно это значение

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FactRecord:
    """История версий одного факта. `history` — список в порядке появления."""
    key: str
    history: list = field(default_factory=list)  # list[FactVersion]

    def current_version(self) -> Optional[FactVersion]:
        """Последняя версия, которая НЕ помечена superseded."""
        for v in reversed(self.history):
            if v.superseded_at is None:
                return v
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(s: str) -> str:
    """Нормализация значения для сравнения (regex-free, stdlib).
    Идемпотентность: одинаковые-по-смыслу значения схлопываются в `mentions`."""
    return " ".join((s or "").strip().lower().split())


class SupersedeStore:
    """In-memory стор недеструктивной истории фактов.

    Ключ — произвольная строка (рекомендуется `user_id::namespace::topic`).
    Опционально подгружается/сохраняется из/в JSON (path задаётся в конструкторе).
    """

    def __init__(self, persist_path: Optional[str] = None):
        self._facts: dict = {}            # key → FactRecord
        self.persist_path = persist_path
        # M4 (вторая волна): SQLite-KV за флагом enable_sqlite_stores —
        # BEGIN IMMEDIATE вместо flock. OFF → прежний JSON байт-в-байт.
        self._db = None
        self._txn = None
        if persist_path:
            try:
                from backend.core.store.sqlite_records import (
                    SqliteKVStore,
                    sqlite_path_for,
                    sqlite_stores_enabled,
                )
                if sqlite_stores_enabled():
                    self._db = SqliteKVStore(sqlite_path_for(persist_path),
                                             table="facts")
                    self._db.migrate_from_json(persist_path, lambda root: root)
            except Exception:
                self._db = None
        if self._db is not None:
            self._load()
        elif persist_path and os.path.exists(persist_path):
            self._load()

    # -------- запросы --------
    def current(self, key: str) -> Optional[str]:
        rec = self._facts.get(key)
        if rec is None:
            return None
        cur = rec.current_version()
        return cur.value if cur else None

    def history(self, key: str) -> list:
        """Полная упорядоченная история (для счёта/прослеживания). Список dict."""
        rec = self._facts.get(key)
        return [v.to_dict() for v in rec.history] if rec else []

    def keys(self, *, prefix: Optional[str] = None, contains: Optional[str] = None) -> list:
        """Все ключи фактов (опц. фильтр по префиксу/подстроке, нормализованно).

        Нужен read-стороне (entity_dossier): «какие факты относятся к сущности X».
        Аддитивно, ничего не меняет в записи.
        """
        out = []
        p = _normalize(prefix) if prefix else None
        c = _normalize(contains) if contains else None
        for k in self._facts:
            kn = _normalize(k)
            if p and not kn.startswith(p):
                continue
            if c and c not in kn:
                continue
            out.append(k)
        return sorted(out)

    def count_distinct_values(self, key: str) -> int:
        """Сколько РАЗНЫХ значений было у факта (для «сколько раз менялось»)."""
        rec = self._facts.get(key)
        return len(rec.history) if rec else 0

    def total_mentions(self, key: str) -> int:
        """Сколько раз факт упоминался суммарно (с учётом повторов того же значения)."""
        rec = self._facts.get(key)
        return sum(v.mentions for v in rec.history) if rec else 0

    # -------- запись --------
    def record(self, key: str, value: str, *, source: Optional[str] = None,
               as_of: Optional[str] = None) -> dict:
        """Зафиксировать новое наблюдение факта.

        Возвращает dict с полями:
          - action: "added" (новый факт) | "mention" (повтор того же значения)
                    | "superseded" (новое значение, прошлое помечено superseded)
          - version_index: индекс затронутой версии в history
          - current: текущее значение после операции
        """
        if not key or value is None:
            raise ValueError("supersede.record: key и value обязательны")
        if not self.persist_path:
            return self._apply_record(key, value, source=source, as_of=as_of)
        if self._db is not None:
            # M4: эксклюзивная транзакция вместо flock (reload → apply → снапшот)
            with self._db.exclusive() as txn:
                self._txn = txn
                try:
                    self._facts = {}
                    self._load_docs(txn.load_all())
                    r = self._apply_record(key, value, source=source, as_of=as_of)
                    self._save()
                    return r
                finally:
                    self._txn = None
        # M1: запись под локом + чистый reload (видим версии конкурентов)
        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._facts = {}
            self._load()
            r = self._apply_record(key, value, source=source, as_of=as_of)
            self._save()
            return r

    def record_many(self, items) -> list:
        """Пакетная запись: один reload → все версии → один save.

        Нужна для разбора встречи: там за раз фиксируется история сразу по
        десяткам фактов (роли людей, статусы проектов, сроки задач). Через
        record() это дало бы столько же циклов «взять лок, перечитать файл,
        записать файл» — на встрече с 30 участниками стор переписывался бы
        30 раз подряд.

        items: последовательность dict с ключами key, value и (необязательно)
        source, as_of. Записи без key/value пропускаются молча — на ингесте
        пустое поле это норма, а не повод уронить разбор встречи.

        Возвращает список результатов _apply_record для записанных фактов.
        """
        prepared = [
            (str(it.get("key")), str(it.get("value")),
             it.get("source"), it.get("as_of"))
            for it in (items or [])
            if it and it.get("key") and it.get("value") not in (None, "")
        ]
        if not prepared:
            return []

        def _apply_all() -> list:
            return [
                self._apply_record(k, v, source=src, as_of=ts)
                for k, v, src, ts in prepared
            ]

        if not self.persist_path:
            return _apply_all()
        if self._db is not None:
            with self._db.exclusive() as txn:
                self._txn = txn
                try:
                    self._facts = {}
                    self._load_docs(txn.load_all())
                    out = _apply_all()
                    self._save()
                    return out
                finally:
                    self._txn = None
        from backend.core.store.tenant_io import file_lock
        with file_lock(self.persist_path):
            self._facts = {}
            self._load()
            out = _apply_all()
            self._save()
            return out

    def _apply_record(self, key: str, value: str, *, source: Optional[str] = None,
                      as_of: Optional[str] = None) -> dict:
        """Применить версию к in-memory (БЕЗ save/lock)."""
        as_of = as_of or _now_iso()
        rec = self._facts.setdefault(key, FactRecord(key=key))

        cur = rec.current_version()
        new_norm = _normalize(value)
        if cur is not None and _normalize(cur.value) == new_norm:
            # повтор того же значения → не создаём новую версию, только усиливаем
            cur.mentions += 1
            action = "mention"
            idx = len(rec.history) - 1 - list(reversed(rec.history)).index(cur)
        else:
            # новое значение — supersede предыдущей текущей (если была)
            if cur is not None:
                cur.superseded_at = as_of
                cur.superseded_by = len(rec.history)  # индекс СЛЕДУЮЩЕЙ записи (это новая)
            rec.history.append(FactVersion(value=value, as_of=as_of, source=source))
            action = "superseded" if cur is not None else "added"
            idx = len(rec.history) - 1

        return {"action": action, "version_index": idx, "current": value}

    # -------- BWE-совместимость --------
    def to_evolution_log(self, key: str) -> list:
        """Конвертер в BWE `evolution_log`-формат (список строк
        "v{n} @ {date}: {head}") — для зеркалирования в pattern_encoder.

        Совместимо с `bwe_models.evolution_log` (Column JSON, list[str]).
        """
        rec = self._facts.get(key)
        if not rec:
            return []
        out = []
        for n, v in enumerate(rec.history, start=1):
            date = (v.as_of or "")[:10]
            head = (v.value or "").splitlines()[0][:80]
            out.append(f"v{n} @ {date}: {head}")
        return out

    # -------- persistence --------
    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            data = {k: {"key": v.key, "history": [h.to_dict() for h in v.history]}
                    for k, v in self._facts.items()}
            if self._db is not None:
                if self._txn is not None:        # внутри exclusive-транзакции
                    self._txn.replace_all(data)
                else:
                    with self._db.exclusive() as txn:
                        txn.replace_all(data)
                return
            # M2/M5-fix: атомарная запись + fsync вместо рваного open("w")
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self.persist_path, data)
        except Exception:
            # persistence — best-effort; in-memory сохраняется
            pass

    def _load_docs(self, data: dict) -> None:
        for k, payload in (data or {}).items():
            try:
                rec = FactRecord(key=payload.get("key", k))
                for h in payload.get("history", []):
                    rec.history.append(FactVersion(**h))
                self._facts[k] = rec
            except Exception:
                continue

    def _load(self) -> None:
        if self._db is not None:
            try:
                self._load_docs(self._db.load_all())
            except Exception:
                self._facts = {}
            return
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                data = json.load(f)
            for k, payload in data.items():
                rec = FactRecord(key=payload.get("key", k))
                for h in payload.get("history", []):
                    rec.history.append(FactVersion(**h))
                self._facts[k] = rec
        except Exception:
            self._facts = {}
