# -*- coding: utf-8 -*-
"""Линза кластеров: закономерности компании снизу вверх (community detection).

Все наши глобальные срезы (снапшот компании, доменные снапшоты, deep
synthesis) работают СВЕРХУ ВНИЗ — видят то, что мы заранее решили видеть.
Эта линза — противоположность: Louvain-разбиение графа знаний находит
плотные кластеры узлов, которые никто не курировал («вот эти 4 клиента,
2 сотрудника, 3 задачи и продукт образуют связку»). Это единственная
техника из Microsoft GraphRAG, которой у нас не было; остальной их
конвейер (extract/graph/hybrid/multi-hop/night-update) в системе давно.

Честность по построению:
- СОСТАВ кластера — математика (Louvain по реальным рёбрам), не LLM;
- LLM только ИМЕНУЕТ кластер и формулирует, чем связка интересна, — ему
  запрещено упоминать узлы вне списка, интерпретация помечается как
  гипотеза;
- Meeting-узлы участвуют в разбиении (через них идут связи), но в состав
  не выводятся — иначе всё склеилось бы в один ком «все были на встречах»;
- fingerprint графа: не изменился — не пересобираем и не жжём LLM.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MIN_SIZE = 3          # минимум сущностей (без Meeting) в кластере
_MAX_COMMUNITIES = 12  # сколько кластеров храним (по размеру)
_LLM_TOP = 8           # скольким старшим кластерам даём LLM-описание
_MEMBER_CAP = 40       # членов кластера в хранении/промпте

_ENTITY_LABELS = {"Person", "Client", "Product", "Project", "Task",
                  "Department", "Team", "Process", "Resource", "Decision",
                  "Risk", "Goal", "Company"}


def lens_enabled() -> bool:
    return os.environ.get("ENABLE_COMMUNITY_LENS", "").strip().lower() in (
        "1", "on", "true", "yes")


def _store_dir() -> Path:
    p = Path("data/communities")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
    return _store_dir() / f"{safe}.json"


def _load(user_id: str) -> Dict[str, Any]:
    try:
        return json.loads(_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(user_id: str, data: Dict[str, Any]) -> None:
    tmp = _path(user_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _path(user_id))


def list_communities(user_id: str) -> Dict[str, Any]:
    data = _load(user_id)
    return {"generated_at": data.get("generated_at"),
            "stats": data.get("stats") or {},
            "communities": data.get("communities") or []}


# ── Детекция (математика, без LLM) ───────────────────────────────────────

def _node_meta(d: Dict[str, Any]) -> Optional[Dict[str, str]]:
    label = str(d.get("_label") or d.get("label") or "").strip()
    name = str(d.get("name") or d.get("title") or "").strip()
    if not name:
        return None
    return {"name": name, "label": label}


def detect_from_nx(nx_g) -> Dict[str, Any]:
    """Louvain-кластеры на неориентированной проекции. Чистая функция —
    тестируется на синтетическом графе. Возврат: {fingerprint, communities}.
    """
    import networkx as nx
    from networkx.algorithms.community import louvain_communities

    und = nx.Graph()
    meta: Dict[str, Dict[str, str]] = {}
    for nid, d in nx_g.nodes(data=True):
        m = _node_meta(d or {})
        if m is None:
            continue
        meta[str(nid)] = m
        und.add_node(str(nid))
    edge_types: Dict[tuple, str] = {}
    for a, b, d in nx_g.edges(data=True):
        a, b = str(a), str(b)
        if a in meta and b in meta and a != b:
            und.add_edge(a, b)
            edge_types[(a, b)] = str((d or {}).get("_type")
                                     or (d or {}).get("type") or "")

    fingerprint = hashlib.sha1(
        (f"{und.number_of_nodes()}:{und.number_of_edges()}:"
         + ",".join(sorted(list(und.nodes))[:500])).encode()).hexdigest()[:16]

    if und.number_of_edges() == 0:
        return {"fingerprint": fingerprint, "communities": []}

    # seed фиксирован: пересборка без изменений графа даёт те же кластеры
    parts = louvain_communities(und, seed=42)
    communities: List[Dict[str, Any]] = []
    for part in parts:
        entity_members, meetings = [], 0
        for nid in part:
            m = meta.get(nid) or {}
            if m.get("label") == "Meeting":
                meetings += 1
                continue
            if m.get("label") in _ENTITY_LABELS or not m.get("label"):
                entity_members.append({"id": nid, "name": m.get("name"),
                                       "label": m.get("label") or "Entity"})
        if len(entity_members) < _MIN_SIZE:
            continue
        # связность и типы связей внутри кластера
        internal = und.subgraph(part)
        types: Dict[str, int] = {}
        for a, b in internal.edges():
            t = edge_types.get((a, b)) or edge_types.get((b, a)) or ""
            if t:
                types[t] = types.get(t, 0) + 1
        # мосты: самые связанные узлы кластера (реальная степень)
        deg = sorted(((nid, internal.degree(nid)) for nid in part
                      if nid in meta and meta[nid].get("label") != "Meeting"),
                     key=lambda kv: -kv[1])
        bridges = [{"name": meta[nid]["name"], "label": meta[nid]["label"],
                    "degree": d} for nid, d in deg[:3] if d > 0]
        entity_members.sort(key=lambda m: m["name"])
        by_label: Dict[str, int] = {}
        for m in entity_members:
            by_label[m["label"]] = by_label.get(m["label"], 0) + 1
        communities.append({
            "size": len(entity_members),
            "members": entity_members[:_MEMBER_CAP],
            "members_total": len(entity_members),
            "by_label": by_label,
            "meetings_involved": meetings,
            "internal_edges": internal.number_of_edges(),
            "edge_types": dict(sorted(types.items(),
                                      key=lambda kv: -kv[1])[:6]),
            "bridges": bridges,
        })
    communities.sort(key=lambda c: -c["size"])
    communities = communities[:_MAX_COMMUNITIES]
    for i, c in enumerate(communities):
        c["id"] = f"c{i + 1}"
    return {"fingerprint": fingerprint, "communities": communities}


# ── Именование кластеров (LLM, с запретом выдумывать) ────────────────────

def _community_card(c: Dict[str, Any]) -> str:
    lines = [f"КЛАСТЕР {c['id']} ({c['size']} сущностей, "
             f"{c['internal_edges']} связей, встреч рядом: "
             f"{c['meetings_involved']})"]
    for m in c["members"][:_MEMBER_CAP]:
        lines.append(f"- [{m['label']}] {m['name']}")
    if c.get("edge_types"):
        lines.append("Типы связей: " + ", ".join(
            f"{t}×{n}" for t, n in c["edge_types"].items()))
    if c.get("bridges"):
        lines.append("Самые связанные узлы: " + ", ".join(
            f"{b['name']} ({b['degree']})" for b in c["bridges"]))
    return "\n".join(lines)


async def _summarize(user_id: str, communities: List[Dict[str, Any]]) -> int:
    if not communities:
        return 0
    from backend.core.llm.router import get_llm_router
    llm = get_llm_router()
    cards = "\n\n".join(_community_card(c) for c in communities[:_LLM_TOP])
    prompt = (
        "Ниже кластеры графа знаний компании, найденные МАТЕМАТИЧЕСКИ "
        "(Louvain по реальным связям) — их состав не обсуждается. Твоя "
        "задача — только назвать каждый кластер и объяснить, чем эта "
        "связка может быть важна руководителю.\n"
        "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
        "1. Упоминай ТОЛЬКО сущности из списка кластера. Ни одного "
        "имени/продукта/клиента извне.\n"
        "2. Состав — факт; твоя интерпретация — гипотеза. В поле "
        "significance формулируй как гипотезу («похоже, что…», «возможно…»).\n"
        "3. Не выдумывай события и цифры.\n"
        'Ответь ТОЛЬКО JSON: {"summaries": [{"id": "c1", '
        '"name": "короткое имя кластера (3-6 слов)", '
        '"pattern": "что фактически связывает эти узлы (по составу и типам '
        'связей)", "significance": "чем это может быть важно (гипотеза)", '
        '"watch_out": "на что взглянуть руководителю (1 фраза)"}]}\n\n'
        + cards)
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.3)
    except Exception as e:
        logger.warning("communities: LLM summary failed: %s", e)
        return 0
    by_id = {c["id"]: c for c in communities}
    named = 0
    known_names = {m["name"].lower() for c in communities[:_LLM_TOP]
                   for m in c["members"]}
    for s in ((data or {}).get("summaries") or []):
        if not isinstance(s, dict):
            continue
        c = by_id.get(str(s.get("id") or ""))
        if c is None:
            continue
        c["summary"] = {
            "name": str(s.get("name") or "")[:80],
            "pattern": str(s.get("pattern") or "")[:400],
            "significance": str(s.get("significance") or "")[:400],
            "watch_out": str(s.get("watch_out") or "")[:200],
        }
        named += 1
    # лёгкая проверка на выдуманные имена собственные в pattern (грубая:
    # слова с заглавной, которых нет среди членов, — снимаем summary целиком
    # не будем: слишком много ложных срабатываний на русской морфологии).
    _ = known_names
    return named


# ── Neo4j → networkx (инсталляции с Neo4j-бэкендом) ─────────────────────

async def _nx_from_neo4j(gb) -> Any:
    """Собрать networkx-проекцию из Neo4j: узлы по меткам (tenant_context
    org_or_user — как федеративное чтение людей), рёбра между ними одним
    Cypher. Louvain дальше одинаковый для обоих бэкендов."""
    import networkx as nx
    g = nx.MultiDiGraph()
    ids: set = set()
    for label in sorted(_ENTITY_LABELS | {"Meeting"}):
        try:
            nodes = await gb.get_all_nodes_async(
                label=label, limit=5000, tenant_id=None, strict_tenant=False)
        except Exception:
            logger.debug("communities: neo4j nodes %s unavailable", label,
                         exc_info=True)
            continue
        for n in nodes or []:
            nid = str(n.get("id") or "").strip()
            name = str(n.get("name") or n.get("title") or "").strip()
            if nid and name:
                g.add_node(nid, _label=label, name=name)
                ids.add(nid)
    driver = getattr(gb, "driver", None)
    if driver is None or not ids:
        return g
    try:
        # AsyncDriver — только async with / async for (см. graph_export)
        async with driver.session() as s:
            result = await s.run(
                "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
                "RETURN a.id AS s, b.id AS t, type(r) AS ty LIMIT 20000",
                {"ids": list(ids)})
            async for rec in result:
                g.add_edge(str(rec["s"]), str(rec["t"]),
                           _type=str(rec["ty"] or ""))
    except Exception:
        logger.warning("communities: neo4j edges unavailable", exc_info=True)
    return g


# ── Полный цикл ──────────────────────────────────────────────────────────

async def build_communities(user_id: str, *, with_llm: bool = True,
                            force: bool = False) -> Dict[str, Any]:
    """Пересобрать кластеры: merged-граф → Louvain → (LLM-имена) → стор.

    Граф не изменился (fingerprint совпал) и не force → отдаём сохранённое
    без пересборки и без LLM. Работает на обоих бэкендах: networkx напрямую,
    Neo4j — через проекцию (_nx_from_neo4j)."""
    from backend.core.store.graph_view import merged_graph_view_for_user

    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        nx_g = getattr(gb, "nx_graph", None)
        if nx_g is None:
            if getattr(gb, "driver", None) is None:
                return {"status": "error",
                        "message": "граф недоступен (ни networkx, ни Neo4j)"}
            nx_g = await _nx_from_neo4j(gb)
        detected = detect_from_nx(nx_g)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass

    prev = _load(user_id)
    if (not force and prev.get("fingerprint") == detected["fingerprint"]
            and prev.get("communities")):
        return {"status": "success", "unchanged": True,
                "communities": prev["communities"],
                "generated_at": prev.get("generated_at"),
                "stats": prev.get("stats") or {}}

    communities = detected["communities"]
    named = 0
    if with_llm and communities:
        named = await _summarize(user_id, communities)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": detected["fingerprint"],
        "stats": {"communities": len(communities), "named": named,
                  "largest": communities[0]["size"] if communities else 0},
        "communities": communities,
    }
    _save(user_id, data)
    return {"status": "success", "unchanged": False,
            "communities": communities,
            "generated_at": data["generated_at"], "stats": data["stats"]}


def communities_context(user_id: str, *, cap: int = 6000) -> str:
    """Кластеры текстом — для подмешивания в глобальные синтезы/отчёты.
    Пусто, если линза ещё не собиралась."""
    data = _load(user_id)
    comms = data.get("communities") or []
    if not comms:
        return ""
    lines = ["КЛАСТЕРЫ КОМПАНИИ (найдены математически по связям графа; "
             "имена и значимость — гипотезы LLM):"]
    for c in comms:
        s = c.get("summary") or {}
        title = s.get("name") or f"Кластер {c['id']}"
        members = ", ".join(m["name"] for m in c["members"][:8])
        lines.append(f"- {title} ({c['size']} сущностей): {members}"
                     + (f". {s.get('significance')}" if s.get("significance")
                        else ""))
    return "\n".join(lines)[:cap]
