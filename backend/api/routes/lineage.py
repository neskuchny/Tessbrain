# -*- coding: utf-8 -*-
"""Data lineage API (P8) — чтение трассы происхождения узла.

Read-only эндпоинт для UI-визуализации трассы (P7 кладёт `_lineage`
на узлы/связи; здесь — чтение). Никогда не raises — при любой
проблеме возвращает {found: false, ...}.
"""
import logging
from typing import Any, Dict, Optional

from litestar import Router, get
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _extract_lineage(props: Dict[str, Any]) -> Dict[str, Any]:
    """Прочитать lineage из props через чистый P7-модуль."""
    try:
        from backend.core.lineage.lineage import get_lineage

        rec = get_lineage(props or {})
        return {
            "lineage": rec.to_dict(),
            "trace": rec.history,
            "has_lineage": bool(rec.created_by or rec.history),
        }
    except Exception as exc:
        logger.debug(f"lineage extract failed: {exc}")
        return {"lineage": {}, "trace": [], "has_lineage": False}


@get("/lineage/trace")
async def lineage_trace(
    node_id: str = Parameter(query="node_id"),
    user_id: str = Parameter(query="user_id"),
) -> Dict[str, Any]:
    """Трасса происхождения узла по его id.

    Возвращает {found, node_id, label, lineage, trace, backend}.
    Изоляция по user_id (как в graph-эндпоинтах).
    """
    base = {
        "found": False,
        "node_id": node_id,
        "label": None,
        "lineage": {},
        "trace": [],
        "backend": None,
    }
    if not node_id or not user_id:
        base["error"] = "node_id and user_id are required"
        return base

    try:
        # Wave 2.4: federated read — lineage показывает связи и через
        # org-узлы (например, decision коллеги, ссылающийся на эту task).
        from backend.core.store.graph_view import merged_graph_view_for_user

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)

        try:
            if gb.driver:
                base["backend"] = "neo4j"
                from neo4j import GraphDatabase

                sync_driver = GraphDatabase.driver(
                    gb.uri, auth=(gb.user, gb.password)
                )
                with sync_driver.session() as session:
                    res = session.run(
                        """
                        MATCH (n) WHERE n.id = $nid
                        AND (n.user_id = $uid OR n.tenant_id = $uid)
                        RETURN labels(n) as labels, properties(n) as props
                        LIMIT 1
                        """,
                        {"nid": node_id, "uid": user_id},
                    )
                    rec = res.single()
                sync_driver.close()
                if rec:
                    props = dict(rec["props"] or {})
                    # Neo4j мог сохранить _lineage как JSON-строку
                    _coerce_lineage_json(props)
                    base.update(found=True,
                                label=(rec["labels"] or [None])[0],
                                **_extract_lineage(props))
            elif gb.nx_graph is not None:
                base["backend"] = "networkx"
                if gb.nx_graph.has_node(node_id):
                    data = gb.nx_graph.nodes[node_id]
                    node_uid = data.get("user_id") or data.get("owner_id")
                    if node_uid and node_uid != user_id:
                        base["error"] = "forbidden"
                        return base
                    base.update(
                        found=True,
                        label=data.get("_label", data.get("label")),
                        **_extract_lineage(dict(data)),
                    )
        finally:
            await gb.close()
    except Exception as exc:
        logger.warning(f"lineage_trace failed: {exc}")
        base["error"] = "lookup failed"

    return base


def _coerce_lineage_json(props: Dict[str, Any]) -> None:
    """Neo4j хранит вложенные maps как JSON-строки — разворачиваем."""
    raw = props.get("_lineage")
    if isinstance(raw, str):
        try:
            import json as _json

            props["_lineage"] = _json.loads(raw)
        except Exception:
            props.pop("_lineage", None)


def _pipeline_of(props: Dict[str, Any]) -> str:
    """pipeline_id узла: из _lineage или плоского зеркала. Безопасно."""
    try:
        from backend.core.lineage.lineage import get_lineage

        pid = get_lineage(props or {}).pipeline_id
        if pid:
            return str(pid)
    except Exception:
        pass
    flat = (props or {}).get("pipeline_id")
    return str(flat) if flat else ""


_MAX_OVERLAY_NODES = 300


@get("/lineage/pipeline")
async def lineage_pipeline(
    node_id: str = Parameter(query="node_id"),
    user_id: str = Parameter(query="user_id"),
) -> Dict[str, Any]:
    """Подграф одного pipeline-прогона для overlay на графе.

    По узлу определяем его `pipeline_id` и возвращаем все узлы+связи,
    рождённые тем же прогоном. Read-only, never-raises, изоляция по
    user_id, bounded (_MAX_OVERLAY_NODES).
    """
    out: Dict[str, Any] = {
        "found": False,
        "node_id": node_id,
        "pipeline_id": "",
        "nodes": [],
        "edges": [],
        "history": [],
        "backend": None,
        "truncated": False,
    }
    if not node_id or not user_id:
        out["error"] = "node_id and user_id are required"
        return out

    try:
        # Wave 2.4: federated read — lineage показывает связи и через
        # org-узлы (например, decision коллеги, ссылающийся на эту task).
        from backend.core.store.graph_view import merged_graph_view_for_user

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            if gb.driver:
                out["backend"] = "neo4j"
                from neo4j import GraphDatabase

                sync_driver = GraphDatabase.driver(
                    gb.uri, auth=(gb.user, gb.password)
                )
                with sync_driver.session() as session:
                    seed = session.run(
                        """
                        MATCH (n) WHERE n.id = $nid
                        AND (n.user_id = $uid OR n.tenant_id = $uid)
                        RETURN properties(n) as props LIMIT 1
                        """,
                        {"nid": node_id, "uid": user_id},
                    ).single()
                    if seed:
                        sp = dict(seed["props"] or {})
                        _coerce_lineage_json(sp)
                        pid = _pipeline_of(sp)
                        out["pipeline_id"] = pid
                        out["history"] = _extract_lineage(sp)["trace"]
                        if pid:
                            rows = session.run(
                                """
                                MATCH (a)-[r]->(b)
                                WHERE a.pipeline_id = $pid
                                  AND b.pipeline_id = $pid
                                  AND (a.user_id = $uid OR a.tenant_id = $uid)
                                RETURN a.id as fa, labels(a)[0] as la,
                                       b.id as fb, labels(b)[0] as lb,
                                       type(r) as rt
                                LIMIT $lim
                                """,
                                {"pid": pid, "uid": user_id,
                                 "lim": _MAX_OVERLAY_NODES * 4},
                            )
                            seen: Dict[str, str] = {}
                            for rec in rows:
                                seen[rec["fa"]] = rec["la"] or "Node"
                                seen[rec["fb"]] = rec["lb"] or "Node"
                                out["edges"].append({
                                    "from": rec["fa"], "to": rec["fb"],
                                    "label": rec["rt"],
                                })
                            out["nodes"] = [
                                {"id": i, "label": l}
                                for i, l in list(seen.items())[
                                    :_MAX_OVERLAY_NODES]
                            ]
                            out["found"] = True
                sync_driver.close()
            elif gb.nx_graph is not None:
                out["backend"] = "networkx"
                if gb.nx_graph.has_node(node_id):
                    data = dict(gb.nx_graph.nodes[node_id])
                    nuid = data.get("user_id") or data.get("owner_id")
                    if nuid and nuid != user_id:
                        out["error"] = "forbidden"
                        return out
                    pid = _pipeline_of(data)
                    out["pipeline_id"] = pid
                    out["history"] = _extract_lineage(data)["trace"]
                    if pid:
                        members: Dict[str, str] = {}
                        for nid, nd in gb.nx_graph.nodes(data=True):
                            n_uid = nd.get("user_id") or nd.get("owner_id")
                            if n_uid and n_uid != user_id:
                                continue
                            if _pipeline_of(dict(nd)) == pid:
                                members[nid] = nd.get(
                                    "_label", nd.get("label", "Node"))
                            if len(members) >= _MAX_OVERLAY_NODES:
                                out["truncated"] = True
                                break
                        for s, tgt, ed in gb.nx_graph.edges(data=True):
                            if s in members and tgt in members:
                                out["edges"].append({
                                    "from": s, "to": tgt,
                                    "label": ed.get("_type",
                                                     ed.get("type", "")),
                                })
                        out["nodes"] = [
                            {"id": i, "label": l}
                            for i, l in members.items()
                        ]
                        out["found"] = True
        finally:
            await gb.close()
    except Exception as exc:
        logger.warning(f"lineage_pipeline failed: {exc}")
        out["error"] = "lookup failed"

    return out


router = Router(
    path="",
    guards=[enforce_user_id_matches_token],
    route_handlers=[lineage_trace, lineage_pipeline],
    tags=["Lineage"],
)
