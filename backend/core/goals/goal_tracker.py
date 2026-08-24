# -*- coding: utf-8 -*-
"""
Goal Tracker — система ведения рабочего процесса: цели (эпики) → задачи,
трекинг движения отделов/сотрудников/компании к результату по неделям.

Зачем (запрос): «всегда есть условные эпики в компании и задачи под них —
надо понимать, движемся ли мы к целям отделов/сотрудников/компании, как
прошло движение на неделе/следующей, какие решения поменять для улучшения».

Дизайн (как у остальных примитивов): per-user JSON через tenant_paths,
file_lock + atomic_write. Цель = эпик с уровнем (company/department/person),
прогрессом 0-100, дедлайном; под целью — связанные задачи (из графа/
задачников). Снимок прогресса берётся еженедельно → дельта «было/стало»
показывает динамику, а LLM (дёшево, опционально) формулирует, что
поменять.

Источники сигналов о движении (best-effort, skeleton-first):
- задачи под целью (граф знаний: статусы done/in_progress/blocked);
- инсайты/рассинхроны, затрагивающие цель;
- решения (decision events) из встреч.
Числа считает код, LLM только формулирует рекомендацию.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GOAL_LEVELS = ("company", "department", "person")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_week(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


class GoalStore:
    """Цели + еженедельные снимки прогресса одного пользователя/тенанта."""

    def __init__(self, persist_path: str):
        self._path = persist_path
        self._data: dict = {"goals": [], "snapshots": []}
        self._load()

    def _locked(self, fn):
        from backend.core.store.tenant_io import file_lock
        with file_lock(self._path):
            self._load()
            return fn()

    # -------- цели --------
    def add_goal(self, *, title: str, level: str, owner: str = "",
                 department: str = "", description: str = "",
                 target_date: str = "", parent_id: str = "",
                 metric: str = "", key_results: Optional[list] = None,
                 cycle: str = "", commitment: str = "committed") -> dict:
        if level not in GOAL_LEVELS:
            raise ValueError(f"level must be one of {GOAL_LEVELS}")
        from backend.core.goals.okr import (
            COMMITMENTS,
            normalize_kr,
            quarter_of,
            score_goal,
        )
        krs = [k for k in (normalize_kr(x) for x in (key_results or []))
               if k is not None]
        if commitment not in COMMITMENTS:
            commitment = "committed"
        okr_score = score_goal(krs)
        goal = {
            "id": str(uuid.uuid4()),
            "title": title, "level": level, "owner": owner,
            "department": department, "description": description,
            "target_date": target_date, "parent_id": parent_id,
            "metric": metric,
            # OKR-каркас. score 0..1 живёт отдельно от прогресса задач:
            # None значит «оценить нечем», и в 0 он не превращается.
            "key_results": krs,
            "cycle": cycle or (quarter_of() if krs else ""),
            "commitment": commitment,
            "score": okr_score,
            "checkins": [],
            "progress": int(round(okr_score * 100)) if okr_score is not None else 0,
            "status": "active",
            "task_ids": [], "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

        def _impl():
            self._data["goals"].append(goal)
            self._save()
            return goal
        return self._locked(_impl)

    def update_goal(self, goal_id: str, patch: dict) -> Optional[dict]:
        allowed = {"title", "description", "owner", "department", "metric",
                   "target_date", "progress", "status", "parent_id", "task_ids",
                   "cycle", "commitment"}

        def _impl():
            from backend.core.goals.okr import (
                COMMITMENTS,
                normalize_kr,
                score_goal,
            )
            for g in self._data["goals"]:
                if g["id"] == goal_id:
                    for k, v in patch.items():
                        if k in allowed:
                            g[k] = v
                    if "key_results" in patch:
                        g["key_results"] = [
                            k for k in (normalize_kr(x)
                                        for x in (patch["key_results"] or []))
                            if k is not None]
                    if g.get("commitment") not in COMMITMENTS:
                        g["commitment"] = "committed"
                    # score пересчитывается, а не принимается с входа:
                    # оценка выводится из KR, руками её не рисуют.
                    if g.get("key_results"):
                        g["score"] = score_goal(g["key_results"])
                        if g["score"] is not None:
                            g["progress"] = int(round(g["score"] * 100))
                    g["updated_at"] = _now_iso()
                    self._save()
                    return g
            return None
        return self._locked(_impl)

    # -------- OKR: чекины и закрытие цикла --------
    def add_checkin(self, goal_id: str, *, author: str, note: str = "",
                    confidence: Optional[float] = None,
                    kr_updates: Optional[list] = None) -> Optional[dict]:
        """Чекин по цели: обновить значения KR, пересчитать оценку, записать.

        kr_updates: [{"kr_id"|"title": ..., "current"|"done"|"fraction": ...}].
        Чекин — это разговор про одну цель, а не агрегат по всем: у записи
        есть автор, заметка, уверенность и снимок оценок на момент чекина.
        """
        def _impl():
            from backend.core.goals.okr import make_checkin, score_goal, score_kr
            for g in self._data["goals"]:
                if g["id"] != goal_id:
                    continue
                krs = g.get("key_results") or []
                by_id = {k["id"]: k for k in krs}
                by_title = {k["title"].strip().lower(): k for k in krs}
                for upd in (kr_updates or []):
                    if not isinstance(upd, dict):
                        continue
                    kr = by_id.get(str(upd.get("kr_id") or "")) or \
                        by_title.get(str(upd.get("title") or "").strip().lower())
                    if kr is None:
                        continue
                    if "current" in upd:
                        try:
                            kr["current"] = float(upd["current"])
                        except (TypeError, ValueError):
                            pass
                    if "done" in upd:
                        kr["done"] = bool(upd["done"])
                    if "fraction" in upd:
                        try:
                            kr["fraction"] = float(upd["fraction"])
                        except (TypeError, ValueError):
                            pass
                g["score"] = score_goal(krs) if krs else g.get("score")
                if g.get("score") is not None:
                    g["progress"] = int(round(g["score"] * 100))
                checkin = make_checkin(
                    author=author, note=note, confidence=confidence,
                    kr_scores={k["id"]: score_kr(k) for k in krs},
                    goal_score=g.get("score"))
                g.setdefault("checkins", []).append(checkin)
                g["checkins"] = g["checkins"][-50:]
                g["updated_at"] = _now_iso()
                self._save()
                return {"goal": g, "checkin": checkin}
            return None
        return self._locked(_impl)

    def close_cycle(self, cycle: str) -> dict:
        """Закрыть квартал: каждой активной цели цикла — финальная оценка и
        грейд по правилам метода (committed требует 100%, aspirational —
        70%). Возвращает сводку; цели переходят в status='closed'."""
        def _impl():
            from backend.core.goals.okr import grade_goal, score_goal
            closed = []
            for g in self._data["goals"]:
                if g.get("cycle") != cycle or g.get("status") != "active":
                    continue
                krs = g.get("key_results") or []
                final = score_goal(krs) if krs else None
                grade = grade_goal(final, g.get("commitment", "committed"))
                g["score"] = final
                g["final_grade"] = grade
                g["status"] = "closed"
                g["closed_at"] = _now_iso()
                g["updated_at"] = _now_iso()
                closed.append({"id": g["id"], "title": g["title"],
                               "score": final, **grade})
            if closed:
                self._save()
            graded = [c for c in closed if c["grade"] != "unmeasured"]
            avg = (round(sum(c["score"] for c in graded) / len(graded), 3)
                   if graded else None)
            return {"cycle": cycle, "closed": closed,
                    "avg_score": avg,
                    "unmeasured": sum(1 for c in closed
                                      if c["grade"] == "unmeasured")}
        return self._locked(_impl)

    def delete_goal(self, goal_id: str) -> bool:
        def _impl():
            before = len(self._data["goals"])
            self._data["goals"] = [g for g in self._data["goals"]
                                   if g["id"] != goal_id]
            if len(self._data["goals"]) != before:
                self._save()
                return True
            return False
        return self._locked(_impl)

    def list_goals(self, *, level: Optional[str] = None,
                   status: Optional[str] = None) -> List[dict]:
        out = self._data["goals"]
        if level:
            out = [g for g in out if g.get("level") == level]
        if status:
            out = [g for g in out if g.get("status") == status]
        return sorted(out, key=lambda g: (g.get("level", ""),
                                          -int(g.get("progress", 0))))

    def get_goal(self, goal_id: str) -> Optional[dict]:
        for g in self._data["goals"]:
            if g["id"] == goal_id:
                return g
        return None

    # -------- снимки прогресса --------
    def record_snapshot(self, *, week: str, goals_state: List[dict],
                        summary: str = "") -> dict:
        """Зафиксировать недельный снимок прогресса (идемпотентно по week)."""
        snap = {"week": week, "recorded_at": _now_iso(),
                "goals": goals_state, "summary": summary}

        def _impl():
            self._data["snapshots"] = [s for s in self._data["snapshots"]
                                       if s.get("week") != week]
            self._data["snapshots"].append(snap)
            self._data["snapshots"] = self._data["snapshots"][-104:]  # ~2 года
            self._save()
            return snap
        return self._locked(_impl)

    def snapshot_for_week(self, week: str) -> Optional[dict]:
        for s in self._data["snapshots"]:
            if s.get("week") == week:
                return s
        return None

    def recent_snapshots(self, limit: int = 8) -> List[dict]:
        return sorted(self._data["snapshots"],
                      key=lambda s: s.get("week", ""), reverse=True)[:limit]

    # -------- persistence --------
    def _save(self) -> None:
        try:
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self._path, self._data)
        except Exception:
            logger.warning("GoalStore save failed", exc_info=True)

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self._data = {"goals": d.get("goals", []),
                                  "snapshots": d.get("snapshots", [])}
        except Exception:
            self._data = {"goals": [], "snapshots": []}


def goal_store_for_user(user_id: str) -> GoalStore:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "goals"
    d.mkdir(parents=True, exist_ok=True)
    return GoalStore(str(d / f"{user_id}.json"))


# ============================================================================
# Прогресс целей по задачам графа + недельная дельта
# ============================================================================

async def _task_progress_from_graph(user_id: str, goal: dict) -> dict:
    """Прогресс цели по связанным задачам в графе знаний.

    Если у цели заданы task_ids — берём их; иначе матчим задачи по
    department/owner/ключевым словам заголовка. done/total → progress."""
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if not (gb.connected and gb.nx_graph):
            return {"done": 0, "total": 0, "blocked": 0, "progress": 0}
        nodes = [d for _i, d in gb.nx_graph.nodes(data=True)]
        try:
            await gb.close(save=False)
        except Exception:
            pass
    except Exception:
        return {"done": 0, "total": 0, "blocked": 0, "progress": 0}

    task_ids = set(goal.get("task_ids") or [])
    dept = (goal.get("department") or "").lower()
    owner = (goal.get("owner") or "").lower()
    kw = [w.lower() for w in (goal.get("title") or "").split() if len(w) > 4]

    def _is_task(d: dict) -> bool:
        return str(d.get("_label") or d.get("label") or "").lower() == "task"

    def _matches(d: dict) -> bool:
        if task_ids:
            return (d.get("id") or d.get("task_id")) in task_ids
        hay = " ".join(str(d.get(k) or "") for k in
                       ("title", "description", "department", "assignee")).lower()
        if dept and dept in hay:
            return True
        if owner and owner in hay:
            return True
        return any(k in hay for k in kw) if kw else False

    done = total = blocked = 0
    for d in nodes:
        if not _is_task(d) or not _matches(d):
            continue
        total += 1
        status = str(d.get("status") or d.get("state") or "").lower()
        if status in ("done", "completed", "closed", "resolved"):
            done += 1
        elif status in ("blocked", "stuck"):
            blocked += 1
    progress = round(100 * done / total) if total else int(goal.get("progress", 0))
    return {"done": done, "total": total, "blocked": blocked, "progress": progress}


def _refresh_metric_krs(user_id: str, goal: dict) -> bool:
    """Подтянуть current метричных KR из реестра метрик компании.

    Это и есть недостающее звено «цель ↔ показатель из данных»: KR с
    metric_name получает свежий факт из тех же данных, по которым считается
    сверка «на встречах звучало X, по данным Y». Прогресс цели перестаёт
    быть счётчиком закрытых задач. Best-effort: реестр недоступен или
    метрика не найдена → KR не трогаем (None остаётся честным «не мерили»).
    """
    changed = False
    krs = goal.get("key_results") or []
    if not any(k.get("metric_name") for k in krs):
        return False
    try:
        from backend.core.ontology.metric_registry import metrics_for_user
        reg = metrics_for_user(user_id)
    except Exception:
        return False
    for kr in krs:
        mn = kr.get("metric_name")
        if not mn or kr.get("kind") != "metric":
            continue
        try:
            pts = [p for p in (reg.series(mn) or [])
                   if p.get("kind") != "plan"]
            if not pts:
                continue
            latest = pts[-1].get("value")
            if latest is not None and latest != kr.get("current"):
                kr["current"] = float(latest)
                changed = True
        except Exception:
            logger.debug("KR metric refresh skipped for %r", mn, exc_info=True)
    return changed


async def compute_goals_progress(user_id: str) -> List[dict]:
    """Текущее состояние всех активных целей.

    Порядок источников прогресса — от данных к прокси:
      1. У цели есть ключевые результаты → оценка 0..1 из них; метричные KR
         перед этим подтягивают свежий факт из реестра метрик.
      2. KR нет, но есть связанные задачи → счётчик задач (как раньше).
    """
    store = goal_store_for_user(user_id)
    out = []
    for g in store.list_goals(status="active"):
        krs = g.get("key_results") or []
        if krs:
            refreshed = _refresh_metric_krs(user_id, g)
            from backend.core.goals.okr import score_goal
            new_score = score_goal(krs)
            if refreshed or new_score != g.get("score"):
                store.update_goal(g["id"], {"key_results": krs})
                g = store.get_goal(g["id"]) or g
            out.append({**g, "progress_source": "key_results"})
            continue
        prog = await _task_progress_from_graph(user_id, g)
        if prog["total"]:  # есть связанные задачи → обновляем прогресс цели
            store.update_goal(g["id"], {"progress": prog["progress"]})
        out.append({**g, "task_progress": prog,
                    "progress_source": "tasks" if prog["total"] else "manual",
                    "progress": prog["progress"] if prog["total"] else g.get("progress", 0)})
    return out


async def weekly_review(user_id: str, *, with_llm: bool = True) -> dict:
    """Недельный обзор движения к целям: дельта против прошлой недели +
    (опц.) рекомендации LLM «что поменять».

    Скелет считает числа (прогресс, дельта, застрявшие задачи), LLM только
    формулирует — работает на дешёвой модели.
    """
    store = goal_store_for_user(user_id)
    week = _iso_week()
    current = await compute_goals_progress(user_id)

    # дельта против последнего отличного от текущей недели снимка
    prev_snap = None
    for s in store.recent_snapshots(limit=12):
        if s.get("week") != week:
            prev_snap = s
            break
    prev_by_id = {g["id"]: g for g in (prev_snap or {}).get("goals", [])}

    movement = []
    for g in current:
        prev = prev_by_id.get(g["id"], {})
        delta = int(g["progress"]) - int(prev.get("progress", g["progress"]))
        movement.append({
            "id": g["id"], "title": g["title"], "level": g["level"],
            "department": g.get("department", ""),
            "progress": g["progress"], "delta": delta,
            "blocked": g.get("task_progress", {}).get("blocked", 0),
            "trend": "↑" if delta > 0 else ("↓" if delta < 0 else "→"),
        })

    # сохраняем снимок этой недели
    goals_state = [{"id": g["id"], "title": g["title"], "level": g["level"],
                    "progress": g["progress"]} for g in current]
    summary = ""
    if with_llm and movement:
        try:
            summary = await _llm_weekly_recommendation(movement)
        except Exception as e:
            logger.warning(f"weekly_review LLM failed: {e}")
    store.record_snapshot(week=week, goals_state=goals_state, summary=summary)

    by_level: Dict[str, list] = {}
    for m in movement:
        by_level.setdefault(m["level"], []).append(m)

    return {
        "week": week,
        "goals_total": len(current),
        "moved_forward": sum(1 for m in movement if m["delta"] > 0),
        "stalled": sum(1 for m in movement if m["delta"] == 0),
        "regressed": sum(1 for m in movement if m["delta"] < 0),
        "blocked_goals": sum(1 for m in movement if m["blocked"] > 0),
        "by_level": by_level,
        "movement": movement,
        "recommendation": summary,
    }


async def _llm_weekly_recommendation(movement: List[dict]) -> str:
    from backend.core.llm.router import LLMRouter, ModelTier
    lines = []
    for m in movement:
        lines.append(f"- [{m['level']}] {m['title']} ({m.get('department', '')}): "
                     f"прогресс {m['progress']}% {m['trend']} (Δ{m['delta']:+d}), "
                     f"застрявших задач: {m['blocked']}")
    prompt = (
        "Ты — операционный директор. Ниже движение целей компании за неделю.\n\n"
        + "\n".join(lines) +
        "\n\nДай КОРОТКО (5-8 пунктов): где мы отстаём и почему похоже застряли, "
        "какие решения стоит поменять на следующей неделе, на чём "
        "сфокусироваться для более быстрого достижения. Опирайся на цифры, "
        "не выдумывай фактов сверх данных."
    )
    router = LLMRouter()
    return await router.generate(prompt=prompt, model_tier=ModelTier.STANDARD,
                                 max_tokens=900)
