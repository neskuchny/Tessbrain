# -*- coding: utf-8 -*-
"""
Insights API — история ночных инсайтов проактивного консультанта
(docs/CAPABILITY_READINESS.md, B6).

Раньше NightInsights генерировались и пропадали (RAM + разовая отправка).
Теперь nightly_consolidation сохраняет их в per-user InsightStore — этот
роут отдаёт историю с фильтрами и помечает прочитанность. Только чтение
существующих данных — нового поведения системы не добавляет (без флага).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Controller, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _store(user_id: str):
    from backend.core.sleep.insight_store import InsightStore
    from backend.core.store.tenant_paths import insights_path_for_user
    return InsightStore(persist_path=insights_path_for_user(user_id))


# ── Дедуп-ревью (кластеры средней уверенности из ночной консолидации) ──────
# Пишутся _save_dedup_review в data/dedup_review/<uid>.json; здесь читаем их
# в очередь «Проверка памяти» и применяем решение человека.

def _dedup_review_path(user_id: str):
    from pathlib import Path
    return Path("data") / "dedup_review" / f"{user_id}.json"


def _sanitize_cluster(cluster: Dict[str, Any],
                      user_id: str = "") -> Dict[str, Any]:
    """Пере-применить АКТУАЛЬНЫЙ фильтр совпадения имён к сохранённому
    кластеру.

    Старые ночные прогоны (до правила «конфликт фамилий = разные люди»)
    записали кластеры, где к «Екатерина Постовалова» прицепились «Екатерина
    Кустова»/«Сабалова»/«Катя Кожемякина» — разные фамилии = разные люди.
    Файл ревью перезаписывается только следующей ночью, поэтому отсекаем
    несовпадающих здесь, при чтении: человек не видит и не подтверждает
    заведомо неверное слияние. Канон пересчитываем по актуальному рангу.
    """
    names = cluster.get("member_names") or []
    ids = cluster.get("member_ids") or []
    if len(names) < 2 or len(ids) != len(names):
        return cluster  # без парных id безопаснее не трогать
    try:
        from backend.core.store.entity_resolver import (
            _entity_matches, _keep_name_rank,
        )
    except Exception:
        return cluster
    etype = cluster.get("entity_type", "person")
    canonical = max(names, key=_keep_name_rank)
    # негативная память: пары, отклонённые человеком раньше, — вон из
    # кластера (файл ревью мог быть записан до отклонения)
    neg = set()
    if user_id:
        try:
            from backend.core.store.dedup_negative import negative_pairs_set
            neg = negative_pairs_set(user_id)
        except Exception:
            neg = set()

    def _ok(n: str) -> bool:
        if not _entity_matches(n, canonical, etype):
            return False
        if neg:
            from backend.core.store.dedup_negative import is_negative
            if is_negative(user_id, n, canonical, pairs=neg):
                return False
        return True

    kept = [(i, n) for i, n in zip(ids, names) if _ok(n)]
    if len(kept) == len(names):
        return cluster  # ничего не отсеклось
    out = dict(cluster)
    out["member_ids"] = [i for i, _ in kept]
    out["member_names"] = [n for _, n in kept]
    out["canonical"] = canonical
    out["_sanitized_dropped"] = len(names) - len(kept)
    return out


def _load_dedup_review_items(user_id: str) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    try:
        import json as _json
        p = _dedup_review_path(user_id)
        if not p.exists():
            return []
        payload = _json.loads(p.read_text(encoding="utf-8"))
        items: List[Dict[str, Any]] = []
        for i, c in enumerate(payload.get("clusters") or []):
            c = _sanitize_cluster(c, user_id)   # актуальный фильтр + негативы
            names = c.get("member_names") or []
            if len(names) < 2:
                continue   # после чистки не осталось пары — не дубли
            conf = c.get("confidence", 0)
            canonical = c.get("canonical", names[0])
            items.append({
                "id": f"dedup::{user_id}::{i}",
                "entity_type": "dedup",
                "title": f"Дубли ({c.get('entity_type', 'person')}): "
                         + " + ".join(names[:5]),
                "original_attribution": f"→ {canonical}",
                "review_reason": (c.get("reason") or "похожие имена")
                                 + f" (уверенность {conf:.0%})",
                "confidence": "medium",
                # UI частичного слияния: чекбоксы по участникам —
                # можно слить двух Кать и отцепить третью
                "members": [
                    {"id": mid, "name": nm,
                     "is_canonical": nm == canonical}
                    for mid, nm in zip(c.get("member_ids") or [], names)
                ],
            })
        return items
    except Exception:
        logger.debug("dedup review load failed", exc_info=True)
        return []


async def _apply_dedup_review(
    correction_id: str, action: str,
    selected_ids: Optional[List[str]] = None,
    comment: str = "",
) -> Dict[str, Any]:
    """confirm → слить (все или ВЫБРАННЫЕ selected_ids) в canonical;
    delete → отклонить; skip → оставить.

    Частичное слияние: в кластере могут быть и настоящие дубли, и чужие —
    человек отмечает, кого сливать. Невыбранные при confirm и весь кластер
    при delete пишутся в НЕГАТИВНУЮ ПАМЯТЬ (пара + комментарий): ночной
    дедуп больше никогда не предложит эти пары, LLM видит их в промпте.
    """
    try:
        import json as _json
        _, uid, idx_s = correction_id.split("::", 2)
        idx = int(idx_s)
        p = _dedup_review_path(uid)
        payload = _json.loads(p.read_text(encoding="utf-8"))
        clusters = payload.get("clusters") or []
        if idx < 0 or idx >= len(clusters):
            return {"ok": False, "error": "cluster not found"}
        # тот же фильтр, что на дисплее: сливаем только реально совпадающих
        # (иначе confirm по «чистому» на вид кластеру слил бы разные фамилии)
        cluster = _sanitize_cluster(clusters[idx], uid)
        etype = cluster.get("entity_type", "person")
        ids = cluster.get("member_ids") or []
        names = cluster.get("member_names") or []
        canonical = cluster.get("canonical") or (names[0] if names else "")
        name_by_id = dict(zip(ids, names))
        merged = 0
        negatives = 0
        if action == "confirm":
            # Выбор человека: сливаем только отмеченных (по умолчанию всех)
            sel = set(selected_ids) if selected_ids else set(ids)
            merge_ids = [i for i in ids if i in sel]
            skip_ids = [i for i in ids if i not in sel]
            if len(merge_ids) >= 2:
                from backend.core.observability.tenant_context import (
                    reset_current_tenant, set_current_tenant,
                )
                from backend.core.store.entity_resolver import (
                    EntityResolver, _keep_name_rank,
                )
                from backend.core.store.graph_view import (
                    merged_graph_view_for_user,
                )
                graph = await merged_graph_view_for_user(uid, use_networkx=None)
                resolver = EntityResolver(graph_builder=graph,
                                          vector_indexer=None)
                tok = set_current_tenant(uid)
                try:
                    # keep: канон, если он среди выбранных; иначе лучший из
                    # выбранных по рангу имени (не сливаем В отцеплённого)
                    keep = next((i for i in merge_ids
                                 if name_by_id.get(i) == canonical), None)
                    keep = keep or max(
                        merge_ids,
                        key=lambda i: _keep_name_rank(name_by_id.get(i, "")))
                    for i in merge_ids:
                        if i != keep:
                            if await resolver._merge_via_builder(keep, i):
                                merged += 1
                finally:
                    reset_current_tenant(tok)
                    await graph.close(save=False)
            # Невыбранные = человек сказал «это не тот» → негативная память
            if skip_ids:
                from backend.core.store.dedup_negative import (
                    add_negative_pairs,
                )
                negatives = add_negative_pairs(
                    uid, canonical,
                    [name_by_id.get(i, "") for i in skip_ids],
                    comment=comment, entity_type=etype)
        elif action == "delete":
            # Отклонение всего кластера: канон ≠ каждый участник
            from backend.core.store.dedup_negative import add_negative_pairs
            negatives = add_negative_pairs(
                uid, canonical,
                [nm for nm in names if nm != canonical],
                comment=comment, entity_type=etype)
        if action in ("confirm", "delete"):
            clusters.pop(idx)
            payload["clusters"] = clusters
            p.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        return {"ok": True, "correction_id": correction_id,
                "action": action, "merged": merged,
                "negatives_recorded": negatives}
    except Exception as e:
        logger.error(f"apply dedup review failed: {e}")
        return {"ok": False, "error": str(e)}


class InsightsController(Controller):
    guards = [enforce_user_id_matches_token]
    path = "/insights"
    tags = ["insights"]

    @get("/")
    async def list_insights(
        self,
        user_id: str = Parameter(query="user_id", required=True),
        unread_only: bool = Parameter(query="unread_only", default=False),
        priority: str = Parameter(query="priority", default=""),
        insight_type: str = Parameter(query="type", default=""),
        limit: int = Parameter(query="limit", default=50),
    ) -> Dict[str, Any]:
        """История инсайтов (новые сверху). Фильтры: unread_only, priority
        (critical/high/medium/low), type (pattern/risk/anomaly/...)."""
        try:
            store = _store(user_id)
            items: List[dict] = store.list(
                unread_only=unread_only,
                priority=priority or None,
                insight_type=insight_type or None,
                limit=limit,
            )
            return {"insights": items, **store.counts()}
        except ValueError as e:           # невалидный user_id (не UUID)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"insights list failed: {e}")
            return {"insights": [], "total": 0, "unread": 0, "error": str(e)}

    @get("/weekly")
    async def weekly_cost_insights(
        self,
        tenant_id: str = Parameter(query="tenant_id", default=""),
        user_id: str = Parameter(query="user_id", default=""),
        max_meetings: int = Parameter(query="max_meetings", default=3),
    ) -> Dict[str, Any]:
        """Финансовые insights за неделю поверх llm_usage (wow-screen, шаг 3).

        Комплементарно к ночным insights: ищет не аномалии в встречах,
        а аномалии в **расходах** (top-N дорогих встреч, surface-spike,
        дни с скачком spend). Формат идентичен ленте `/api/v1/meetflow/
        insights` — HomeTab объединяет их без специальной логики.

        tenant_id (рекомендуется на проде) фильтрует расходы по тенанту;
        user_id оставлен для совместимости и логирования, в эвристиках
        не участвует (cost-аналитика per-tenant, не per-user).
        """
        try:
            from backend.core.llm.weekly_insights import compute_weekly_insights
            insights = compute_weekly_insights(
                tenant_id=tenant_id or None,
                max_meetings=max(1, min(int(max_meetings), 10)),
                # publish_events=True — каждый insight уйдёт через /ws/signals
                # → frontend HomeTab подсветится в real-time
                publish_events=True,
            )
            return {
                "insights": insights,
                "total": len(insights),
                "tenant_id": tenant_id or None,
                "source": "weekly_cost_attribution",
            }
        except Exception as e:
            logger.error(f"weekly insights failed: {e}")
            return {"insights": [], "total": 0, "error": str(e)}

    @get("/memory-quality")
    async def memory_quality_summary(
        self,
        tenant_id: str = Parameter(query="tenant_id", default=""),
    ) -> Dict[str, Any]:
        """Memory Quality summary для HomeTab виджета (issue #112 T-9).

        Возвращает распределение attribution_confidence по Decision/Task
        узлам тенанта:
          {tenant_id, tasks: {total, high, medium, low, needs_review},
           decisions: {...}, overall: {total, honest_attribution_pct,
           needs_review}}

        Foundry-style: показываем не «100% accuracy», а честное
        распределение — система говорит, когда не знает. Это и есть
        enterprise-trust аргумент.
        """
        try:
            from backend.core.store.memory_quality import scan_quality_for_tenant
            return await scan_quality_for_tenant(tenant_id or None)
        except Exception as e:
            logger.error(f"memory-quality summary failed: {e}")
            return {"error": str(e)}

    @get("/needs-review")
    async def memory_review_queue(
        self,
        tenant_id: str = Parameter(query="tenant_id", default=""),
        user_id: str = Parameter(query="user_id", default=""),
        limit: int = Parameter(query="limit", default=50),
    ) -> Dict[str, Any]:
        """Очередь review (issue #112 T-8/T-10). Читает из corrections_store
        (canonical queue с workflow-состоянием) + дедуп-кандидатов ночной
        консолидации. Fallback на graph-scan если store пуст (legacy данные
        без enqueue)."""
        try:
            from backend.core.store.corrections_store import get_corrections_store
            store = get_corrections_store()
            items = store.list_pending(
                tenant_id or None, limit=max(1, min(int(limit), 200)))
            stats = store.stats(tenant_id or None)
            source = "corrections_store"
            # Fallback: если очередь пуста, попробуем graph-scan (legacy
            # узлы с needs_human_review, созданные до T-8)
            if not items:
                from backend.core.store.memory_quality import list_needs_review
                items = await list_needs_review(
                    tenant_id or None, limit=max(1, min(int(limit), 200)))
                source = "graph_scan"
            # Дедуп-кандидаты средней уверенности из ночной консолидации.
            # Раньше писались в data/dedup_review/<uid>.json и НИКТО их не
            # читал — панель «Проверка памяти» была пуста, хотя лог говорил
            # «N on review». confirm = слить, delete = отклонить.
            dedup_items = _load_dedup_review_items(user_id or tenant_id or "")
            if dedup_items:
                items = dedup_items + list(items)
            return {"items": items, "total": len(items),
                    "tenant_id": tenant_id or None, "source": source,
                    "stats": stats}
        except Exception as e:
            logger.error(f"needs-review list failed: {e}")
            return {"items": [], "total": 0, "error": str(e)}

    @post("/review/{correction_id:str}/apply")
    async def apply_review_correction(
        self,
        correction_id: str,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", default=""),
    ) -> Dict[str, Any]:
        """Применить human-решение к записи review queue (issue #112 T-10).

        body: {action: 'confirm'|'fix'|'delete'|'skip', correction?: str}
          - confirm: система была права, attribution подтверждена
          - fix: человек дал correction (новое имя assignee/maker)
          - delete: attribution неверна, удалить
          - skip: отложить (остаётся в очереди)
        """
        action = str((data or {}).get("action") or "")
        if action not in ("confirm", "fix", "delete", "skip"):
            return {"ok": False,
                    "error": "action must be confirm|fix|delete|skip"}
        # Дедуп-кандидат (id "dedup::<uid>::<idx>"): confirm = слить сущности
        # (все или selected_ids — частичное слияние), delete = отклонить,
        # skip = оставить. Невыбранные/отклонённые + comment → негативная
        # память (эти пары дедуп больше не предложит).
        if correction_id.startswith("dedup::"):
            sel = (data or {}).get("selected_ids")
            return await _apply_dedup_review(
                correction_id, action,
                selected_ids=([str(x) for x in sel]
                              if isinstance(sel, list) else None),
                comment=str((data or {}).get("comment") or ""))
        try:
            from backend.core.store.corrections_store import get_corrections_store
            ok = get_corrections_store().apply_correction(
                correction_id,
                action=action,
                correction=(data or {}).get("correction"),
                reviewed_by=user_id or None,
            )
            return {"ok": ok, "correction_id": correction_id, "action": action}
        except Exception as e:
            logger.error(f"apply correction failed: {e}")
            return {"ok": False, "error": str(e)}

    @get("/focus/today")
    async def daily_focus(
        self,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """Фокус дня для главного экрана: самое важное сейчас (тип
        чередуется ночной консолидацией). Читаем готовый файл — мгновенно."""
        try:
            import json as _json
            from pathlib import Path
            f = Path("data") / "focus" / f"{user_id}.json"
            if f.exists():
                return {"ok": True, "focus": _json.loads(f.read_text(encoding="utf-8"))}
        except Exception as e:
            logger.debug(f"daily focus read failed: {e}")
        return {"ok": True, "focus": None}

    @get("/report/today")
    async def daily_report(
        self,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """Отчёт дня (формат чередуется по дням недели ночной консолидацией)."""
        try:
            import json as _json
            from pathlib import Path
            f = Path("data") / "reports" / "daily" / f"{user_id}.json"
            if f.exists():
                return {"ok": True, "report": _json.loads(f.read_text(encoding="utf-8"))}
        except Exception as e:
            logger.debug(f"daily report read failed: {e}")
        return {"ok": True, "report": None}

    @post("/{insight_id:str}/read")
    async def mark_read(
        self,
        insight_id: str,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """Пометить инсайт прочитанным."""
        try:
            ok = _store(user_id).mark_read(insight_id)
            if not ok:
                raise HTTPException(status_code=404, detail="инсайт не найден")
            return {"ok": True, "insight_id": insight_id}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


router = InsightsController
