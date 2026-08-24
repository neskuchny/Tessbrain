# -*- coding: utf-8 -*-
"""Синхронизация организации «снизу» — 4 уровня сверки + индекс S(t)
(CogniLayer Ф2, Часть III корректирующего документа).

Обобщение скелета cross_department_desync на все уровни запроса:
  L1 «сотрудники внутри отдела»  — person-цели одного отдела: конфликты между
     собой, конфликт с целью отдела, цели отдела «не подхваченные» людьми;
  L2 «отдел ↔ отдел»             — существующий детектор (переиспользован);
  L3 «отделы ↔ генеральный»      — каскад: у каждой company-цели есть
     отдел-владелец (parent_id или общая метрика), конфликты company↔dept;
  L4 «руководители между собой»  — person-цели руководителей разных отделов
     попарно (reports_to-иерархия из Ф0, фолбэк — маркер роли).

Поверх — сводный индекс S(t) (аналог S(t) ТРИС): доля выровненных пар +
покрытие каскада + подхваченность целей отделов; история — per-user JSON.

Дисциплина (та же, что у Столпа B): скелет детерминирован и тестируется без
LLM; hard — только когда конфликт по фактам графа (известный KPI), иначе soft.
Рассинхрон не выдумывается: нет данных уровня → уровень честно пуст и НЕ
участвует в индексе (пустота ≠ идеальная синхронизация).

Данные — ТОЛЬКО из тенанта запрашивающего: его goal_tracker-store, его граф,
плюс метаданные членства орга (роль/отдел/reports_to — видимы оргу и так).
Кросс-тенантных чтений нет — чужие цели не читаются и не утекают.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.core.think.cross_department_desync import (
    detect_pair,
    find_desyncs,
    metric_signature,
)

logger = logging.getLogger(__name__)

# Маркеры руководящей роли (фолбэк, когда reports_to не заполнен).
_MANAGER_ROLE_MARKERS = ("руковод", "директор", "начальник", "лид", "тимлид",
                         "head", "lead", "manager", "director", "ceo", "cto",
                         "coo", "cmo", "founder", "владел")


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


# ── чистый скелет: уровни сверки ──

def check_within_department(
    person_goals: Dict[str, List[str]], dept_goal_texts: List[str],
    department: str = "",
) -> Dict[str, Any]:
    """L1: сверка внутри одного отдела.

    person_goals: {человек: [его цели]}; dept_goal_texts: цели этого отдела.
    Возвращает {conflicts: [...], uncovered_dept_goals: [...], pairs: N}."""
    conflicts: List[dict] = []
    pairs = 0
    people = [p for p in (person_goals or {}) if person_goals.get(p)]

    # люди между собой
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            for ga in person_goals[people[i]]:
                for gb in person_goals[people[j]]:
                    pairs += 1
                    c = detect_pair(str(ga), str(gb))
                    if c:
                        conflicts.append({
                            "kind": "person_person", "department": department,
                            "a": people[i], "b": people[j],
                            "goal_a": str(ga), "goal_b": str(gb), **c,
                        })
    # человек против цели отдела
    for p in people:
        for gp in person_goals[p]:
            for gd in (dept_goal_texts or []):
                pairs += 1
                c = detect_pair(str(gp), str(gd))
                if c:
                    conflicts.append({
                        "kind": "person_dept", "department": department,
                        "a": p, "b": department or "отдел",
                        "goal_a": str(gp), "goal_b": str(gd), **c,
                    })

    # цели отдела, не подхваченные ни одним человеком (по общей метрике)
    uncovered: List[str] = []
    all_person_sigs = [metric_signature(g)
                       for gs in person_goals.values() for g in gs]
    for gd in (dept_goal_texts or []):
        sig = metric_signature(str(gd))
        if sig and not any(sig & ps for ps in all_person_sigs):
            uncovered.append(str(gd))

    return {"conflicts": conflicts, "uncovered_dept_goals": uncovered,
            "pairs": pairs}


def check_cascade(
    company_goals: List[dict], dept_goals: Dict[str, List[str]],
) -> Dict[str, Any]:
    """L3: каскад company → department.

    company_goals: [{id,title,description,...}]; dept_goals: {отдел: [тексты]}.
    Цель компании «покрыта», если есть dept-цель с parent_id на неё ЛИБО с
    общей метрикой. Плюс конфликты company↔dept (detect_pair)."""
    conflicts: List[dict] = []
    covered: List[dict] = []
    uncovered: List[dict] = []
    pairs = 0
    for cg in (company_goals or []):
        text = str(cg.get("title") or "") + " " + str(cg.get("description") or "")
        sig = metric_signature(text)
        cid = str(cg.get("id") or "")
        owners: List[str] = []
        for dept, goals in (dept_goals or {}).items():
            for dg in goals:
                pairs += 1
                dg_text = dg if isinstance(dg, str) else str(
                    (dg or {}).get("title") or "")
                dg_parent = "" if isinstance(dg, str) else str(
                    (dg or {}).get("parent_id") or "")
                linked = bool(cid and dg_parent == cid) or bool(
                    sig and sig & metric_signature(dg_text))
                if linked:
                    owners.append(dept)
                c = detect_pair(text, dg_text)
                if c:
                    conflicts.append({
                        "kind": "company_dept", "a": "company", "b": dept,
                        "goal_a": str(cg.get("title") or ""), "goal_b": dg_text,
                        **c,
                    })
        entry = {"goal": str(cg.get("title") or ""), "goal_id": cid,
                 "owners": sorted(set(owners))}
        (covered if owners else uncovered).append(entry)
    return {"conflicts": conflicts, "covered": covered,
            "uncovered": uncovered, "pairs": pairs}


def check_managers(mgr_goals: Dict[str, List[str]]) -> Dict[str, Any]:
    """L4: руководители между собой — person-цели руководителей попарно.
    mgr_goals: {руководитель: [его цели]}."""
    conflicts: List[dict] = []
    pairs = 0
    mgrs = [m for m in (mgr_goals or {}) if mgr_goals.get(m)]
    for i in range(len(mgrs)):
        for j in range(i + 1, len(mgrs)):
            for ga in mgr_goals[mgrs[i]]:
                for gb in mgr_goals[mgrs[j]]:
                    pairs += 1
                    c = detect_pair(str(ga), str(gb))
                    if c:
                        conflicts.append({
                            "kind": "manager_manager",
                            "a": mgrs[i], "b": mgrs[j],
                            "goal_a": str(ga), "goal_b": str(gb), **c,
                        })
    return {"conflicts": conflicts, "pairs": pairs}


def detect_managers(members: List[dict]) -> List[dict]:
    """Руководители орга: те, на кого указывает reports_to (Ф0-иерархия);
    фолбэк — маркер руководящей роли. Возвращает подсписок members."""
    members = [m for m in (members or []) if isinstance(m, dict)]
    targets = {m.get("reports_to") for m in members if m.get("reports_to")}
    mgrs = [m for m in members if m.get("user_id") in targets]
    if mgrs:
        return mgrs
    return [m for m in members
            if any(k in _norm(m.get("role")) for k in _MANAGER_ROLE_MARKERS)]


def sync_index(levels: Dict[str, Any]) -> Dict[str, Any]:
    """Сводный индекс S(t) 0-100 по компонентам. Уровень без данных (0 пар)
    в индекс НЕ входит — пустота не выдаётся за идеальную синхронизацию.
    Soft-конфликт, отклонённый судьёй (judge.verdict=rejected), не считается."""
    weighted_conflicts = 0.0
    total_pairs = 0
    for key in ("within_departments", "cross_department", "cascade", "managers"):
        lvl = levels.get(key) or {}
        pairs = int(lvl.get("pairs") or 0)
        total_pairs += pairs
        for c in (lvl.get("conflicts") or []):
            if (c.get("judge") or {}).get("verdict") == "rejected":
                continue
            weighted_conflicts += 1.0 if c.get("severity") == "high" else 0.5

    components: Dict[str, Optional[float]] = {}
    components["alignment"] = (
        max(0.0, 1.0 - weighted_conflicts / total_pairs) if total_pairs else None)

    casc = levels.get("cascade") or {}
    n_cov = len(casc.get("covered") or [])
    n_unc = len(casc.get("uncovered") or [])
    components["cascade_coverage"] = (
        n_cov / (n_cov + n_unc) if (n_cov + n_unc) else None)

    wd = levels.get("within_departments") or {}
    dept_total = int(wd.get("dept_goals_total") or 0)
    uncovered_dept = len(wd.get("uncovered_dept_goals") or [])
    components["dept_pickup"] = (
        (dept_total - uncovered_dept) / dept_total if dept_total else None)

    known = [v for v in components.values() if v is not None]
    s = round(100 * sum(known) / len(known)) if known else None
    return {"s": s, "components": components,
            "pairs_total": total_pairs,
            "weighted_conflicts": round(weighted_conflicts, 1)}


# ── LLM-судья неоднозначных пар (под скелетом) ──

# Судим только soft-пары и только ограниченное число: скелет решает, ЧТО
# смотреть, судья — реально ли это конфликт. High (метрика в противоположных
# направлениях) детерминирован и в суде не нуждается.
_JUDGE_MAX_PAIRS = 12


def _judge_prompt(pairs: List[dict]) -> str:
    lines = []
    for p in pairs:
        lines.append(
            f'{p["id"]}. [{p.get("kind", "")}] «{p.get("a", "")}»: '
            f'{p.get("goal_a", "")}\n   «{p.get("b", "")}»: {p.get("goal_b", "")}'
            + (f'\n   общий ресурс: {", ".join(p.get("shared") or [])}'
               if p.get("shared") else ""))
    return (
        "Ты судишь потенциальные конфликты целей внутри одной компании. Для "
        "каждой пары реши: реально ли эти две цели противоречат друг другу или "
        "спорят за один ограниченный ресурс ТАК, что одновременное достижение "
        "обеих затруднено.\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "1. Суди ТОЛЬКО по двум формулировкам ниже. Никакого выдуманного "
        "контекста компании, никаких допущений о бюджетах, людях или сроках, "
        "которых нет в тексте.\n"
        "2. Совпадение слова ≠ конфликт: «увеличить бюджет маркетинга» и "
        "«согласовать бюджет с финансами» — НЕ конфликт.\n"
        "3. Сомневаешься — rejected: ложный рассинхрон вреднее пропущенного "
        "(шум приучает игнорировать сигналы).\n"
        "4. reason — одно предложение, опирающееся на слова самих целей.\n\n"
        "ПАРЫ:\n" + "\n".join(lines) +
        "\n\nОтвет — ТОЛЬКО валидный JSON-массив, по объекту на каждую пару:\n"
        '[{"id": 1, "verdict": "confirmed", "reason": "..."}]\n'
        'verdict строго "confirmed" или "rejected".'
    )


def _parse_judge_response(text: str) -> Dict[int, dict]:
    """id → {verdict, reason}; мусор игнорируется (вердикт останется uncertain)."""
    out: Dict[int, dict] = {}
    try:
        t = str(text or "").strip()
        if "```" in t:
            t = t.split("```")[1].lstrip("json").strip()
        m = re.search(r"\[.*\]", t, re.DOTALL)
        items = json.loads(m.group(0) if m else t)
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            v = _norm(it.get("verdict"))
            if v not in ("confirmed", "rejected"):
                continue
            out[int(it.get("id"))] = {
                "verdict": v, "reason": str(it.get("reason") or "").strip()[:400]}
    except Exception:
        logger.debug("judge parse failed", exc_info=True)
    return out


async def judge_soft_conflicts(
    levels: Dict[str, Any], *, max_pairs: int = _JUDGE_MAX_PAIRS,
) -> Dict[str, int]:
    """Просудить soft-конфликты всех уровней одним LLM-вызовом (in-place).

    Каждому просуженному конфликту дописывается judge={verdict, reason};
    не попавшие в лимит / нераспарсенные остаются без вердикта (uncertain —
    честнее, чем угадывать). Never-raise."""
    stats = {"judged": 0, "confirmed": 0, "rejected": 0, "skipped": 0}
    soft: List[dict] = []
    for key in ("within_departments", "cross_department", "cascade", "managers"):
        for c in ((levels.get(key) or {}).get("conflicts") or []):
            if c.get("severity") == "soft" and "judge" not in c:
                soft.append(c)
    if not soft:
        return stats
    batch = soft[:max_pairs]
    stats["skipped"] = len(soft) - len(batch)
    pairs = [{"id": i + 1, **{k: c.get(k) for k in
                              ("kind", "a", "b", "goal_a", "goal_b", "shared")}}
             for i, c in enumerate(batch)]
    try:
        from backend.core.llm.router import LLMRouter, ModelTier
        raw = await LLMRouter().generate(
            prompt=_judge_prompt(pairs), model_tier=ModelTier.STANDARD,
            max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        logger.warning("org_sync judge LLM failed: %s", e)
        return stats
    verdicts = _parse_judge_response(raw)
    for i, c in enumerate(batch):
        v = verdicts.get(i + 1)
        if not v:
            continue
        c["judge"] = v
        stats["judged"] += 1
        stats[v["verdict"]] += 1
    return stats


# ── история индекса (тренд S(t)) ──

def _index_path(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "sync_index"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


def record_index(user_id: str, index: Dict[str, Any]) -> None:
    """Дописать точку истории S(t) (best-effort, максимум 1 в час)."""
    try:
        path = _index_path(user_id)
        hist: List[dict] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            hist = d.get("history", []) if isinstance(d, dict) else []
        now = datetime.now(timezone.utc).isoformat()
        if hist and str(hist[-1].get("ts", ""))[:13] == now[:13]:
            hist[-1] = {"ts": now, **index}      # та же часовая корзина
        else:
            hist.append({"ts": now, **index})
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(path, {"history": hist[-500:]})
    except Exception:
        logger.debug("sync index history skipped", exc_info=True)


def index_history(user_id: str, limit: int = 60) -> List[dict]:
    try:
        path = _index_path(user_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("history") or [])[-max(1, limit):]
    except Exception:
        return []


# ── сбор данных и оркестратор ──

def _goal_owner_matches(goal_owner: str, member: dict, account_name: str) -> bool:
    """Принадлежит ли цель (owner — свободный текст) участнику орга."""
    o = _norm(goal_owner)
    if not o:
        return False
    if o == _norm(member.get("user_id")):
        return True
    if account_name:
        try:
            from backend.core.identity.identity_service import _name_score
            if _name_score(account_name, goal_owner) >= 0.75:
                return True
        except Exception:
            pass
    return False


async def collect_org_goals(user_id: str) -> Dict[str, Any]:
    """Цели по уровням из goal_tracker запрашивающего + цели отделов из его
    графа. Только его тенант."""
    company: List[dict] = []
    dept: Dict[str, List[Any]] = {}
    person: List[dict] = []
    try:
        from backend.core.goals.goal_tracker import goal_store_for_user
        store = goal_store_for_user(user_id)
        for g in store.list_goals(status="active"):
            lvl = g.get("level")
            if lvl == "company":
                company.append(g)
            elif lvl == "department":
                dept.setdefault(str(g.get("department") or "без отдела"),
                                []).append(g)
            elif lvl == "person":
                person.append(g)
    except Exception:
        logger.debug("org_sync: goal store read skipped", exc_info=True)

    # цели отделов из графа (авто-извлечение, узлы с department + целе-текстом)
    try:
        from backend.core.think.cross_department_desync import (
            dept_goals_from_graph_nodes,
        )
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        nodes = ([data for _i, data in gb.nx_graph.nodes(data=True)]
                 if getattr(gb, "nx_graph", None) is not None else [])
        try:
            await gb.close(save=False)
        except Exception:
            pass
        for d, texts in dept_goals_from_graph_nodes(nodes).items():
            dept.setdefault(d, []).extend(texts)
    except Exception:
        logger.debug("org_sync: graph goals skipped", exc_info=True)

    return {"company": company, "dept": dept, "person": person}


def _dept_texts(dept_goals: Dict[str, List[Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for d, goals in (dept_goals or {}).items():
        out[d] = [g if isinstance(g, str) else str(g.get("title") or "")
                  for g in goals]
        out[d] = [t for t in out[d] if t]
    return out


async def org_sync_report(user_id: str, *, judge: bool = False,
                          notify: bool = True) -> Dict[str, Any]:
    """Полный отчёт синхронизации по 4 уровням + S(t). Never-raise.

    judge=True — LLM-судья подтверждает/отклоняет soft-пары (скелет решает,
    какие пары смотреть; отклонённые не входят в индекс).
    notify=True — фоново уведомить вовлечённых о НОВЫХ значимых рассинхронах
    (дедуп по отпечатку; см. sync_notify)."""
    goals = await collect_org_goals(user_id)
    dept_texts = _dept_texts(goals["dept"])

    # членство/иерархия орга (метаданные, видимые оргу)
    members: List[dict] = []
    try:
        from backend.core.ingest import membership
        org_id = membership.get_org_for_user(user_id)
        if org_id:
            members = membership.list_members(org_id)
    except Exception:
        logger.debug("org_sync: membership skipped", exc_info=True)

    # имена аккаунтов участников — для матчинга owner-поля целей
    acc_names: Dict[str, str] = {}
    try:
        from backend.core.identity.identity_service import _account_identity
        for m in members[:50]:
            uid = m.get("user_id")
            if uid:
                ident = await _account_identity(uid)
                if ident.get("name"):
                    acc_names[uid] = ident["name"]
    except Exception:
        logger.debug("org_sync: account names skipped", exc_info=True)

    # person-цели по отделам (department цели или отдела владельца)
    person_by_dept: Dict[str, Dict[str, List[str]]] = {}
    for g in goals["person"]:
        owner = str(g.get("owner") or "")
        gd = str(g.get("department") or "")
        if not gd and owner:
            for m in members:
                if _goal_owner_matches(owner, m, acc_names.get(m.get("user_id"), "")):
                    gd = str(m.get("department") or "")
                    break
        d = gd or "без отдела"
        person_by_dept.setdefault(d, {}).setdefault(owner or "без владельца",
                                                    []).append(
            str(g.get("title") or ""))

    # L1: внутри каждого отдела
    within = {"conflicts": [], "uncovered_dept_goals": [], "pairs": 0,
              "dept_goals_total": sum(len(v) for v in dept_texts.values())}
    for d, pg in person_by_dept.items():
        r = check_within_department(pg, dept_texts.get(d, []), department=d)
        within["conflicts"].extend(r["conflicts"])
        within["uncovered_dept_goals"].extend(
            {"department": d, "goal": t} for t in r["uncovered_dept_goals"])
        within["pairs"] += r["pairs"]
    # отделы с целями, но без единой person-цели — тоже «не подхвачено»
    for d, texts in dept_texts.items():
        if d not in person_by_dept and texts:
            within["uncovered_dept_goals"].extend(
                {"department": d, "goal": t} for t in texts)

    # L2: отдел ↔ отдел. Через детектор с известными KPI компании, а не
    # голым find_desyncs: без known_kpis поле grounded оставалось False
    # ВСЕГДА, и «high» в отчёте означало лишь противоположные слова-
    # направления в формулировках («увеличить» против «сократить»), а не
    # подтверждённый фактами графа конфликт. Руководитель читал это как
    # «система нашла реальное расхождение» — разница принципиальная.
    known_kpis: set = set()
    try:
        from backend.core.think.cross_department_desync import (
            CrossDepartmentDesyncDetector,
        )
        from backend.core.think.table_understanding_agent import (
            prior_from_graph_nodes,
        )
        from backend.core.store.graph_view import merged_graph_view_for_user
        _gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            _nodes = await _gb.find_nodes_by_label("KPI", limit=500,
                                                   strict_tenant=True) or []
        finally:
            try:
                await _gb.close(save=False)
            except Exception:
                pass
        known_kpis = {k[:5] for k in prior_from_graph_nodes(_nodes).get("kpi", set())
                      if len(k) >= 4}
        cross_list = CrossDepartmentDesyncDetector(
            known_kpis=known_kpis).detect(dept_texts)
    except Exception:
        logger.debug("org_sync: заземление по KPI недоступно, "
                     "конфликты остаются незаземлёнными", exc_info=True)
        cross_list = find_desyncs(dept_texts)
    n_depts = len([d for d in dept_texts if dept_texts[d]])
    cross_pairs = sum(len(dept_texts[a]) * len(dept_texts[b])
                      for i, a in enumerate(sorted(dept_texts))
                      for b in sorted(dept_texts)[i + 1:])
    cross = {"conflicts": [{**c.to_dict(), "kind": "dept_dept",
                            "a": c.department_a, "b": c.department_b}
                           for c in cross_list],
             "pairs": cross_pairs, "departments": n_depts}

    # L3: каскад company → dept
    cascade = check_cascade(goals["company"], dept_texts)

    # L4: руководители между собой
    mgr_members = detect_managers(members)
    mgr_goals: Dict[str, List[str]] = {}
    for m in mgr_members:
        uid = m.get("user_id") or ""
        name = acc_names.get(uid) or uid[:8]
        for g in goals["person"]:
            if _goal_owner_matches(str(g.get("owner") or ""), m,
                                   acc_names.get(uid, "")):
                mgr_goals.setdefault(name, []).append(str(g.get("title") or ""))
    managers = check_managers(mgr_goals)
    managers["managers_detected"] = len(mgr_members)

    levels = {"within_departments": within, "cross_department": cross,
              "cascade": cascade, "managers": managers}

    # LLM-судья soft-пар (опционально): отклонённые не входят в индекс
    judge_stats: Optional[Dict[str, int]] = None
    if judge:
        judge_stats = await judge_soft_conflicts(levels)

    index = sync_index(levels)
    record_index(user_id, index)

    # шаг 3: проактивные уведомления о новых значимых рассинхронах (фоном)
    if notify:
        try:
            from backend.core.think.sync_notify import spawn_notify
            spawn_notify(user_id, levels, acc_names=acc_names, members=members)
        except Exception:
            logger.debug("org_sync: notify spawn skipped", exc_info=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "levels": levels,
        "index": index,
        "judge": judge_stats,
        "inputs": {
            "company_goals": len(goals["company"]),
            "departments_with_goals": len([d for d in dept_texts
                                           if dept_texts[d]]),
            "person_goals": len(goals["person"]),
            "org_members": len(members),
        },
        "note": ("уровни без данных не участвуют в индексе; "
                 "hard — только конфликт по фактам, soft — по формулировкам"),
    }


if __name__ == "__main__":  # смоук чистого скелета
    # L1: конфликт человек-человек + неподхваченная цель отдела
    r1 = check_within_department(
        {"Максим": ["Увеличить число демо до 40 в месяц"],
         "Оля": ["Сократить число демо ради качества лидов"]},
        ["Расширить воронку продаж", "Снизить отток клиентов"],
        department="Продажи")
    assert any(c["kind"] == "person_person" and c["severity"] == "high"
               for c in r1["conflicts"]), r1["conflicts"]
    assert any("отток" in u for u in r1["uncovered_dept_goals"]), r1

    # L3: каскад — покрытая и непокрытая цель компании
    r3 = check_cascade(
        [{"id": "c1", "title": "Увеличить выручку до 100М"},
         {"id": "c2", "title": "Выйти на рынок Казахстана"}],
        {"Продажи": ["Увеличить выручку сегмента SMB"],
         "Маркетинг": ["Снизить стоимость лида"]})
    assert len(r3["covered"]) == 1 and r3["covered"][0]["owners"] == ["Продажи"]
    assert len(r3["uncovered"]) == 1 and "Казахстан" in r3["uncovered"][0]["goal"]

    # L4: руководители тянут в разные стороны
    r4 = check_managers({"Антон": ["Ускорить релизы до еженедельных"],
                         "Максим": ["Замедлить релизы ради стабильности"]})
    assert r4["conflicts"] and r4["conflicts"][0]["severity"] == "high"

    # менеджеры: по reports_to, фолбэк по роли
    ms = [{"user_id": "u1", "role": "разработчик", "reports_to": "u2"},
          {"user_id": "u2", "role": "инженер"},
          {"user_id": "u3", "role": "дизайнер", "reports_to": "u2"}]
    assert [m["user_id"] for m in detect_managers(ms)] == ["u2"]
    ms2 = [{"user_id": "a", "role": "Руководитель продаж"},
           {"user_id": "b", "role": "менеджер по продажам"}]
    got = {m["user_id"] for m in detect_managers(ms2)}
    assert "a" in got

    # S(t): уровни без данных не входят; конфликты снижают
    idx = sync_index({
        "within_departments": {"pairs": 10, "conflicts": [
            {"severity": "high"}, {"severity": "soft"}],
            "dept_goals_total": 4, "uncovered_dept_goals": [{"g": 1}]},
        "cross_department": {"pairs": 0, "conflicts": []},
        "cascade": {"pairs": 4, "conflicts": [], "covered": [{}],
                    "uncovered": [{}]},
        "managers": {"pairs": 0, "conflicts": []},
    })
    assert idx["s"] is not None and 0 <= idx["s"] <= 100
    assert abs(idx["components"]["alignment"] - (1 - 1.5 / 14)) < 1e-9
    assert idx["components"]["cascade_coverage"] == 0.5
    assert idx["components"]["dept_pickup"] == 0.75
    empty = sync_index({})
    assert empty["s"] is None  # нет данных → честное «неизвестно», не 100
    print("org_sync smoke OK")
