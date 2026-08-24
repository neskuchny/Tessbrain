# -*- coding: utf-8 -*-
"""
Сбор сигналов компании для заземления совета «Гида».

Берём то, что и так собирается со встреч (граф знаний пользователя): сколько
встреч/задач/людей/проектов, сколько открытых и ПРОСРОЧЕННЫХ задач, и аномалии
(«что не так»). Эти сигналы советник подмешивает, чтобы предлагать под реальную
ситуацию: «у вас N просроченных задач → включите напоминания», а не общими
словами. Best-effort, никогда не raises; пустой граф → {}.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

_DONE = {"done", "completed", "closed", "resolved", "finished", "выполнено", "закрыт"}


async def collect_company_signals(user_id: str) -> dict[str, Any]:
    """Компактные сигналы по графу пользователя. {} если графа нет."""
    if not user_id:
        return {}
    gb = None
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if not (gb.connected and gb.nx_graph):
            return {}

        counts: dict[str, int] = {}
        open_tasks = 0
        overdue = 0
        today = date.today().isoformat()
        for _nid, d in gb.nx_graph.nodes(data=True):
            label = str(d.get("_label") or d.get("label") or "").strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
            if label == "Task":
                status = str(d.get("status") or d.get("state") or "").lower()
                if status not in _DONE:
                    open_tasks += 1
                    due = str(d.get("deadline") or d.get("due_date") or "")[:10]
                    if due and due < today:
                        overdue += 1

        signals: dict[str, Any] = {
            "встреч в памяти": counts.get("Meeting", 0),
            "задач": counts.get("Task", 0),
            "открытых задач": open_tasks,
            "просроченных задач": overdue,
            "людей": counts.get("Person", 0),
            "проектов": counts.get("Project", 0),
            "решений": counts.get("Decision", 0),
        }

        # Аномалии — «что не так» (та же логика, что /meetflow/anomalies).
        try:
            from backend.core.sleep.anomaly_detector import AnomalyDetector
            data = await AnomalyDetector(gb).detect_all()
            anom = [f"{cat}: {len(items)}"
                    for cat, items in (data or {}).items()
                    if isinstance(items, list) and items]
            if anom:
                signals["аномалии (что не так)"] = "; ".join(anom[:6])
        except Exception as e:
            logger.debug("anomaly detect skipped: %s", e)

        return {k: v for k, v in signals.items() if v not in (0, "", None)}
    except Exception as e:
        logger.debug("signals collect failed: %s", e)
        return {}
    finally:
        if gb is not None:
            try:
                await gb.close(save=False)
            except Exception:
                pass


__all__ = ["collect_company_signals"]
