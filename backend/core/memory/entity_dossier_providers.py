# -*- coding: utf-8 -*-
"""
Дополнительные провайдеры для Universal Entity Dossier (CAPABILITY_READINESS C12).

ЗАЧЕМ. entity_dossier.default_providers даёт только память (mentions/events/
facts). Но «всё о сущности» должно включать и СТРУКТУРНЫЕ данные:
- узлы графа (Person/Team/Project с свойствами и связями),
- Persona (как с человеком разговаривать, его метафоры, сильные стороны),
- Snapshots (актуальный срез: текущие проекты, KPI, дельты).

Дизайн (как везде):
- Каждая фабрика возвращает Provider — sync-callable без аргументов.
- Async-источники зовутся через нашу sync-обёртку (asyncio.run в отдельном
  потоке через caller; провайдер инжектируется в досье уже как sync).
- ЧЕСТНОСТЬ §1: источник упал или вернул None → пустой список → coverage
  пометит 'empty' (не выдумываем).
- Инъекция объектов — модуль не знает про tenant_paths/инициализацию.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

Provider = Callable[[], List[dict]]


def _run_sync(coro):
    """Запустить async-coroutine синхронно — БЕЗОПАСНО в любом контексте.

    ИСПРАВЛЕНИЕ аудита F5: раньше тут был голый asyncio.run(coro), который
    кидает RuntimeError 'cannot be called from a running event loop' при
    вызове из корутины → broad-except в провайдерах глотал ошибку и МОЛЧА
    возвращал [] (graph/persona/snapshot терялись). Теперь: если loop НЕ
    запущен — asyncio.run; если запущен — гоняем в отдельном потоке с его
    собственным loop'ом и ждём результат."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)   # синхронный контекст — норма
    # уже внутри работающего loop'а: оффлоадим в поток со своим loop'ом
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# ---------- Граф ----------------------------------------------------------

def graph_provider(graph_builder, entity_name: str) -> Provider:
    """Узлы графа, имя которых матчится с сущностью (case-insensitive,
    подстрока). Только Person/Team/Project/Department по умолчанию.

    Возвращает list[dict] вида:
      {"at": "", "text": f"{label}: {name} — {role/description}",
       "label": ..., "id": ..., "properties": {...}}
    """
    target = (entity_name or "").strip().casefold()
    LABELS = ("Person", "Team", "Project", "Department", "Product")

    def fn() -> List[dict]:
        if not graph_builder or not target:
            return []
        out: List[dict] = []
        try:
            for label in LABELS:
                try:
                    nodes = _run_sync(
                        graph_builder.get_all_nodes_async(label=label))
                except Exception:
                    continue
                for n in nodes or []:
                    name = str(n.get("name") or "").casefold()
                    nid = str(n.get("id") or "").casefold()
                    if not name and not nid:
                        continue
                    if (name == target or nid == target
                            or (target and target in name)
                            or (target and target in nid)):
                        descr = (n.get("description") or n.get("role")
                                 or n.get("status") or "")
                        out.append({
                            "at": str(n.get("created_at") or
                                      n.get("updated_at") or ""),
                            "text": f"[{label}] {n.get('name') or n.get('id')}"
                                    + (f": {descr}" if descr else ""),
                            "label": label,
                            "id": n.get("id"),
                            "properties": {k: v for k, v in n.items()
                                           if not k.startswith("_")},
                        })
        except Exception as e:
            logger.debug("graph_provider failed: %s", e)
        return out

    return fn


# ---------- Persona -------------------------------------------------------

def persona_provider(persona_obj, entity_name: str) -> Provider:
    """Persona-сводка как одна-две позиции досье: стиль/метафоры/жаргон +
    strengths_weaknesses. Если persona_obj пуст / имена не совпадают —
    возвращаем пусто."""
    target = (entity_name or "").strip().casefold()

    def fn() -> List[dict]:
        if not persona_obj or not target:
            return []
        out: List[dict] = []
        # имена могут жить в разных полях
        names: list = []
        for attr in ("display_name", "name", "person_id", "user_id"):
            v = getattr(persona_obj, attr, None)
            if v:
                names.append(str(v).casefold())
        if names and not any(target == n or target in n or n in target
                             for n in names):
            return []

        # CommunicationCognitive → стиль/метафоры/жаргон/уровень абстракции
        try:
            cc = getattr(persona_obj, "communication_cognitive", None)
            if cc is not None:
                parts = []
                style = getattr(cc, "preferred_style", None)
                if style:
                    parts.append(f"стиль: {getattr(style, 'value', style)}")
                voc = getattr(cc, "vocabulary_register", None)
                if voc:
                    parts.append(f"регистр: {voc}")
                metas = list(getattr(cc, "metaphor_domains", []) or [])
                if metas:
                    parts.append(f"метафоры: {', '.join(metas[:6])}")
                abs_lv = getattr(cc, "abstraction_level", None)
                if abs_lv:
                    parts.append(
                        f"абстракция: {getattr(abs_lv, 'value', abs_lv)}")
                if parts:
                    out.append({"at": "", "text": "коммуникация — "
                               + "; ".join(parts), "source_type": "persona"})
        except Exception:
            pass

        # Сильные/слабые стороны
        try:
            sw = getattr(persona_obj, "strengths_weaknesses", None)
            if sw is not None:
                strong = list(getattr(sw, "strong", []) or [])
                weak = list(getattr(sw, "developing", []) or [])
                if strong or weak:
                    text = ("сильные: "
                            + ", ".join(getattr(a, "label", str(a))
                                        for a in strong[:5]))
                    if weak:
                        text += " | развивает: " + ", ".join(
                            getattr(a, "label", str(a)) for a in weak[:5])
                    out.append({"at": "", "text": text,
                                "source_type": "persona"})
        except Exception:
            pass

        return out

    return fn


# ---------- Snapshot ------------------------------------------------------

def snapshot_provider(snapshot_generator, entity_name: str,
                      entity_type: str = "") -> Provider:
    """PersonSnapshot/TeamSnapshot/DepartmentSnapshot — актуальная сводка."""
    et = (entity_type or "").strip().casefold()
    GETTERS = {
        "person":     "get_person_snapshot",
        "team":       "get_team_snapshot",
        "department": "get_department_snapshot",
        "project":    "get_project_snapshot",
    }

    def fn() -> List[dict]:
        if not snapshot_generator or not entity_name:
            return []
        getter_name = GETTERS.get(et)
        if not getter_name:
            return []
        getter = getattr(snapshot_generator, getter_name, None)
        if not callable(getter):
            return []
        try:
            snap = _run_sync(getter(entity_name))
        except Exception as e:
            logger.debug("snapshot_provider failed: %s", e)
            return []
        if not snap:
            return []
        try:
            text = snap.to_text() if hasattr(snap, "to_text") else str(snap)
        except Exception:
            text = str(snap)
        at = ""
        for attr in ("last_updated", "generated_at", "updated_at"):
            v = getattr(snap, attr, None)
            if v:
                at = str(v)
                break
        return [{"at": at, "text": text, "source_type": f"snapshot:{et}"}]

    return fn


def build_extra_providers(
    *,
    graph_builder=None,
    persona_obj=None,
    snapshot_generator=None,
    entity_name: str = "",
    entity_type: str = "",
) -> dict:
    """Собрать словарь extra-providers (None → провайдер пропущен)."""
    extra: dict = {}
    if graph_builder is not None:
        extra["graph"] = graph_provider(graph_builder, entity_name)
    if persona_obj is not None:
        extra["persona"] = persona_provider(persona_obj, entity_name)
    if snapshot_generator is not None:
        extra["snapshot"] = snapshot_provider(
            snapshot_generator, entity_name, entity_type)
    return extra
