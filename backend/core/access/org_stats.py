# -*- coding: utf-8 -*-
"""Статистика орг-мозга через призму прав зрителя (экран «Компания целиком»).

Каждый видит цифры СВОЕГО охвата (те же правила, что merged view: уровни +
отделы + свои узлы) + честное «всего в мозге компании N узлов, вам видно M».
Генеральный (founder, уровень 5) видит всё. Чистая функция — тестируется без
файлов/роутов."""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from backend.core.access.levels import node_visible_to


def org_brain_stats(org_nodes: list[dict], *, user_id: str,
                    max_level: int,
                    viewer_department: Optional[str]) -> dict[str, Any]:
    """Args: org_nodes — список узлов орг-графа (dict с _label/department/
    access_level/…). Returns: total/visible + разбивки видимого по типам,
    отделам, уровням + сколько скрыто."""
    total = 0
    visible = 0
    by_label: Counter = Counter()
    by_department: Counter = Counter()
    by_level: Counter = Counter()
    for node in org_nodes or []:
        if not isinstance(node, dict):
            continue
        total += 1
        if not node_visible_to(node, user_id=user_id, max_level=max_level,
                               viewer_department=viewer_department):
            continue
        visible += 1
        by_label[str(node.get("_label") or "Unknown")] += 1
        dept = str(node.get("department") or "").strip()
        by_department[dept or "без отдела"] += 1
        lvl = node.get("access_level")
        by_level[str(lvl if lvl is not None else 2)] += 1
    return {
        "total_nodes": total,
        "visible_nodes": visible,
        "hidden_nodes": total - visible,
        "by_type": dict(by_label.most_common(12)),
        "by_department": dict(by_department.most_common(12)),
        "by_level": dict(sorted(by_level.items())),
    }
