# -*- coding: utf-8 -*-
"""
PlanMemory — память «КАК мы делаем такие задачи» для композитора
(docs/CAPABILITY_READINESS.md; продолжение agent_memory PROCEDURAL).

ЗАЧЕМ. Задачи повторяются: «exit-интервью по Мише» сегодня → «exit-интервью по
Олегу» через месяц. Система должна ЗАПОМНИТЬ, как делала (схему артефакта,
нужна ли оценка), и переиспользовать: (а) консистентность — повторная задача
делается ТЕМ ЖЕ способом; (б) схемы можно курировать руками — память умнеет;
(в) меньше LLM-планирования.

ПОЧЕМУ НЕ agent_memory: он есть (PROCEDURAL) и включён, но его recall — простой
текстовый поиск по свободному тексту; СТРУКТУРНЫЕ планы (схема как данные) он
не хранит и не матчит. Тот же урок, что с event_log vs «события в тексте»:
структурной вещи — структурный стор. agent_memory остаётся для свободных
навыков-заметок.

СКЕЛЕТ (без LLM): нормализация запроса в «шаблон» — токены в нижнем регистре,
СТОП-СЛОВА и ИМЕНА СУЩНОСТЕЙ выброшены (Titlecase не в начале фразы — дёшево и
детерминированно: «резюме по Пете» и «резюме по Маше» → один шаблон); recall —
Jaccard-похожесть шаблонов. Исходы (approve/reject из оценки) копятся в stats.

Per-user JSON (tenant_paths), как остальные примитивы. Чистый stdlib.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from typing import List, Optional

_STOP = {
    "и", "а", "но", "по", "для", "про", "о", "об", "с", "со", "в", "во", "на",
    "к", "ко", "из", "у", "от", "до", "за", "под", "над", "при", "без", "же",
    "наш", "наша", "наше", "наши", "мне", "меня", "мой", "моя", "это", "этот",
    "его", "её", "ее", "их", "ему", "ей", "им", "нам",
    "the", "a", "an", "for", "to", "of", "in", "on", "with", "and", "or", "my", "our",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_request(request: str) -> List[str]:
    """Запрос → токены-шаблон: нижний регистр, без стоп-слов и без имён
    сущностей (Titlecase не в начале фразы). Детерминированно, без LLM."""
    words = re.findall(r"[\w\-]+", (request or ""), re.UNICODE)
    out = []
    for i, w in enumerate(words):
        if i > 0 and w[:1].isupper() and not w.isupper():
            continue  # вероятное имя сущности (Петя, Альфа, СтройХолдинг — не шаблон)
        lw = w.lower()
        if lw in _STOP or len(lw) < 2:
            continue
        out.append(lw)
    return sorted(set(out))


def _similarity(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class PlanRecord:
    """Запомненный способ решения типа задач. template — данные плана
    (schema_name / custom_schema / evaluate_result), НЕ сущности."""
    record_id: str
    request_example: str            # первый запрос-образец (для человека)
    tokens: List[str]               # нормализованный шаблон запроса
    template: dict                  # {"schema_name", "custom_schema", "evaluate_result"}
    created_at: str = ""
    last_used_at: str = ""
    uses: int = 1
    outcomes: dict = field(default_factory=lambda: {"approve": 0, "review": 0, "reject": 0})

    def to_dict(self) -> dict:
        return asdict(self)


def _template_fingerprint(template: dict) -> str:
    raw = json.dumps(template, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class PlanMemory:
    """Структурная память планов. In-memory + опц. JSON-персистенс."""

    def __init__(self, persist_path: Optional[str] = None):
        self._path = persist_path
        self._records: dict = {}      # record_id → PlanRecord
        # M4 (вторая волна): SQLite-KV за флагом enable_sqlite_stores —
        # BEGIN IMMEDIATE вместо flock, снапшот в транзакции. OFF → JSON.
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
                                             table="plans")
                    self._db.migrate_from_json(
                        persist_path,
                        lambda root: {r["record_id"]: r
                                      for r in (root.get("records") or [])
                                      if r.get("record_id")})
            except Exception:
                self._db = None
        if persist_path:
            self._load()

    # -- запись ---------------------------------------------------------
    def _locked(self, fn):
        """M1: выполнить мутацию ПОД ЛОКОМ с чистым reload (видим записи
        конкурентов). Без persist — просто выполнить (in-memory/тесты).
        SQLite-режим: эксклюзивная транзакция вместо flock."""
        if not self._path:
            return fn()
        if self._db is not None:
            with self._db.exclusive() as txn:
                self._txn = txn
                try:
                    self._records = {}
                    self._load_docs(txn.load_all())
                    return fn()
                finally:
                    self._txn = None
        from backend.core.store.tenant_io import file_lock
        with file_lock(self._path):
            self._records = {}
            self._load()
            return fn()

    def _load_docs(self, docs: dict) -> None:
        for rid, rd in (docs or {}).items():
            try:
                self._records[rid] = PlanRecord(**rd)
            except Exception:
                continue

    def remember(self, request: str, template: dict) -> dict:
        """Запомнить «такие запросы делаем так». Повтор того же шаблона на
        похожем запросе → reinforce (uses+1), не дубль. M1: под локом."""
        return self._locked(lambda: self._remember_impl(request, template))

    def _remember_impl(self, request: str, template: dict) -> dict:
        tokens = normalize_request(request)
        fp = _template_fingerprint(template)
        for rec in self._records.values():
            if (_template_fingerprint(rec.template) == fp
                    and _similarity(tokens, rec.tokens) >= 0.5):
                rec.uses += 1
                rec.last_used_at = _now_iso()
                # шаблон запроса обогащается пересечением — устойчивее к шуму
                rec.tokens = sorted(set(rec.tokens) | set(tokens))
                self._save()
                return {"action": "reinforced", "record_id": rec.record_id}
        rid = f"plan::{fp}::{len(self._records)}"
        self._records[rid] = PlanRecord(
            record_id=rid, request_example=request, tokens=tokens,
            template=dict(template), created_at=_now_iso(), last_used_at=_now_iso())
        self._save()
        return {"action": "created", "record_id": rid}

    def record_outcome(self, record_id: str, decision: str) -> bool:
        """Исход оценки артефакта (approve/review/reject). M1: под локом."""
        def _impl():
            rec = self._records.get(record_id)
            if not rec or decision not in rec.outcomes:
                return False
            rec.outcomes[decision] += 1
            self._save()
            return True
        return self._locked(_impl)

    # -- чтение ---------------------------------------------------------
    def recall(self, request: str, *, min_similarity: float = 0.4) -> Optional[dict]:
        """Самый похожий запомненный способ (или None). Решение — по
        детерминированной похожести шаблонов, не по LLM."""
        tokens = normalize_request(request)
        best, best_sim = None, 0.0
        for rec in self._records.values():
            sim = _similarity(tokens, rec.tokens)
            if sim > best_sim:
                best, best_sim = rec, sim
        if best is None or best_sim < min_similarity:
            return None
        return {"record_id": best.record_id, "template": dict(best.template),
                "similarity": round(best_sim, 4), "uses": best.uses,
                "outcomes": dict(best.outcomes),
                "request_example": best.request_example}

    def entries(self) -> List[dict]:
        return [r.to_dict() for r in self._records.values()]

    # -- персистенс -----------------------------------------------------
    def _save(self) -> None:
        if not self._path:
            return
        if self._db is not None:
            docs = {rid: r.to_dict() for rid, r in self._records.items()}
            try:
                if self._txn is not None:        # внутри exclusive-транзакции
                    self._txn.replace_all(docs)
                else:                            # вне (ред. путь) — своя транзакция
                    with self._db.exclusive() as txn:
                        txn.replace_all(docs)
            except Exception:
                pass
            return
        # M5-fix: общий хелпер добавляет fsync файла и директории
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(self._path,
                          {"records": [r.to_dict() for r in self._records.values()]})

    def _load(self) -> None:
        if self._db is not None:
            try:
                self._load_docs(self._db.load_all())
            except Exception:
                self._records = {}
            return
        if not (self._path and os.path.exists(self._path)):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            for rd in data.get("records", []):
                rec = PlanRecord(**rd)
                self._records[rec.record_id] = rec
        except Exception:
            self._records = {}
