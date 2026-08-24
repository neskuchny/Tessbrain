#!/usr/bin/env python3
# ruff: noqa: T201
"""Найти potential Person identity collisions в существующем графе
(issue #112 T-2).

Контекст: ДО правки graph_builder.py делал MERGE Person по name only,
поэтому два Максима в одном тенанте сливались в один node. Этот скрипт
ищет уже-сломанные Person'ы для ручного разделения админом.

Эвристики «подозрительной» личности:
  1. Person с >1 разной role (за разные periods)
  2. Person с >2 разных department
  3. Person участвует в встречах с >N разными участниками (выглядит как
     несколько людей с одним именем)

Использование:
  python scripts/find_identity_collisions.py \\
      --neo4j-uri bolt://localhost:7687 \\
      --neo4j-user neo4j --neo4j-password ... \\
      [--tenant-id t-A] [--output collisions.json]

  python scripts/find_identity_collisions.py \\
      --networkx-graph data/graph.json [--tenant-id t-A]

Output: JSON список кандидатов + админ-CSV для ручной проверки.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


def _score_collision_risk(person: Dict[str, Any]) -> str:
    """Эвристика: low/medium/high риск, что Person — на самом деле
    несколько разных людей слепленных в один node."""
    role_diversity = len(person.get("_distinct_roles", set()))
    dept_diversity = len(person.get("_distinct_departments", set()))
    coworker_count = len(person.get("_coworkers", set()))

    if role_diversity >= 3 or dept_diversity >= 2:
        return "high"
    if role_diversity == 2 and coworker_count >= 5:
        return "medium"
    if role_diversity == 2 or coworker_count >= 10:
        return "low"
    return "none"


def scan_networkx(graph_path: str, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """Скан экспортированного NetworkX-графа (json) на коллизии."""
    with open(graph_path, encoding="utf-8") as f:
        data = json.load(f)

    nodes = {n["id"]: n["data"] for n in data.get("nodes", [])
             if n.get("data", {}).get("_label") == "Person"}
    if tenant_id:
        nodes = {nid: nd for nid, nd in nodes.items()
                 if nd.get("tenant_id") == tenant_id}

    # Группируем Person'ов по normalized_name (без #suffix)
    persons_by_name: Dict[str, List[str]] = {}
    for nid, nd in nodes.items():
        base_name = (nd.get("normalized_name") or nd.get("name") or "").split("#")[0]
        if base_name:
            persons_by_name.setdefault(base_name, []).append(nid)

    suspicious: List[Dict[str, Any]] = []
    # Алгоритм: для каждого Person собираем встречи и сослуживцев
    edges = data.get("edges", [])
    coworkers_by_person: Dict[str, set[str]] = {}
    for edge in edges:
        if edge.get("data", {}).get("type") != "PARTICIPATED_IN":
            continue
        person_id = edge["source"]
        meeting_id = edge["target"]
        # Найдём всех остальных участников этой встречи
        for e2 in edges:
            if (e2.get("data", {}).get("type") == "PARTICIPATED_IN"
                    and e2["target"] == meeting_id
                    and e2["source"] != person_id):
                coworkers_by_person.setdefault(person_id, set()).add(e2["source"])

    for nid, nd in nodes.items():
        person_info = {
            "id": nid,
            "name": nd.get("name", ""),
            "role": nd.get("role", ""),
            "department": nd.get("department", ""),
            "tenant_id": nd.get("tenant_id", ""),
            "_distinct_roles": {nd.get("role", "")} if nd.get("role") else set(),
            "_distinct_departments": {nd.get("department", "")} if nd.get("department") else set(),
            "_coworkers": coworkers_by_person.get(nid, set()),
        }
        risk = _score_collision_risk(person_info)
        if risk in ("high", "medium", "low"):
            suspicious.append({
                "id": nid,
                "name": person_info["name"],
                "tenant_id": person_info["tenant_id"],
                "risk": risk,
                "role": person_info["role"],
                "department": person_info["department"],
                "coworker_count": len(person_info["_coworkers"]),
                "reason": _reason_for_risk(person_info, risk),
            })

    # Группа подозрительных: разные nodes под одним base_name
    for base_name, node_ids in persons_by_name.items():
        if len(node_ids) > 1:
            # Это уже #2-style разделение — проверим что у каждого
            # стоит needs_review
            for nid in node_ids:
                nd = nodes[nid]
                if not nd.get("needs_review") and not any(
                    s["id"] == nid for s in suspicious
                ):
                    suspicious.append({
                        "id": nid, "name": nd.get("name", ""),
                        "tenant_id": nd.get("tenant_id", ""),
                        "risk": "low",
                        "reason": f"sibling Persons with base_name='{base_name}' "
                                  f"(possible legacy MERGE collision)",
                    })

    return suspicious


def _reason_for_risk(person: Dict[str, Any], risk: str) -> str:
    if risk == "high":
        return (f"role diversity={len(person['_distinct_roles'])}, "
                f"dept diversity={len(person['_distinct_departments'])}")
    if risk == "medium":
        return (f"2 roles + {len(person['_coworkers'])} distinct coworkers — "
                f"possibly 2 different people")
    return (f"2 roles OR many coworkers ({len(person['_coworkers'])}) — "
            f"check manually")


async def scan_neo4j(
    uri: str, user: str, password: str, tenant_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Скан Neo4j-графа. Использует aiocounter — async через neo4j async driver."""
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        print("❌ neo4j package not installed", file=sys.stderr)
        return []

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    suspicious: List[Dict[str, Any]] = []
    try:
        async with driver.session() as session:
            # Запрос: Person'ы с подозрительно высоким разнообразием связей
            query = """
            MATCH (p:Person)
            WHERE $tenant IS NULL OR p.tenant_id = $tenant
            OPTIONAL MATCH (p)-[r:PARTICIPATED_IN]->(m:Meeting)
            WITH p,
                 COUNT(DISTINCT m) AS meeting_count,
                 COLLECT(DISTINCT r.role) AS roles_in_meetings,
                 [(p)-[:KNOWS|MENTIONED_BY|COWORKER]-(other) | other.id] AS connections
            WHERE meeting_count >= 2 AND size(roles_in_meetings) >= 2
            RETURN p.id AS id, p.name AS name, p.tenant_id AS tenant_id,
                   p.role AS role, p.department AS department,
                   meeting_count, size(roles_in_meetings) AS role_variety,
                   size(connections) AS connection_count
            ORDER BY role_variety DESC, meeting_count DESC
            LIMIT 200
            """
            result = await session.run(query, {"tenant": tenant_id})
            async for row in result:
                risk = "high" if row["role_variety"] >= 3 else "medium"
                suspicious.append({
                    "id": row["id"],
                    "name": row["name"] or "",
                    "tenant_id": row["tenant_id"] or "",
                    "risk": risk,
                    "role": row["role"] or "",
                    "department": row["department"] or "",
                    "meeting_count": row["meeting_count"],
                    "role_variety": row["role_variety"],
                    "reason": (
                        f"{row['meeting_count']} встреч с "
                        f"{row['role_variety']} разными ролями"
                    ),
                })
    finally:
        await driver.close()
    return suspicious


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--networkx-graph", help="JSON-файл с экспортированным графом")
    src.add_argument("--neo4j-uri", help="bolt://host:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="")
    parser.add_argument("--tenant-id", default=None,
                        help="фильтр по тенанту (рекомендуется на проде)")
    parser.add_argument("--output", default="-",
                        help="путь к JSON-output (- = stdout)")
    parser.add_argument("--admin-csv", default=None,
                        help="опц. CSV для админа на ручную проверку")
    args = parser.parse_args()

    if args.networkx_graph:
        suspicious = scan_networkx(args.networkx_graph, args.tenant_id)
    else:
        import asyncio
        suspicious = asyncio.run(scan_neo4j(
            args.neo4j_uri, args.neo4j_user, args.neo4j_password,
            args.tenant_id,
        ))

    summary = {
        "total_suspicious": len(suspicious),
        "by_risk": dict(Counter(s["risk"] for s in suspicious)),
        "tenant_id": args.tenant_id,
        "items": suspicious,
    }

    out = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"✅ Wrote {len(suspicious)} suspicious Persons → {args.output}")

    if args.admin_csv:
        with open(args.admin_csv, "w", encoding="utf-8") as f:
            f.write("id,name,tenant_id,risk,role,reason\n")
            for s in suspicious:
                f.write(f"{s['id']},{s['name']},{s['tenant_id']},"
                        f"{s['risk']},{s.get('role','')},{s['reason']}\n")
        print(f"✅ Wrote admin CSV → {args.admin_csv}")

    print(f"\n📊 Summary: {summary['by_risk']}")
    print("   Recommend: admin review high-risk Persons first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
