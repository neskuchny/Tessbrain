# -*- coding: utf-8 -*-
"""GET /api/v1/graph/company — анализ компании для внешних систем-карт.

Контракт согласован с командой призменной карты
(docs/integrations/tessent-analysis.schema.json, их запрос —
docs/integrations/PRISM_MAP_ANSWER.md). Принцип: отдаём то, что граф и
снапшот УЖЕ посчитали — ничего не переизвлекаем, ничего не выдумываем;
пустое поле честнее заглушки.

Авторизация без пользовательской сессии (их вопрос №2): персональный токен
tess_mcp_… из вкладки «Интеграции» — он привязан ровно к ОДНОМУ тенанту,
выпускается и отзывается владельцем в UI, второй тенант этим токеном
прочитать нельзя. Обычные JWT (user/service) тоже принимаются.

Гарантии контракта: id сущностей = стабильные id узлов графа (переживают
пересинк); ссылочная целостность — связи на несуществующие id отбрасываются
у нас, а не ломают карту; дублей id нет.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)

# Метка узла графа → kind карты. Неизвестное карта сама считает entity.
_KIND = {"Task": "process", "Project": "process", "Process": "process",
         "Product": "result", "Resource": "resource"}
# Какие метки отдаём как сущности (Decision/Goal/Risk идут отдельными блоками)
_ENTITY_LABELS = ["Person", "Product", "Project", "Department", "Team",
                  "Task", "Resource", "Client", "Company", "Process"]


def _resolve_export_user(request: Request, id_param: Optional[str]) -> str:
    """tess_mcp_-токен (один тенант, отзываемый) ИЛИ проверенный JWT."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token.startswith("tess_mcp_"):
        try:
            import sys
            from pathlib import Path
            root = Path(__file__).resolve().parents[3]
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            import mcp_token_store
            uid = mcp_token_store.validate(token)
        except Exception:
            uid = None
        if uid:
            # токен жёстко одного тенанта: ?id= с чужим значением — отказ
            if id_param and id_param != uid:
                raise HTTPException(status_code=403,
                                    detail="token not authorized for this id")
            return uid
        raise HTTPException(status_code=401, detail="bad token")
    # ВАЖНО: только ПОДТВЕРЖДЁННАЯ подписью личность. trusted_user_id без
    # Bearer-токена возвращает (requested_user_id, "unverified") — принимать
    # это здесь означало бы отдать анализ любой компании по одному лишь
    # ?id=<чужой тенант>. Эндпоинт экспортирует данные наружу, поэтому
    # fail-closed: нет проверяемого токена — нет ответа.
    from backend.core.auth.service_token import trusted_user_id
    try:
        uid, source = trusted_user_id(request.headers, id_param or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/id mismatch")
    if source not in ("user_jwt", "service_jwt") or not uid:
        raise HTTPException(
            status_code=401,
            detail="нужен токен: tess_mcp_-токен интеграции или JWT аккаунта")
    return uid


def _s(v: Any) -> str:
    return str(v or "").strip()


def _node_entity(n: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
    nid = _s(n.get("id") or n.get("node_id") or n.get("task_id"))
    name = _s(n.get("name") or n.get("title"))
    if not nid or not name:
        return None
    ent: Dict[str, Any] = {"id": nid, "label": name,
                           "kind": _KIND.get(label, "entity")}
    if _s(n.get("problem")):
        ent["problem"] = _s(n["problem"])
    if _s(n.get("risk") or n.get("danger")):
        ent["danger"] = _s(n.get("risk") or n.get("danger"))
    if _s(n.get("goal")):
        ent["goal"] = _s(n["goal"])
    facts = [f for f in (n.get("facts") or []) if _s(f)]
    desc = _s(n.get("description") or n.get("summary"))
    if desc:
        facts = [desc] + facts
    if facts:
        ent["facts"] = facts[:10]
    if n.get("critical") is True or _s(n.get("priority")).lower() in (
            "critical", "критично"):
        ent["critical"] = True
    # опора-цитата: отдаём ТОЛЬКО если реально сохранена при извлечении
    quote = _s(n.get("quote") or n.get("evidence"))
    if quote:
        prov: Dict[str, Any] = {"quote": quote[:120]}
        if _s(n.get("meeting_id")):
            prov["meeting_id"] = _s(n["meeting_id"])
        ent["provenance"] = [prov]
    return ent


# Их схема разрешает у relations[].type только эти значения; наши типы
# рёбер графа (ASSIGNED_TO, IMPLEMENTS…) — семантика связи, а не они.
# Отдать сырой тип означало бы провалить их же валидатор схемы, поэтому
# кладём его в resource_flow (свободная строка), а type оставляем валидным.
_REL_TYPE_ENUM = {"direct", "indirect", "feedback", "emerging", "missing",
                  "reinforcing", "balancing", "resource"}


def _map_rel_type(raw: str) -> str:
    t = str(raw or "").strip().lower()
    return t if t in _REL_TYPE_ENUM else "direct"


def _rel_type(data: Any) -> str:
    """Тип nx-ребра. GraphBuilder пишет его в `_type` (см. edge_props при
    создании ребра); чтение `type` давало None → все связи схлопывались в
    «direct», а тип связи — главный сигнал призменной карты."""
    d = data or {}
    return _map_rel_type(d.get("_type") or d.get("type") or "")


def _rel_label(data: Any) -> str:
    """Исходный тип связи как есть — уходит в resource_flow, чтобы карта не
    теряла семантику, оставаясь в рамках их enum."""
    d = data or {}
    return _s(d.get("_type") or d.get("type"))


def _metric_from_kpi(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """KPI снапшота → метрика их контракта.

    Реальные формы: kpis [{name, current_value, target, trend}],
    financial_kpis [{name, value, period, series}]. Число отдаём числом
    (их просьба №3: без него конверсии между этапами не считаются); если
    значение — строка вида «18 платящих», отделяем unit."""
    name = _s(item.get("name"))
    if not name:
        return None
    raw = item.get("current_value")
    if raw is None:
        raw = item.get("value")
    m: Dict[str, Any] = {"name": name}
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        m["value"] = float(raw)
    else:
        parts = _s(raw).replace(",", ".").split()
        try:
            m["value"] = float(parts[0])
            if len(parts) > 1:
                m["unit"] = " ".join(parts[1:])
        except (ValueError, IndexError):
            m["value"] = None
        if _s(raw):
            m["display"] = _s(raw)
    if _s(item.get("trend")):
        m["trend"] = _s(item["trend"])
    if _s(item.get("target")):
        m["goal"] = _s(item["target"])
    return m


async def _collect_edges(gb, ids: set) -> List[Dict[str, Any]]:
    """Рёбра графа между экспортируемыми сущностями (nx или Neo4j)."""
    edges: List[Dict[str, Any]] = []
    nx_g = getattr(gb, "nx_graph", None)
    if nx_g is not None:
        try:
            for a, b, data in nx_g.edges(data=True):
                if str(a) not in ids or str(b) not in ids:
                    continue
                e: Dict[str, Any] = {"source": str(a), "target": str(b),
                                     "type": _rel_type(data)}
                raw = _rel_label(data)
                if raw and raw.lower() not in _REL_TYPE_ENUM:
                    e["resource_flow"] = raw
                edges.append(e)
            return edges
        except Exception:
            logger.debug("nx edges failed", exc_info=True)
    driver = getattr(gb, "driver", None)
    if driver is None:
        return edges
    try:
        # driver — AsyncDriver (graph_builder: `async with driver.session()`).
        # Синхронный `with` здесь молча падал бы в except и оставлял карту
        # без единой связи на всех Neo4j-инсталляциях.
        async with driver.session() as s:
            result = await s.run(
                "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
                "RETURN a.id AS s, b.id AS t, type(r) AS ty LIMIT 5000",
                {"ids": list(ids)})
            async for rec in result:
                raw = str(rec["ty"] or "")
                e: Dict[str, Any] = {"source": str(rec["s"]),
                                     "target": str(rec["t"]),
                                     "type": _map_rel_type(raw)}
                if raw and raw.lower() not in _REL_TYPE_ENUM:
                    e["resource_flow"] = raw
                edges.append(e)
    except Exception:
        logger.warning("graph export: edges unavailable", exc_info=True)
    return edges


async def build_company_analysis(user_id: str) -> Dict[str, Any]:
    """Собрать ответ контракта из графа + снапшота + целей. Never-invent."""
    from backend.core.store.graph_view import merged_graph_view_for_user

    # strict-tenant: наружу уходят ТОЛЬКО свои узлы. Без strict видны ещё и
    # legacy-узлы без tenant_id — чужие аккаунты (та же дыра, что чинили в
    # снапшоте компании: «Алексей из другого аккаунта», people=20).
    _tid = user_id or None
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    entities: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    seen_ids: set = set()
    try:
        for label in _ENTITY_LABELS:
            try:
                nodes = await gb.get_all_nodes_async(
                    label=label, limit=2000, tenant_id=_tid,
                    strict_tenant=bool(_tid))
            except Exception:
                continue
            for n in nodes or []:
                ent = _node_entity(n, label)
                if ent and ent["id"] not in seen_ids:
                    seen_ids.add(ent["id"])
                    entities.append(ent)
        for label in ("Decision", "Risk"):
            try:
                nodes = await gb.get_all_nodes_async(
                    label=label, limit=1000, tenant_id=_tid,
                    strict_tenant=bool(_tid))
            except Exception:
                continue
            for n in nodes or []:
                text = _s(n.get("summary") or n.get("description")
                          or n.get("name"))
                if not text:
                    continue
                d: Dict[str, Any] = {"text": text}
                if _s(n.get("status")):
                    d["status"] = _s(n["status"])
                if label == "Risk":
                    d["risk"] = True
                decisions.append(d)
        edges_raw = await _collect_edges(gb, seen_ids)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass

    # Ссылочная целостность на нашей стороне: битое ребро не ломает карту.
    # Плюс дедуп: в проде граф — MultiDiGraph, между теми же узлами может
    # быть несколько параллельных рёбер, и карта нарисовала бы дубли стрелок.
    relations: List[Dict[str, Any]] = []
    _seen_rel: set = set()
    for e in edges_raw:
        if e["source"] not in seen_ids or e["target"] not in seen_ids:
            continue
        key = (e["source"], e["target"], e.get("type") or "direct")
        if key in _seen_rel:
            continue
        _seen_rel.add(key)
        relations.append(e)

    # профиль компании + метрики — из снапшота (уже посчитан консолидацией)
    # id компании = организация, если человек в ней состоит. Иначе трое
    # коллег из одной компании создали бы в карте три «разные компании» с
    # пересекающимися сущностями, а отметки о выполнении размазались бы.
    _org = None
    try:
        from backend.core.ingest import membership
        _org = membership.get_org_for_user(user_id)
    except Exception:
        logger.debug("graph export: org lookup skipped", exc_info=True)
    company: Dict[str, Any] = {"id": _org or user_id}
    metrics: List[Dict[str, Any]] = []
    mission = ""
    try:
        from backend.core.reports.report_context import _company_snapshot_text  # noqa: F401
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        from backend.core.store.graph_view import merged_graph_view_for_user as _mv
        gb2 = await _mv(user_id, use_networkx=None)
        try:
            gen = get_enhanced_snapshot_generator(gb2, user_id=user_id)
            snap = await gen.get_company_snapshot(force_regenerate=False)
            d = snap.to_dict() if hasattr(snap, "to_dict") else {}
        finally:
            try:
                await gb2.close(save=False)
            except Exception:
                pass
        if _s(d.get("name")):
            company["name"] = _s(d["name"])
        if _s(d.get("industry")):
            company["domain"] = _s(d["industry"])
        if _s(d.get("description")):
            company["description"] = _s(d["description"])
        mission = _s(d.get("mission"))
        # kpis и financial_kpis — это СПИСКИ словарей, а не словарь:
        #   kpis:           [{name, current_value, target, trend}]
        #   financial_kpis: [{name, value, period, series, source}]
        # Прошлая версия проверяла isinstance(val, dict) → цикл не выполнялся
        # ни разу и metrics не уезжали вообще, хотя обещаны в контракте.
        for block in ("kpis", "financial_kpis"):
            for item in (d.get(block) or []):
                if not isinstance(item, dict):
                    continue
                m = _metric_from_kpi(item)
                if m:
                    metrics.append(m)
    except Exception:
        logger.warning("graph export: снапшот компании недоступен",
                       exc_info=True)

    goals: List[Dict[str, Any]] = []
    try:
        from backend.core.goals.goal_tracker import goal_store_for_user
        for g in goal_store_for_user(user_id).list_goals():
            row: Dict[str, Any] = {"goal": _s(g.get("title"))}
            if _s(g.get("target_date")):
                row["target"] = _s(g["target_date"])
            if row["goal"]:
                goals.append(row)
    except Exception:
        logger.debug("graph export: цели пропущены", exc_info=True)

    out: Dict[str, Any] = {"company": company, "entities": entities}
    if relations:
        out["relations"] = relations
    if metrics:
        out["metrics"] = metrics
    if decisions:
        out["decisions"] = decisions
    if goals:
        out["goals"] = goals
    if mission:
        out["mission"] = mission
    return out


@get("/company")
async def graph_company(request: Request,
                        id: Optional[str] = Parameter(query="id",
                                                      default=None)) -> Dict[str, Any]:
    uid = _resolve_export_user(request, id)
    return await build_company_analysis(uid)


# ── Исходящий пуш в карту компетенций ───────────────────────────────────────

def _resolve_user_jwt(request: Request, user_id: Optional[str]) -> str:
    """Только подтверждённая личность — этот POST выгружает когнитивные
    профили СОТРУДНИКОВ во внешнюю систему. Ролевой гейт внутри проверяет
    роль переданного user_id, а не запрашивающего, поэтому «unverified»
    здесь означало бы: любой без токена сливает чужую команду наружу."""
    from backend.core.auth.service_token import trusted_user_id
    try:
        uid, source = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    if source not in ("user_jwt", "service_jwt") or not uid:
        raise HTTPException(status_code=401,
                            detail="нужен вход в аккаунт (JWT)")
    return uid


@post("/sync")
async def competency_map_sync(
        request: Request,
        user_id: str = Parameter(query="user_id", required=True),
        dry_run: bool = Parameter(query="dry_run", default=False)) -> Dict[str, Any]:
    """Выгрузить cogni-профили команды в карту компетенций.

    dry_run=true — план (кто уедет, кто и почему пропущен) без единого
    сетевого вызова, работает всегда. Реальная отправка — только при
    ENABLE_COMPETENCY_MAP_PUSH; порядок фиксирован — отделы раньше людей."""
    uid = _resolve_user_jwt(request, user_id)
    from backend.core.integrations.competency_map_push import (
        push_to_competency_map,
    )
    return await push_to_competency_map(uid, dry_run=dry_run)


router = Router(path="/graph", route_handlers=[graph_company],
                tags=["Graph Export"])

competency_map_router = Router(path="/integrations/competency-map",
                               route_handlers=[competency_map_sync],
                               tags=["Competency Map"])
