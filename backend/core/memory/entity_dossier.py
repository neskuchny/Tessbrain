# -*- coding: utf-8 -*-
"""
Universal Entity Dossier — «собери ВСЁ о сущности X» одним вызовом
(docs/CAPABILITY_READINESS.md, универсальная операция №1).

ЗАЧЕМ. Целевые задачи продукта (резюме сотрудника, отчёт по команде, КП для
клиента, подготовка к переговорам, …) — это ПРИМЕРЫ одного класса: агент
должен собрать полный контекст о произвольной сущности из ВСЕХ слоёв памяти
компании. Строить пайплайн под каждый пример не масштабируется — нужен один
универсальный примитив «досье», который дальше потребляют генерация артефактов
и композитор.

ПРИНЦИПЫ (как у остальных примитивов памяти):
- СКЕЛЕТ В КОДЕ: сборка детерминированная, LLM не участвует. Повествование —
  отдельный шаг (наш analysis-режим получает to_text()).
- ЧЕСТНОСТЬ ПОКРЫТИЯ (§8): досье явно говорит, какие источники дали данные,
  какие пусты, какие упали — completeness() метрика измерима без LLM.
- BEST-EFFORT: упавший источник = coverage 'error', не роняет сборку.
- РАСШИРЯЕМОСТЬ: providers — словарь callables; стандартные фабрики поверх
  per-user сторов (mentions/events/facts) + любые внешние (граф, persona,
  снапшоты) инъектируются без правки ядра.

Чистый stdlib (тестируется изолированно, без инфры).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DossierSection:
    """Один источник досье: что нашли и в каком состоянии источник."""
    source: str                  # 'mentions' | 'events' | 'facts' | <custom>
    title: str                   # человекочитаемый заголовок
    coverage: str                # 'ok' | 'empty' | 'error'
    items: tuple = ()            # tuple[dict] — структурные факты (с датами где есть)
    error: Optional[str] = None  # кратко, если coverage == 'error'


@dataclass(frozen=True)
class EntityDossier:
    """Собранное досье. Иммутабельно; повествование — забота вызывающего."""
    entity: str
    entity_type: str             # 'person'|'team'|'client'|'project'|...|'unknown'
    generated_at: str
    sections: tuple = ()         # tuple[DossierSection]

    def coverage(self) -> Dict[str, str]:
        """source → 'ok'|'empty'|'error' — честная карта покрытия."""
        return {s.source: s.coverage for s in self.sections}

    def completeness(self) -> float:
        """Доля источников с данными (ok) — детерминированная метрика полноты."""
        if not self.sections:
            return 0.0
        ok = sum(1 for s in self.sections if s.coverage == "ok")
        return round(ok / len(self.sections), 4)

    def items_count(self) -> int:
        return sum(len(s.items) for s in self.sections)

    def to_text(self, *, max_items_per_section: int = 25) -> str:
        """Текст досье для LLM-контекста (наш analysis-режим): секции с датами,
        ЯВНАЯ карта покрытия в шапке (модель видит, чего не хватает, — §8)."""
        cov = ", ".join(f"{s.source}={s.coverage}({len(s.items)})" for s in self.sections)
        lines = [f"=== ДОСЬЕ: {self.entity} (тип: {self.entity_type}) ===",
                 f"Покрытие источников: {cov or 'нет источников'}"]
        for s in self.sections:
            if s.coverage != "ok":
                continue
            lines.append(f"\n--- {s.title} ({len(s.items)}) ---")
            for it in s.items[:max_items_per_section]:
                at = it.get("at") or it.get("as_of") or ""
                date = f"[{at}] " if at else ""
                body = it.get("text") or it.get("value") or it.get("kind") or str(
                    {k: v for k, v in it.items() if k not in ("at", "as_of")})
                lines.append(f"{date}{body}")
            if len(s.items) > max_items_per_section:
                lines.append(f"(+ ещё {len(s.items) - max_items_per_section})")
        missing = [s.source for s in self.sections if s.coverage != "ok"]
        if missing:
            lines.append(f"\nНЕТ ДАННЫХ из: {', '.join(missing)} — учитывай при выводах.")
        return "\n".join(lines)


# Provider: callable() -> list[dict]. Пустой список = 'empty', исключение = 'error'.
Provider = Callable[[], List[dict]]

_TITLES = {
    "mentions": "Упоминания по источникам (встречи/документы/чаты)",
    "events": "Датированные события (решения/задачи)",
    "facts": "Факты и история их версий",
}


def build_dossier(entity: str, providers: Dict[str, Provider], *,
                  entity_type: str = "unknown") -> EntityDossier:
    """Собрать досье сущности из providers. Детерминированно, best-effort."""
    sections = []
    for source, fn in providers.items():
        title = _TITLES.get(source, source)
        try:
            items = list(fn() or [])
            coverage = "ok" if items else "empty"
            sections.append(DossierSection(source=source, title=title,
                                           coverage=coverage, items=tuple(items)))
        except Exception as e:  # best-effort по контракту
            sections.append(DossierSection(source=source, title=title,
                                           coverage="error", error=str(e)[:200]))
    return EntityDossier(entity=entity, entity_type=entity_type,
                         generated_at=_now_iso(), sections=tuple(sections))


# ---------------------------------------------------------------------------
# Стандартные фабрики providers поверх per-user сторов памяти.
# Инстансы сторов ИНЪЕКТИРУЮТСЯ (тесты — in-memory; прод-glue — tenant-пути).
# ---------------------------------------------------------------------------

def mentions_provider(store, entity_key: str) -> Provider:
    """CrossSourceMentions → хронология упоминаний + сводка по источникам."""
    def fn() -> List[dict]:
        items = [m if isinstance(m, dict) else m.to_dict()
                 for m in store.timeline(entity_key)]
        out = []
        for m in items:
            payload = m.get("payload") or {}
            text = (f"упомянут в {m.get('source_type','?')}:"
                    f"{m.get('source_id','?')}")
            # контекст упоминания — самая ценная часть для повествования
            ctx = payload.get("context") or payload.get("text") or payload.get("title")
            if ctx:
                text += f" — {ctx}"
            out.append({"at": m.get("at", ""), "text": text,
                        "source_type": m.get("source_type"),
                        "source_id": m.get("source_id"),
                        "payload": payload})
        return out
    return fn


def events_provider(store, actor_key: str) -> Provider:
    """EventLog → датированные доменные события сущности-актора."""
    def fn() -> List[dict]:
        evs = [e if isinstance(e, dict) else e.to_dict()
               for e in store.events(actor=actor_key)]
        return [{"at": e.get("at", ""), "kind": e.get("kind", ""),
                 "text": f"{e.get('kind','событие')}: "
                         f"{(e.get('payload') or {}).get('title') or (e.get('payload') or {}).get('text') or ''}".strip(": "),
                 "payload": e.get("payload") or {}} for e in evs]
    return fn


def facts_provider(store, entity_name: str) -> Provider:
    """SupersedeStore → текущие значения фактов сущности + глубина истории.
    Ключи матчим по подстроке (конвенции ключей различаются по подсистемам)."""
    def fn() -> List[dict]:
        out = []
        for key in store.keys(contains=entity_name):
            cur = store.current(key)
            if cur is None:
                continue
            hist = store.history(key)
            last = hist[-1] if hist else {}
            out.append({"as_of": last.get("as_of", ""), "key": key, "value": cur,
                        "text": f"{key} = {cur}",
                        "versions": len(hist),
                        "total_mentions": store.total_mentions(key)})
        return out
    return fn


def default_providers(*, mentions_store=None, events_store=None, facts_store=None,
                      entity_key: str = "", entity_name: str = "",
                      extra: Optional[Dict[str, Provider]] = None) -> Dict[str, Provider]:
    """Собрать providers из доступных сторов (None → источник пропускается)
    + произвольные внешние (граф/persona/снапшоты) через extra."""
    providers: Dict[str, Provider] = {}
    if mentions_store is not None:
        providers["mentions"] = mentions_provider(mentions_store, entity_key or entity_name)
    if events_store is not None:
        providers["events"] = events_provider(events_store, entity_key or entity_name)
    if facts_store is not None:
        providers["facts"] = facts_provider(facts_store, entity_name or entity_key)
    if extra:
        providers.update(extra)
    return providers
