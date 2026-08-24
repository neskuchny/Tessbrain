# -*- coding: utf-8 -*-
"""Консоль холдинга — Слой 1: обзор корпораций (docs/HOLDING_CONSOLE.md).

Директор владеет N организациями (корпорациями). Здесь:
  GET    /holdings/mine                    — мои холдинги + корпорации (с именами)
  POST   /holdings                         — создать холдинг {name}
  POST   /holdings/{id}/orgs               — привязать корпорацию {org_id}
  DELETE /holdings/{id}/orgs/{org_id}      — отвязать
  GET    /holdings/{id}/overview           — карточки: по каждой корпорации сводка
                                             её мозга (узлы, по типам) — read-only

Доступ: только владелец холдинга (owner_user_id). Изоляция корпораций
сохраняется — директор видит их по отдельности, данные не смешиваются.
Вопросы к корпорации и roll-up — Слой 2/3 (см. док)."""
from __future__ import annotations

import json as _json
import logging
from typing import Any, Optional

from litestar import Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.holdings import holding_store
from backend.core.ingest import org_store
from backend.core.store.tenant_paths import org_graph_path

logger = logging.getLogger(__name__)


def _caller(authorization: Optional[str]) -> str:
    # Переиспускаем разбор Bearer из orgs-роутов (единая логика идентичности).
    from backend.api.routes.orgs import _extract_caller
    caller_id, _role = _extract_caller(authorization)
    return caller_id


def _org_name(org_id: str) -> str:
    meta = org_store.get_org(org_id)
    return (meta or {}).get("name") or org_id


def _org_brain_summary(org_id: str) -> dict[str, Any]:
    """Сводка орг-мозга для владельца (read-only, видит всё): всего узлов и
    разбивка по типам. Пустой/отсутствующий граф → честные нули."""
    by_label: dict[str, int] = {}
    total = 0
    try:
        with open(org_graph_path(org_id), "r", encoding="utf-8") as f:
            data = _json.load(f)
        for n in data.get("nodes", []):
            if not isinstance(n, dict):
                continue
            total += 1
            label = str(n.get("_label") or n.get("label") or n.get("entity_type") or "Прочее")
            by_label[label] = by_label.get(label, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("holding overview: org graph read failed (%s): %s", org_id, e)
    top = sorted(by_label.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {"nodes_total": total, "by_type": [{"type": k, "count": v} for k, v in top]}


@get("/holdings/mine", status_code=200)
async def holdings_mine(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Холдинги, которыми владеет вызывающий, + корпорации с именами."""
    caller = _caller(authorization)
    out = []
    for h in holding_store.get_holdings_for_owner(caller):
        orgs = [{"org_id": oid, "name": _org_name(oid)} for oid in (h.get("org_ids") or [])]
        out.append({"id": h["id"], "name": h.get("name"), "orgs": orgs})
    return {"holdings": out}


@post("/holdings", status_code=201)
async def holdings_create(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    caller = _caller(authorization)
    name = (data or {}).get("name") or ""
    rec = holding_store.create_holding(caller, name)
    return {"holding": {"id": rec["id"], "name": rec["name"], "orgs": []}}


@post("/holdings/{holding_id:str}/orgs", status_code=200)
async def holdings_attach_org(
    holding_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    caller = _caller(authorization)
    if not holding_store.is_owner(holding_id, caller):
        raise HTTPException(status_code=403, detail="not the holding owner")
    org_id = ((data or {}).get("org_id") or "").strip()
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id required")
    meta = org_store.get_org(org_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"org {org_id} not found")
    # Привязать можно только СВОЮ корпорацию: основатель org (created_by)
    # или её founder/admin по membership. Иначе владелец холдинга мог бы
    # привязать чужой org_id и читать весь его мозг через overview/rollup/ask.
    from backend.core.ingest import membership
    if meta.get("created_by") != caller and not membership.can_manage_org(caller, org_id):
        raise HTTPException(
            status_code=403,
            detail="можно привязать только организацию, которой вы управляете")
    # Сегрегация портфеля: full (default) — отчёт/вопросы к мозгу;
    # aggregate — «китайская стена», только счётчики.
    policy = str((data or {}).get("data_policy") or "full")
    h = holding_store.add_org(holding_id, org_id, data_policy=policy)
    return {"holding_id": holding_id, "org_ids": (h or {}).get("org_ids", []),
            "org_policies": (h or {}).get("org_policies", {})}


@delete("/holdings/{holding_id:str}/orgs/{org_id:str}", status_code=200)
async def holdings_detach_org(
    holding_id: str,
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    caller = _caller(authorization)
    if not holding_store.is_owner(holding_id, caller):
        raise HTTPException(status_code=403, detail="not the holding owner")
    h = holding_store.remove_org(holding_id, org_id)
    return {"holding_id": holding_id, "org_ids": (h or {}).get("org_ids", [])}


def _require_owner_of_org(caller: str, holding_id: str, org_id: str) -> None:
    """Владелец холдинга + org реально в этом холдинге. Иначе 403."""
    if not holding_store.is_owner(holding_id, caller):
        raise HTTPException(status_code=403, detail="not the holding owner")
    h = holding_store.get_holding(holding_id)
    if not h or org_id not in (h.get("org_ids") or []):
        raise HTTPException(status_code=404, detail="corporation not in this holding")


def _require_full_policy(holding_id: str, org_id: str) -> None:
    """Сегрегация портфеля: org с политикой aggregate читается через холдинг
    только агрегатами — содержимое мозга (отчёт/вопросы) закрыто."""
    if holding_store.org_policy(holding_id, org_id) != "full":
        raise HTTPException(
            status_code=403,
            detail="для этой корпорации включена сегрегация данных "
                   "(aggregate): через холдинг доступны только агрегаты")


def _org_report(org_id: str) -> dict[str, Any]:
    """Детерминированный отчёт по корпорации из орг-графа (без LLM): сводка +
    свежие решения/задачи/проекты/люди. Владелец видит всё (read-only)."""
    nodes: list[dict[str, Any]] = []
    try:
        with open(org_graph_path(org_id), "r", encoding="utf-8") as f:
            nodes = [n for n in _json.load(f).get("nodes", []) if isinstance(n, dict)]
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("org report read failed (%s): %s", org_id, e)

    def _label(n: dict[str, Any]) -> str:
        return str(n.get("_label") or n.get("label") or n.get("entity_type") or "")

    def _date(n: dict[str, Any]) -> str:
        return str(n.get("date") or n.get("created_at") or n.get("decided_at") or "")

    def _pick(lbl: str, k: int) -> list[dict[str, Any]]:
        items = [n for n in nodes if _label(n).lower() == lbl]
        items.sort(key=_date, reverse=True)
        out = []
        for n in items[:k]:
            out.append({"name": n.get("name") or n.get("title") or n.get("id"),
                        "status": n.get("status"), "date": _date(n)[:10] or None})
        return out

    return {
        "org_id": org_id, "name": _org_name(org_id),
        "brain": _org_brain_summary(org_id),
        "recent_decisions": _pick("decision", 5),
        "recent_tasks": _pick("task", 5),
        "projects": _pick("project", 8),
        "people_count": sum(1 for n in nodes if _label(n).lower() == "person"),
    }


def _holding_rollup(h: dict[str, Any]) -> dict[str, Any]:
    """Кросс-корп сводка по холдингу (детерминированная, без LLM): тоталы,
    таблица по корпорациям и структурные сигналы («X из N …»)."""
    per_corp: list[dict[str, Any]] = []
    for oid in (h.get("org_ids") or []):
        rep = _org_report(oid)
        projects = rep.get("projects") or []
        in_progress = sum(
            1 for p in projects
            if str(p.get("status") or "").lower() in ("in_progress", "в работе", "active"))
        per_corp.append({
            "org_id": oid, "name": rep.get("name"),
            "nodes": rep.get("brain", {}).get("nodes_total", 0),
            "people": rep.get("people_count", 0),
            "decisions": len(rep.get("recent_decisions") or []),
            "tasks": len(rep.get("recent_tasks") or []),
            "projects": len(projects),
            "projects_in_progress": in_progress,
        })

    totals = {
        "corporations": len(per_corp),
        "nodes": sum(c["nodes"] for c in per_corp),
        "people": sum(c["people"] for c in per_corp),
        "projects": sum(c["projects"] for c in per_corp),
        "projects_in_progress": sum(c["projects_in_progress"] for c in per_corp),
    }

    signals: list[dict[str, Any]] = []
    empty = [c["name"] for c in per_corp if c["nodes"] == 0]
    if empty:
        signals.append({"kind": "empty_brain",
                        "text": f"{len(empty)} из {len(per_corp)} корпораций без данных — нужна синхронизация",
                        "orgs": empty})
    active = [c for c in per_corp if c["projects_in_progress"] > 0]
    if active:
        signals.append({"kind": "active_projects",
                        "text": f"проекты в работе есть в {len(active)} из {len(per_corp)} корпораций "
                                f"(всего {totals['projects_in_progress']})"})
    if per_corp:
        top = max(per_corp, key=lambda c: c["nodes"])
        if top["nodes"] > 0:
            signals.append({"kind": "largest",
                            "text": f"крупнейший мозг: {top['name']} ({top['nodes']} узлов)"})

    return {"id": h["id"], "name": h.get("name"),
            "totals": totals, "per_corp": per_corp, "signals": signals}


@get("/holdings/{holding_id:str}/rollup", status_code=200)
async def holdings_rollup(
    holding_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Кросс-корп сводка по холдингу (Слой 3): тоталы + таблица + сигналы."""
    caller = _caller(authorization)
    if not holding_store.is_owner(holding_id, caller):
        raise HTTPException(status_code=403, detail="not the holding owner")
    h = holding_store.get_holding(holding_id)
    if not h:
        raise HTTPException(status_code=404, detail="holding not found")
    return _holding_rollup(h)


@get("/holdings/available-orgs", status_code=200)
async def holdings_available_orgs(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Корпорации, которые можно добавить: где вызывающий — основатель
    (created_by), и которые ещё не в его холдингах."""
    caller = _caller(authorization)
    attached = set()
    for h in holding_store.get_holdings_for_owner(caller):
        attached.update(h.get("org_ids") or [])
    orgs = []
    for o in org_store.list_orgs():
        if o.get("created_by") == caller and o.get("org_id") not in attached:
            orgs.append({"org_id": o["org_id"], "name": o.get("name") or o["org_id"]})
    return {"orgs": orgs}


@get("/holdings/{holding_id:str}/orgs/{org_id:str}/report", status_code=200)
async def holdings_org_report(
    holding_id: str,
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Отчёт по корпорации (детерминированный, из орг-мозга)."""
    caller = _caller(authorization)
    _require_owner_of_org(caller, holding_id, org_id)
    _require_full_policy(holding_id, org_id)
    from backend.core.observability import audit_log
    await audit_log.emit(action="holding.org_report.read", user_id=caller,
                         resource=f"org:{org_id}",
                         metadata={"holding_id": holding_id})
    return _org_report(org_id)


@post("/holdings/{holding_id:str}/orgs/{org_id:str}/ask", status_code=200)
async def holdings_org_ask(
    holding_id: str,
    org_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Вопрос к данным корпорации (read-only). Поиск идёт по ОРГ-мозгу этой
    корпорации; ответ — через тот же enhanced search. Доступ только владельцу,
    действие аудируется. Записи в мозг корпорации не происходит."""
    caller = _caller(authorization)
    _require_owner_of_org(caller, holding_id, org_id)
    _require_full_policy(holding_id, org_id)
    question = ((data or {}).get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")

    # Аудит доступа директора к данным корпорации (never-raise).
    try:
        from backend.core.auth.resource_permissions import audit
        await audit(actor_id=caller, action="holding_ask",
                    resource_type="org", resource_id=org_id,
                    detail={"holding_id": holding_id, "q": question[:200]})
    except Exception:
        logger.debug("holding_ask audit skipped", exc_info=True)

    try:
        from backend.core.search.enhanced_search_orchestrator import (
            EnhancedSearchOrchestrator,
        )
        from backend.core.search.hybrid_search_orchestrator import (
            get_hybrid_search_orchestrator,
        )
        from backend.core.llm.router import LLMRouter
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import org_vector_path
        from backend.core.store.vector_indexer import VectorIndexer

        # read-only билдер на ОРГ-граф (как merged-view, но только корпорация)
        gb = GraphBuilder(use_networkx=True, graph_storage_path=org_graph_path(org_id))
        await gb.connect()
        vi = VectorIndexer(storage_path=org_vector_path(org_id), namespace=org_id)
        await vi.connect()
        hybrid = get_hybrid_search_orchestrator(vector_indexer=vi, graph_builder=gb, user_id=org_id)
        engine = EnhancedSearchOrchestrator(hybrid_search=hybrid, llm_router=LLMRouter())
        res = await engine.search(query=question, user_id=org_id, search_depth="standard")
        return {"org_id": org_id, "name": _org_name(org_id),
                "answer": getattr(res, "answer", str(res)),
                "sources": getattr(res, "sources", []) or []}
    except Exception as e:  # noqa: BLE001
        logger.error("holding_ask failed (%s): %s", org_id, e)
        return {"org_id": org_id, "answer": None,
                "error": "не удалось выполнить запрос к корпорации"}


@get("/holdings/{holding_id:str}/overview", status_code=200)
async def holdings_overview(
    holding_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Карточки корпораций: имя + сводка мозга каждой (read-only владельца)."""
    caller = _caller(authorization)
    if not holding_store.is_owner(holding_id, caller):
        raise HTTPException(status_code=403, detail="not the holding owner")
    h = holding_store.get_holding(holding_id)
    if not h:
        raise HTTPException(status_code=404, detail="holding not found")
    cards = []
    for oid in (h.get("org_ids") or []):
        cards.append({"org_id": oid, "name": _org_name(oid),
                      "brain": _org_brain_summary(oid)})
    return {"id": holding_id, "name": h.get("name"), "corporations": cards}


router = Router(
    path="",
    route_handlers=[
        holdings_mine,
        holdings_available_orgs,
        holdings_create,
        holdings_attach_org,
        holdings_detach_org,
        holdings_overview,
        holdings_rollup,
        holdings_org_report,
        holdings_org_ask,
    ],
    tags=["Holdings"],
)
