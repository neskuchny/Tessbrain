# -*- coding: utf-8 -*-
"""
Объектный 360-браузер — «палантир-модуль», часть 4 (docs/ru/DATA_ONTOLOGY.md).

«Кликнул на Петю → всё про Петю на одном экране»: факты (с версиями),
упоминания через встречи/документы/чаты, события, связи из графа знаний,
датасеты, где сущность живёт (заземлённые колонки). Детерминированная
СБОРКА из существующих сторов — без LLM, ничего не выдумывается:
недоступный источник честно отсутствует в sources_present.

Все сторы инжектируемы (тестируемость без networkx/БД); прод-обёртка
object360_for_user собирает их по tenant_paths (изоляция per-user).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LIMIT = 15


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def build_object360(name: str, *,
                    facts_store=None, mentions_store=None,
                    events_store=None, registry=None,
                    graph_view: Optional[dict] = None,
                    tracker_tasks: Optional[List[dict]] = None,
                    verified_store=None) -> dict:
    """Собрать 360-карточку сущности из доступных сторов.

    Каждый источник best-effort: упал/пуст → секции нет, в
    sources_present он не значится (честно, без заглушек)."""
    name = (name or "").strip()
    card: dict = {"name": name, "sources_present": [], "related": []}
    related: Dict[str, str] = {}      # name → откуда узнали

    # 1) Факты (supersede: версии не теряются)
    if facts_store is not None:
        try:
            facts = []
            for key in facts_store.keys(contains=name)[:_LIMIT * 2]:
                hist = facts_store.history(key)
                item = {
                    "key": key,
                    "current": facts_store.current(key),
                    "versions": len(hist),
                    "mentions": facts_store.total_mentions(key),
                }
                # аддитивно: отметка «проверено» из ОТДЕЛЬНОГО стора
                # (supersede только читается; ошибки глушатся)
                if verified_store is not None:
                    try:
                        as_of = (hist[-1].get("as_of")
                                 if hist and isinstance(hist[-1], dict)
                                 else getattr(hist[-1], "as_of", None)
                                 if hist else None)
                        vs = verified_store.status(key, as_of)
                        if vs:
                            item["verified"] = vs
                    except Exception:
                        pass
                facts.append(item)
            if facts:
                card["facts"] = facts[:_LIMIT]
                card["sources_present"].append("facts")
        except Exception as e:
            logger.debug(f"360 facts unavailable: {e}")

    # 2) Упоминания через источники (встречи/документы/чаты)
    if mentions_store is not None:
        try:
            summary = mentions_store.sources_summary(name)
            tl = mentions_store.timeline(name)
            timeline = []
            for m in list(tl)[-_LIMIT:]:
                d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
                timeline.append({k: d.get(k) for k in
                                 ("source_type", "source_id", "at", "count")
                                 if k in d})
            if summary or timeline:
                card["mentions"] = {"by_source": summary,
                                    "recent": timeline}
                card["sources_present"].append("mentions")
        except Exception as e:
            logger.debug(f"360 mentions unavailable: {e}")

    # 3) События (доменный event log)
    if events_store is not None:
        try:
            kinds = events_store.kinds(actor=name)
            evs = []
            for ev in list(events_store.events(actor=name))[-_LIMIT:]:
                d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
                evs.append({k: d.get(k) for k in ("kind", "at", "payload")
                            if k in d})
            if kinds or evs:
                card["events"] = {"kinds": kinds, "recent": evs}
                card["sources_present"].append("events")

            # 3b) Daily Pulse: операционка отдельной секцией (стендапы/
            #     блокеры/сигналы по задачам приходят в тот же event_log из
            #     интеграции — провенанс _source=daily_pulse). Это «что
            #     ПРОИСХОДИТ» в дополнение к «что РЕШИЛИ».
            dp = _daily_pulse_section(events_store, name)
            if dp:
                card["daily_pulse"] = dp
                card["sources_present"].append("daily_pulse")

            # 3c) Колл-центр (CallInsight): как сотрудник работает с
            #     клиентами — жалобы в его звонках, сделки к спасению,
            #     coach-советы и их исходы (worked/worsened — measured).
            cc = _callcenter_section(events_store, name)
            if cc:
                card["callcenter"] = cc
                card["sources_present"].append("callcenter")
        except Exception as e:
            logger.debug(f"360 events unavailable: {e}")

    # 4) Граф знаний: сам узел + соседи по типам
    if graph_view:
        node = graph_view.get("node") or {}
        neighbors = graph_view.get("neighbors") or []
        grouped: Dict[str, List[dict]] = {}
        for nb in neighbors:
            label = _norm(nb.get("label")) or "other"
            entry = {"name": nb.get("name") or nb.get("id"),
                     "title": nb.get("title"),
                     "rel_type": nb.get("rel_type"),
                     "direction": nb.get("direction")}
            # Богатые поля психопрофиля выводим наружу (тип личности и пр.)
            if nb.get("details"):
                entry["details"] = nb["details"]
            if nb.get("decision_maker"):
                entry["decision_maker"] = nb["decision_maker"]
            if nb.get("needs_human_review") is not None:
                entry["needs_human_review"] = nb.get("needs_human_review")
                entry["severity"] = nb.get("severity")
            grouped.setdefault(label, []).append(entry)
            if entry["name"] and label in ("person", "project", "product",
                                           "department", "team"):
                related.setdefault(str(entry["name"]), f"graph:{label}")
        # задачи — отдельной секцией со статусами (не просто имена)
        task_labels = ("task", "задача")
        tasks = [nb for nb in neighbors
                 if _norm(nb.get("label")) in task_labels]
        if tasks:
            card["tasks"] = [{
                "title": t.get("title") or t.get("name"),
                "status": t.get("status") or "—",
                "deadline": t.get("deadline"),
                "source": "graph",
            } for t in tasks[:_LIMIT]]
            card["sources_present"].append("tasks")
        if node or grouped:
            card["graph"] = {
                "node": {k: node.get(k) for k in
                         ("label", "name", "title", "role", "description")
                         if node.get(k)},
                "neighbors": {k: v[:_LIMIT] for k, v in grouped.items()},
            }
            card["sources_present"].append("graph")

    # 4b) Задачи из внешних задачников (yougile/trello/jira) — назначенные
    #     на сущность; дедуп с граф-задачами по заголовку
    if tracker_tasks:
        existing = {_norm(t.get("title")) for t in card.get("tasks") or []}
        merged = list(card.get("tasks") or [])
        for t in tracker_tasks:
            if _norm(t.get("title")) in existing:
                continue
            merged.append({
                "title": t.get("title"),
                "status": t.get("status") or "—",
                "deadline": t.get("deadline") or t.get("due_date"),
                "source": t.get("tracker") or t.get("system") or "tracker",
            })
        if merged:
            card["tasks"] = merged[:_LIMIT * 2]
            if "tasks" not in card["sources_present"]:
                card["sources_present"].append("tasks")

    # 5) Датасеты, где сущность живёт (заземлённые колонки)
    if registry is not None:
        try:
            hits = []
            for slim in registry.list():
                rec = registry.get(slim["dataset_id"]) or {}
                grounding = (rec.get("ontology") or {}).get("grounding", {})
                prof_cols = {p["name"]: p for p in
                             (rec.get("profile") or {}).get("columns", [])}
                for col, g in grounding.items():
                    examples = {_norm(x) for x in (g.get("examples") or [])}
                    cats = {_norm(c) for c in
                            (prof_cols.get(col, {}).get("categories") or {})}
                    samples = {_norm(s) for s in
                               prof_cols.get(col, {}).get("samples", [])}
                    if _norm(name) in (examples | cats | samples):
                        hit = {"dataset_id": rec.get("dataset_id"),
                               "title": rec.get("title"),
                               "column": col,
                               "entity_type": g.get("entity_type")}
                        # живые цифры про сущность: фильтр по колонке →
                        # count + суммы числовых колонок (считает код)
                        try:
                            from backend.core.ontology.query_engine import (
                                execute_plan,
                            )
                            rows = registry.load_rows(rec["dataset_id"])
                            flt = [{"column": col, "op": "eq", "value": name}]
                            cnt = execute_plan(
                                {"op": "count", "column": None,
                                 "group_by": None, "limit": 10,
                                 "filters": flt}, rows)
                            hit["rows"] = cnt.get("result", 0)
                            totals = {}
                            prof = rec.get("profile") or {}
                            num_cols = [c for c in
                                        prof.get("numeric_columns", [])
                                        if c not in prof.get(
                                            "percent_columns", [])][:3]
                            for nc in num_cols:
                                s = execute_plan(
                                    {"op": "sum", "column": nc,
                                     "group_by": None, "limit": 10,
                                     "filters": flt}, rows)
                                if s.get("result") is not None:
                                    totals[nc] = s["result"]
                            if totals:
                                hit["totals"] = totals
                        except Exception as e:
                            logger.debug(f"360 dataset numbers: {e}")
                        hits.append(hit)
                        break
            if hits:
                card["datasets"] = hits[:_LIMIT]
                card["sources_present"].append("datasets")
        except Exception as e:
            logger.debug(f"360 datasets unavailable: {e}")

    card["related"] = [{"name": n, "via": via}
                       for n, via in list(related.items())[:_LIMIT]
                       if _norm(n) != _norm(name)]
    card["found"] = bool(card["sources_present"])
    return card


_DP_KINDS = ("standup", "blocker", "chat_digest", "weekly_report",
             "sprint_analysis")


def _is_dp_event(d: dict) -> bool:
    """Событие из Daily Pulse: provenance.source=daily_pulse или
    известный kind/префикс task_signal:."""
    prov = (d.get("payload") or {}).get("provenance") or {}
    if str(prov.get("source") or "") == "daily_pulse":
        return True
    kind = str(d.get("kind") or "")
    return kind in _DP_KINDS or kind.startswith("task_signal:")


def _daily_pulse_section(events_store, name: str) -> Optional[dict]:
    """Выжимка операционки из event_log: последние стендапы, активные
    блокеры, сигналы по задачам, последний недельный отчёт. Только
    daily-pulse события; нет их → None (секция не появится)."""
    try:
        rows = []
        for ev in list(events_store.events(actor=name)):
            d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
            if _is_dp_event(d):
                rows.append(d)
    except Exception:
        return None
    if not rows:
        return None
    rows.sort(key=lambda d: str(d.get("at") or ""))

    blockers, signals, standups = [], [], []
    weekly = None
    for d in rows:
        kind = str(d.get("kind") or "")
        pl = d.get("payload") or {}
        if kind == "blocker":
            blockers.append({"task": pl.get("task"), "reason": pl.get("reason"),
                             "severity": pl.get("severity"),
                             "days_open": pl.get("days_open"),
                             "at": d.get("at")})
        elif kind.startswith("task_signal:"):
            signals.append({"signal": kind.split(":", 1)[1],
                            "task": pl.get("task"),
                            "evidence": pl.get("evidence"), "at": d.get("at")})
        elif kind == "standup":
            standups.append({"at": d.get("at"), "mood": pl.get("mood")})
        elif kind == "weekly_report":
            weekly = {"at": d.get("at"), "metrics": pl.get("metrics") or {}}
    out = {"source": "daily_pulse",
           "blockers": blockers[-_LIMIT:],
           "signals": signals[-_LIMIT:],
           "standups_recent": standups[-5:],
           "weekly_report": weekly,
           "note": ("операционка из Daily Pulse — _source разный: блокеры/"
                    "метрики measured, стендапы — llm_narrative")}
    # пустая секция (только note) — не показываем
    if not (blockers or signals or standups or weekly):
        return None
    return out


_CC_KINDS = ("cc_complaint", "cc_deal_at_risk", "cc_coach_rec",
             "cc_coach_outcome", "cc_objection", "cc_digest")


def _callcenter_section(events_store, name: str) -> Optional[dict]:
    """Выжимка работы с клиентами из event_log (события CallInsight,
    actor = имя менеджера в звонках): жалобы, сделки к спасению,
    coach-советы + исходы. Исходы — measured (метрика выросла/упала),
    жалобы/советы — llm_narrative (вывод их LLM). Нет событий → None."""
    try:
        rows = []
        for ev in list(events_store.events(actor=name)):
            d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
            prov = (d.get("payload") or {}).get("provenance") or {}
            if (str(prov.get("source") or "") == "callinsight"
                    or str(d.get("kind") or "") in _CC_KINDS):
                rows.append(d)
    except Exception:
        return None
    if not rows:
        return None
    rows.sort(key=lambda d: str(d.get("at") or ""))

    complaints, at_risk, advice = [], [], []
    outcomes = {"worked": 0, "worsened": 0, "implicit": 0}
    for d in rows:
        kind = str(d.get("kind") or "")
        pl = d.get("payload") or {}
        if kind == "cc_complaint":
            complaints.append({"at": d.get("at"),
                               "marker": pl.get("marker_phrase"),
                               "excerpt": pl.get("excerpt"),
                               "deep_link": pl.get("deep_link")})
        elif kind == "cc_deal_at_risk":
            at_risk.append({"at": d.get("at"),
                            "customer_phone": pl.get("customer_phone"),
                            "key_insight": pl.get("key_insight"),
                            "deep_link": pl.get("deep_link")})
        elif kind == "cc_coach_rec":
            advice.append({"at": d.get("at"), "title": pl.get("title"),
                           "focus_metric": pl.get("focus_metric")})
        elif kind == "cc_coach_outcome":
            verdict = str(pl.get("verdict") or "").lower()
            if verdict in outcomes:
                outcomes[verdict] += 1
    if not (complaints or at_risk or advice or any(outcomes.values())):
        return None
    return {"source": "callinsight",
            "complaints": complaints[-_LIMIT:],
            "at_risk": at_risk[-_LIMIT:],
            "coach_advice": advice[-_LIMIT:],
            "coach_outcomes": outcomes,
            "note": ("работа с клиентами по данным CallInsight — исходы "
                     "советов measured (по движению метрики), жалобы/"
                     "советы — вывод их LLM (llm_narrative)")}


async def _graph_view_for(user_id: str, name: str) -> Optional[dict]:
    """Узел сущности + соседи из per-user графа знаний (best-effort).

    Backend-agnostic: работает и с NetworkX, и с Neo4j. Раньше код читал
    ТОЛЬКО gb.nx_graph (in-memory) и на Neo4j-деплое (nx_graph=None) сразу
    возвращал None → Object 360 всегда отдавал found:false («не работает»).
    Теперь — через async-API GraphBuilder (search_nodes / get_node_by_id /
    get_node_relationships), который един для обоих backend'ов.
    """
    gb = None
    try:
        # merged-вид personal ∪ org: проект, извлечённый из ОБЩИХ встреч,
        # живёт в орг-графе — открывая только личный файл, 360 честно
        # отвечал «не найдено» на реальный проект («Не нашел в 360 проект
        # finai»). Тот же паттерн, что у слепков и задач.
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        if not gb.connected:
            return None

        nm = _norm(name)

        # Анти-протечка (общий Neo4j): и поиск, и lookup'ы по id — только
        # свои tenant-ы (user + организации + коллеги) и legacy-узлы. Иначе
        # чужие соседи (децидент, психопрофиль) протекали в 360.
        try:
            from backend.core.store.tenant_scope import allowed_tenants
            _own = sorted(allowed_tenants(user_id)) or None
        except Exception:
            _own = None

        # 1) Находим узел по имени: строгий тенант + добор по своим оргам.
        # search_nodes принимает ОДИН tenant — узлы, поднятые на уровень
        # организации (tenant_id=org), под фильтром user_id не находились.
        hits: list = []
        _seen_ids: set = set()
        for _t in ([user_id] + [t for t in (_own or []) if t != user_id]):
            try:
                for h in await gb.search_nodes(query=name, limit=15,
                                               tenant_id=_t) or []:
                    hid = h.get("id")
                    if hid and hid not in _seen_ids:
                        _seen_ids.add(hid)
                        hits.append(h)
            except Exception:
                logger.debug("360: search for tenant %s failed", _t,
                             exc_info=True)

        target_id = None
        target_node = None
        for h in hits:
            if nm in (_norm(h.get("name")), _norm(h.get("title")), _norm(h.get("id"))):
                target_id = h.get("id")
                target_node = h
                break
        if target_id is None and hits:
            # Точного совпадения нет, но поиск (CONTAINS) что-то нашёл:
            # «finai» должен находить узел «Проект FinAI». Берём самое
            # короткое имя с вхождением запроса — наименьший риск взять
            # случайную длинную сущность.
            sub = [h for h in hits
                   if nm and (nm in _norm(h.get("name"))
                              or nm in _norm(h.get("title")))]
            sub.sort(key=lambda h: len(_norm(h.get("name"))
                                       or _norm(h.get("title")) or "z" * 99))
            if sub:
                target_id = sub[0].get("id")
                target_node = sub[0]

        # 2) Fallback: прямой lookup по id.
        if target_id is None:
            try:
                n = await gb.get_node_by_id(name, tenant_id=_own)
                if n and n.get("id"):
                    target_id = n.get("id")
                    target_node = n
            except Exception:
                logger.debug("360: get_node_by_id fallback failed", exc_info=True)

        if target_id is None:
            return None

        node = dict(target_node or {})

        # 3) Соседи через get_node_relationships (Neo4j + NetworkX).
        neighbors = []
        try:
            rels = await gb.get_node_relationships(target_id)
            seen = set()
            # Тегируем направление, чтобы знать СМЫСЛ связи (кто принял / кто
            # подчиняется / что заменило). dir=out: target→other; in: other→target.
            tagged = ([("out", r) for r in rels.get("outgoing", [])]
                      + [("in", r) for r in rels.get("incoming", [])])
            for direction, rel in tagged:
                nb_id = rel.get("target") if direction == "out" else rel.get("source")
                rtype = rel.get("type")
                if not nb_id or nb_id in seen:
                    continue
                seen.add(nb_id)
                nd = await gb.get_node_by_id(nb_id, tenant_id=_own)
                if not nd:
                    continue
                _nb_label = nd.get("_label") or nd.get("label")
                _nb = {
                    "id": nb_id,
                    "label": _nb_label,
                    "name": nd.get("name"),
                    "title": nd.get("title"),
                    "status": nd.get("status"),
                    "deadline": nd.get("deadline") or nd.get("due_date"),
                    # Тип и направление связи — для атрибуции/эволюции/оргсвязей
                    "rel_type": rtype,
                    "direction": direction,
                }
                _lbl = str(_nb_label or "").lower()
                # Психопрофиль — богатые поля наружу.
                if _lbl == "psychologicalprofile":
                    _nb["created_at"] = (nd.get("created_at")
                                         or nd.get("updated_at") or "")
                    _nb["details"] = {
                        "personality_type": nd.get("personality_type") or "",
                        "dominant_traits": nd.get("dominant_traits") or [],
                        "communication_style": nd.get("communication_style") or "",
                        "motivation_drivers": nd.get("motivation_drivers") or [],
                        "team_role": nd.get("team_role") or "",
                        "strengths": nd.get("strengths") or [],
                        "leadership_style": nd.get("leadership_style") or "",
                    }
                # Решение — атрибуция (кто принял) + было ли заменено.
                if _lbl == "decision":
                    _nb["decision_maker"] = (
                        nd.get("decision_maker_resolved")
                        or nd.get("decision_maker")
                        or nd.get("decision_maker_speaker") or "")
                # Противоречие — нужна ли ручная проверка + важность.
                if _lbl == "contradiction":
                    _nb["needs_human_review"] = bool(nd.get("needs_human_review"))
                    _nb["severity"] = nd.get("severity") or ""
                neighbors.append(_nb)
                if len(neighbors) >= 80:
                    break
        except Exception:
            logger.debug("360: neighbors unavailable", exc_info=True)

        # СХЛОПЫВАЕМ психопрофили: пайплайн пишет профиль НА КАЖДУЮ встречу
        # (это история наблюдений), а карточка вываливала все ~20 подряд —
        # «куча профилей одного человека». Показываем только самый свежий;
        # сколько наблюдений за ним стоит — в profiles_history.
        psych = [n for n in neighbors
                 if str(n.get("label") or "").lower() == "psychologicalprofile"]
        if len(psych) > 1:
            psych.sort(key=lambda n: str(n.get("created_at") or ""), reverse=True)
            keep = psych[0]
            keep["profiles_history"] = len(psych)
            drop_ids = {n["id"] for n in psych[1:]}
            neighbors = [n for n in neighbors if n["id"] not in drop_ids]

        return {"node": node, "neighbors": neighbors}
    except Exception as e:
        logger.debug(f"360 graph unavailable: {e}")
        return None
    finally:
        if gb is not None:
            try:
                await gb.close(save=False)
            except Exception:
                pass


async def object360_for_user(user_id: str, name: str) -> dict:
    """Прод-сборка 360: все сторы пользователя по tenant_paths."""
    from backend.core.store import tenant_paths as tp

    def _mk(cls_path: str, path_fn) -> Any:
        try:
            module_name, cls_name = cls_path.rsplit(".", 1)
            import importlib
            cls = getattr(importlib.import_module(module_name), cls_name)
            return cls(persist_path=path_fn(user_id))
        except Exception as e:
            logger.debug(f"360 store {cls_path} unavailable: {e}")
            return None

    facts = _mk("backend.core.memory.supersede_store.SupersedeStore",
                tp.supersede_store_path_for_user)
    mentions = _mk(
        "backend.core.memory.cross_source_mentions.CrossSourceMentions",
        tp.cross_source_mentions_path_for_user)
    events = _mk("backend.core.memory.event_log.EventLog",
                 tp.event_log_path_for_user)
    try:
        from backend.core.ontology.dataset_service import registry_for_user
        registry = registry_for_user(user_id)
    except Exception:
        registry = None
    graph_view = await _graph_view_for(user_id, name)

    # задачи из настроенных задачников, назначенные на сущность
    tracker_tasks: List[dict] = []
    try:
        from backend.core.tasks.task_analysis import (
            collect_tasks_from_trackers,
        )
        for t in await collect_tasks_from_trackers(user_id):
            assignee = _norm(t.get("assignee"))
            if assignee and (_norm(name) in assignee
                             or assignee in _norm(name)):
                tracker_tasks.append(t)
    except Exception as e:
        logger.debug(f"360 tracker tasks unavailable: {e}")

    try:
        from backend.core.memory.verified_answers import verified_for_user
        verified = verified_for_user(user_id)
    except Exception:
        verified = None

    card = build_object360(name, facts_store=facts, mentions_store=mentions,
                           events_store=events, registry=registry,
                           graph_view=graph_view,
                           tracker_tasks=tracker_tasks or None,
                           verified_store=verified)

    # Вердикт из PersonSnapshot (единый источник истины с карточкой
    # «Сотрудник»): LLM-выжимка + метрики активности. Читаем ГОТОВЫЙ снапшот
    # с диска (пишется ночной консолидацией) — без генерации, дёшево.
    try:
        import json as _json
        pdir = tp.snapshots_dir_for_user(user_id) / "persons"
        if pdir.exists():
            want = _norm(name)
            for f in pdir.glob("*.json"):
                try:
                    data = _json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                nm = _norm(data.get("name", ""))
                if nm and (want in nm or nm in want):
                    card["verdict"] = {
                        "summary": data.get("activity_summary", ""),
                        "role": data.get("role", ""),
                        "department": data.get("department", ""),
                        "tasks_in_progress": data.get("tasks_in_progress", 0),
                        "tasks_completed_week": data.get("tasks_completed_week", 0),
                        "meetings": data.get("meetings_participated", 0),
                        "achievements": (data.get("recent_achievements") or [])[:3],
                        "updated": data.get("last_updated", ""),
                    }
                    # Эволюция (ночной синтез по истории психопрофилей) —
                    # живёт на Person-узле графа, не в снапшоте.
                    try:
                        gv = card.get("graph") or {}
                        evo = (gv.get("node") or {}).get("psych_evolution")
                        if evo:
                            card["verdict"]["evolution"] = evo
                    except Exception:
                        pass
                    card["sources_present"].append("person_snapshot")
                    break
    except Exception as e:
        logger.debug(f"360 person snapshot verdict unavailable: {e}")

    return card
