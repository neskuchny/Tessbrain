# -*- coding: utf-8 -*-
"""
Insight Collector — единый сборщик инсайтов по всем продуцентам.

Используется ДВУМЯ потребителями:
1. GET /meetflow/insights (страница) — live-скан при открытии;
2. ПРОАКТИВНЫЙ ЦИКЛ (scheduler._proactive_insight_scan) — система сама,
   без участия человека, периодически прогоняет сканеры по каждому
   пользователю и складывает НОВОЕ в персистентную ленту. Идемпотентность
   по содержимому (stable_insight_id) гарантирует: повторный прогон не
   дублирует, в ленте появляется только реально новая информация — это
   и есть «новые исследования появляются сами».

Секции (каждая best-effort, сбой одной не валит остальные):
- NightProcessor: эвристики по графу (связи, паттерны, тренды, риски);
- BlindSpotScanner: Pattern/Contradiction-узлы графа + СИГНАТУРЫ из текстов
  встреч (signal_library) — повторяющиеся «болевые» формулировки;
- CrossDepartmentDesync: рассинхроны целей отделов;
- AnomalyDetector: застрявшие задачи, дедлайны, изолированные люди.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def collect_and_store_insights(user_id: str) -> Dict[str, Any]:
    """Прогнать все сканеры по пользователю и сохранить новое в ленту.

    Возвращает {fresh: int, added: int, sources: {...}, stats: {...}}.
    added < fresh означает, что часть находок уже была в ленте (норма)."""
    from backend.core.sleep.insight_store import InsightStore, stable_insight_id
    from backend.core.sleep.night_processor import NightProcessor
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import (
        graph_path_for_user,
        insights_path_for_user,
    )

    fresh: list = []
    stats: Dict[str, Any] = {}
    sources: Dict[str, int] = {}

    graph_builder = GraphBuilder(
        use_networkx=None,
        graph_storage_path=graph_path_for_user(user_id)
    )
    await graph_builder.connect()

    if graph_builder.connected and graph_builder.nx_graph:
        nodes = [data for _id, data in graph_builder.nx_graph.nodes(data=True)]

        # 1) NightProcessor (эвристики по графу, без LLM — быстрый скан)
        try:
            processor = NightProcessor(graph_builder=graph_builder,
                                       llm_router=None)
            result = await processor.run_night_processing()
            stats = result.to_dict().get("stats", {})
            for insight in result.insights:
                fresh.append({
                    "type": insight.insight_type.value,
                    "title": insight.title,
                    "description": insight.description,
                    "priority": insight.priority.value,
                    "confidence": insight.confidence,
                    "entities": insight.entities,
                    "actions": insight.actions,
                    "source": "night_processor",
                })
            sources["night_processor"] = len(fresh)
        except Exception as e:
            logger.warning(f"insight collector: night_processor failed: {e}")

        # 2) Слепые зоны: граф + сигнатуры из текстов встреч
        try:
            from backend.core.think.blind_spot_scanner import (
                BlindSpotScanner,
                proactive_insights,
            )
            from backend.core.think.signal_library import signals_to_episodes
            scanner = BlindSpotScanner()
            spots = scanner.scan_from_graph(nodes)
            meetings = [
                {"meeting_id": d.get("id") or d.get("meeting_id"),
                 "text": " ".join(str(d.get(k) or "") for k in
                                  ("title", "summary", "description", "content"))}
                for d in nodes
                if str(d.get("_label") or d.get("label") or "").lower() == "meeting"
            ]
            episodes = signals_to_episodes(meetings)
            if episodes:
                spots += scanner.scan(episodes=episodes)
            blind_dicts = [s.to_dict() for s in spots]

            # 3) Рассинхроны отделов
            desync_dicts: list = []
            try:
                from backend.core.think.cross_department_desync import (
                    dept_goals_from_graph_nodes,
                    find_desyncs,
                )
                desync_dicts = [d.to_dict() for d in
                                find_desyncs(dept_goals_from_graph_nodes(nodes))]
            except Exception as e:
                logger.warning(f"insight collector: desync failed: {e}")

            proactive = proactive_insights(blind_dicts, desync_dicts)
            for p in proactive:
                fresh.append({
                    "type": p.get("insight_type", "risk"),
                    "title": p.get("title", ""),
                    "description": p.get("description", ""),
                    "priority": p.get("priority", "medium"),
                    "confidence": p.get("confidence", 0.6),
                    "entities": [],
                    "actions": p.get("actions", []),
                    "source": "desync" if p.get("kind") == "desync" else "blind_spots",
                })
            sources["blind_spots+desync"] = len(proactive)
        except Exception as e:
            logger.warning(f"insight collector: blind spots failed: {e}")

        # 4) Аномалии
        try:
            from backend.core.sleep.anomaly_detector import AnomalyDetector
            detector = AnomalyDetector(graph_builder)
            anomalies = await detector.detect_all()
            for a in anomalies:
                d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
                fresh.append({
                    "type": "anomaly",
                    "title": d.get("title") or str(d.get("description", ""))[:120],
                    "description": d.get("description", ""),
                    "priority": d.get("severity", d.get("priority", "medium")),
                    "confidence": d.get("confidence", 0.7),
                    "entities": d.get("entities", []),
                    "actions": d.get("recommendations", d.get("actions", [])),
                    "source": "anomaly_detector",
                })
            sources["anomalies"] = len(anomalies)
        except Exception as e:
            logger.warning(f"insight collector: anomalies failed: {e}")

    # Сохраняем (идемпотентно по содержимому)
    added = 0
    new_ids: list = []
    for f in fresh:
        f.setdefault("insight_id", stable_insight_id(f))
    try:
        store = InsightStore(persist_path=insights_path_for_user(user_id))
        res = store.add(fresh)
        added = int(res.get("added", 0))
        new_ids = res.get("new_ids", [])
    except Exception as e:
        logger.warning(f"insight collector: store save failed: {e}")

    # OUTCOME-проверка («помогло ли»): сверяем старые инсайты (≥N дней) со
    # СВЕЖИМ сканом. Снова всплыл → persists (проблема не решена, поднимаем
    # в ранжировании); перестал → resolved. Структурно, без LLM. Дёшево —
    # делаем каждый прогон.
    outcomes = {}
    try:
        import os as _os
        fresh_ids = {f.get("insight_id") for f in fresh if f.get("insight_id")}
        outcomes = store.check_outcomes(
            fresh_ids,
            min_age_days=int(_os.getenv("OUTCOME_CHECK_DAYS", "7")))
        if outcomes.get("checked"):
            logger.info(f"🔁 Outcomes for {user_id[:8]}: {outcomes}")
    except Exception as e:
        logger.warning(f"outcome check failed: {e}")

    # ДОСТАВКА в Telegram (за флагом enable_proactive_telegram_delivery):
    # шлём ТОЛЬКО новые инсайты через reactive-гейт (анти-шум по confidence,
    # калибровка, анти-повтор по source_id). Не новое не доставляем повторно.
    delivered = await _maybe_deliver(user_id, fresh, new_ids)

    try:
        await graph_builder.close(save=False)
    except Exception:
        pass

    return {"fresh": len(fresh), "added": added, "delivered": delivered,
            "outcomes": outcomes, "sources": sources, "stats": stats,
            "live": fresh}


async def _maybe_deliver(user_id: str, fresh: list, new_ids: list) -> int:
    """Доставить НОВЫЕ инсайты в каналы (Telegram) через reactive-гейт.

    За флагом enable_proactive_telegram_delivery (default OFF — чтобы не
    спамить, пока канал не настроен). Гейт сам решает, что реально слать
    (confidence/калибровка/нагрузка) — мы лишь подаём только новое."""
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if not get_feature_flags().enable_proactive_telegram_delivery:
            return 0
    except Exception:
        return 0
    new_set = set(new_ids or [])
    to_deliver = [f for f in fresh if f.get("insight_id") in new_set]
    if not to_deliver:
        return 0
    try:
        from backend.core.reactive.recommendations import submit_night_insights
        # is_actionable нужен гейту — помечаем у тех, где есть actions/высокий приоритет
        for f in to_deliver:
            f.setdefault("is_actionable", bool(f.get("actions"))
                         or str(f.get("priority", "")).lower() in ("high", "critical"))
        res = await submit_night_insights(user_id=user_id, insights=to_deliver)
        return int(res.get("submitted", 0))
    except Exception as e:
        logger.warning(f"insight delivery failed for {user_id}: {e}")
        return 0
