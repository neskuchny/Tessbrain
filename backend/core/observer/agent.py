# -*- coding: utf-8 -*-
"""Цикл наблюдателя (Ф2): скрин фронтов → ротация → живая зацепка.

Один цикл = два дешёвых LLM-вызова:
  1. СКРИН: по снапшотам встреч и истории прошлых наблюдений выбрать,
     какие фронты «горячие» прямо сейчас (кандидаты со score+signal).
  2. ДИСПЕТЧЕР: из топ-кандидатов (после формулы ротации) написать одну
     живую зацепку — или честно промолчать.

Формула ротации (адаптирована из изученного proactive-модуля):
  priority = score − w_recency·повтор_фронта_недавно − w_declined·отказы
Чередование фронтов — суть характера: недавно показанный фронт получает
штраф, и агент физически не может долбить одно и то же.

Дисциплина: «лучше молчать, чем выдумывать» — нет горячего кандидата или
LLM недоступен → цикл фиксируется (присутствие живёт), зацепки нет.
Высказанное наблюдение пишется в память агента И в общую ленту инсайтов
(InsightStore) — оттуда его видит главная страница. Never-raise.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from backend.core.observer.fronts import FRONTS, fronts_block, get_front
from backend.core.observer.meeting_snapshot import list_snapshot_records
from backend.core.observer.state import (
    add_observation,
    list_watch,
    load_state,
    mark_cycle,
    recent_observations,
)

logger = logging.getLogger(__name__)

_MIN_SNAPSHOTS = int(os.getenv("OBSERVER_MIN_SNAPSHOTS", "3"))
_MIN_SCORE = 0.25
_TOP_K = 4
_RECENCY_PENALTY_DAYS = 10
_W_RECENCY = 0.45
_W_DECLINED = 0.35

SCREEN_SYSTEM_PROMPT = """\
Ты — наблюдатель компании: агент, который между встречами просматривает,
что происходит, и решает, на какой фронт стоит посмотреть пристальнее.

Тебе дают структурные снапшоты последних встреч (темы, блокеры,
напряжения, решения, просьбы), историю твоих прошлых наблюдений с
реакциями человека, и каталог фронтов.

Читай как аналитик, не как поисковик:
- фронт годится, только если в снапшотах видна КОНКРЕТНАЯ зацепка именно
  под его угол — с фактом: кто, что, сколько встреч подряд;
- если по фронту уже было наблюдение и тема закрылась — не повторяй;
- если по фронту человек 2+ раза отказал — пропускай его;
- один и тот же фронт не должен давать одинаковый signal;
- ПОРУЧЕНИЯ (если приложены) — высший приоритет: любые новости по теме
  поручения в снапшотах → кандидат watch_orders с этим фактом.
Если ни одного реально горячего кандидата нет — верни пустой список.
Лучше молчать, чем выдумывать.

Верни строго JSON без markdown:
{"candidates": [
  {"front_id": "<из каталога>", "score": 0.0..1.0,
   "signal": "одно-два предложения с конкретным фактом из снапшотов",
   "meeting_ids": ["1-5 встреч, где это видно"]}
]}
"""

DISPATCHER_SYSTEM_PROMPT = """\
Ты — голос наблюдателя компании. Из кандидатов-фронтов выбери ОДИН,
который сейчас полезнее всего показать человеку, и напиши короткую живую
зацепку. Либо реши, что сейчас лучше промолчать.

Требования к hook_text:
- 1–3 коротких предложения на русском, живым языком, без канцелярита;
- опирайся на конкретные факты из signal кандидата (имена, сроки,
  количество встреч) — никаких «наблюдается» и «рекомендуется»;
- закончи мягким приглашением: «разобрать подробнее?» или вопросом.

Правила: не повторяй недавнее (история приложена); после 2+ отказов
подряд по фронту — либо skip, либо формат question вместо hook.

Верни строго JSON без markdown:
{"skip": true}
или
{"skip": false, "front_id": "<из кандидатов>",
 "format": "hook" | "question",
 "hook_text": "..."}
"""


def observer_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_observer_agent)
    except Exception:
        return False


def _snapshot_block(records: Dict[str, Dict[str, Any]]) -> str:
    parts: List[str] = []
    for mid, rec in records.items():
        snap = rec.get("snapshot") or {}
        title = rec.get("title") or "(без названия)"
        date = str(rec.get("created_at") or "")[:10] or "?"
        lines = [f"[{mid}] {date} — {title}"]
        if snap.get("summary"):
            lines.append(f"  summary: {snap['summary']}")
        for key, label in (("topics", "topics"), ("blockers", "blockers"),
                           ("tensions", "tensions"), ("decisions", "decisions"),
                           ("explicit_asks", "asks"),
                           ("people_mentioned", "people"),
                           ("clients_mentioned", "clients")):
            vals = snap.get(key) or []
            if vals:
                lines.append(f"  {label}: " + "; ".join(str(v) for v in vals))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) or "(снапшотов нет)"


def _history_block(user_id: str) -> str:
    obs = recent_observations(user_id, limit=10)
    if not obs:
        return "(наблюдений ещё не было)"
    lines: List[str] = []
    for o in obs:
        date = time.strftime("%Y-%m-%d", time.localtime(o.get("ts") or 0))
        lines.append(f"- {date} [{o.get('front_id')}] "
                     f"reaction={o.get('reaction') or 'none'}: "
                     f"{o.get('hook') or ''}")
        if o.get("outcome_note"):
            lines.append(f"    → итог ({o.get('outcome_status')}): "
                         f"{o['outcome_note']}")
    return "\n".join(lines)


def _watch_block(user_id: str) -> str:
    watch = list_watch(user_id)
    if not watch:
        return ""
    lines = "\n".join(f"- {w.get('text')}" for w in watch[:20])
    return f"\n# Поручения от человека («проследи за …»)\n{lines}\n"


def _tg_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_observer_telegram_delivery)
    except Exception:
        return False


async def _deliver_tg(user_id: str, hook: str, front_title: str,
                      source_id: str, score: float) -> Optional[str]:
    """Зацепка → Telegram через reactive-гейт (нагрузка/калибровка/дедуп).
    Кнопок в TG нет — реакции живут на главной; так и пишем. Never-raise."""
    if not _tg_enabled():
        return None
    try:
        from backend.core.reactive.recommendations import submit_recommendation
        res = await submit_recommendation(
            user_id=user_id,
            title=f"👁 {front_title}"[:120],
            body_markdown=f"👁 {hook}\n\nОтветить (интересно/нет) можно "
                          f"на главной странице Tessbrain.",
            signal_type="observer_hook",
            priority=70 if score >= 0.7 else 50,
            recommended_channels=["telegram"],
            source_kind="observer", source_id=source_id)
        return str((res or {}).get("status") or "")
    except Exception:
        logger.debug("observer tg delivery failed", exc_info=True)
        return None


def _rank(candidates: List[Dict[str, Any]], user_id: str,
          ) -> List[Dict[str, Any]]:
    """Формула ротации: штраф за недавний повтор фронта и за отказы."""
    now = time.time()
    history = recent_observations(user_id, limit=30)

    def priority(c: Dict[str, Any]) -> float:
        fid = str(c.get("front_id") or "")
        score = float(c.get("score") or 0)
        last_ts = max((o.get("ts") or 0 for o in history
                       if o.get("front_id") == fid), default=0)
        recency = 0.0
        if last_ts:
            days = (now - last_ts) / 86400.0
            recency = max(0.0, 1.0 - days / _RECENCY_PENALTY_DAYS)
        declined = sum(1 for o in history if o.get("front_id") == fid
                       and o.get("reaction") == "declined")
        # Поручения человека — вне общей ротации: явная просьба следить
        # бьёт штраф за повтор (но не отменяет штраф за отказы).
        watch_bonus = 0.35 if fid == "watch_orders" else 0.0
        return (score + watch_bonus - _W_RECENCY * recency
                - _W_DECLINED * min(1.0, declined / 2.0))

    valid = [c for c in candidates
             if get_front(str(c.get("front_id") or "")) is not None
             and float(c.get("score") or 0) >= _MIN_SCORE]
    return sorted(valid, key=priority, reverse=True)


def _push_to_insights(user_id: str, front_title: str, hook: str,
                      signal: str, score: float) -> None:
    """Наблюдение → общая лента инсайтов (её читает главная). Best-effort."""
    try:
        from backend.core.sleep.insight_store import (
            InsightStore,
            stable_insight_id,
        )
        from backend.core.store.tenant_paths import insights_path_for_user
        item = {
            "type": "observer",
            "title": hook[:200],
            "description": f"{front_title}: {signal}"[:400],
            "priority": "high" if score >= 0.7 else "medium",
            "confidence": round(float(score), 2),
            "entities": [], "actions": [],
            "source": "observer",
        }
        item["insight_id"] = stable_insight_id(item)
        InsightStore(persist_path=insights_path_for_user(user_id)).add([item])
    except Exception:
        logger.debug("observer → insights push failed", exc_info=True)


EXPAND_SYSTEM_PROMPT = """\
Ты — наблюдатель компании. Человек нажал «разобрать подробнее» на твоей
зацепке. Напиши полный, но плотный разбор по этому фронту.

Структура (markdown, без приветствий и воды):
## Что я вижу
Факты из снапшотов с датами встреч — конкретно: кто, что, сколько раз.
## Почему это важно
1–2 абзаца: чем грозит / что даёт, если оставить как есть.
## Что сделать
2–4 конкретных шага с исполнителями, если они видны из данных.

Жёсткие правила: только факты из приложенных снапшотов; ничего не
выдумывать; никаких «рекомендуется обратить внимание» — живой язык;
если данных мало для раздела — честно написать, чего не хватает.
"""


async def expand_observation(user_id: str, observation_id: str,
                             ) -> Dict[str, Any]:
    """«Разобрать подробнее»: полный разбор наблюдения премиум-моделью.
    Кэшируется на наблюдении — повторная кнопка бесплатна. Never-raise."""
    from backend.core.observer.state import get_observation, set_report
    obs = get_observation(user_id, observation_id)
    if obs is None:
        return {"status": "not_found"}
    if obs.get("report"):
        return {"status": "ok", "report": obs["report"], "cached": True}
    front = get_front(str(obs.get("front_id") or ""))
    records = list_snapshot_records(user_id, limit=40)
    # Сначала встречи, на которых стояла зацепка; их снапшоты — первыми.
    mids = [str(m) for m in (obs.get("meeting_ids") or [])]
    ordered = {mid: records[mid] for mid in mids if mid in records}
    for mid, rec in records.items():
        if mid not in ordered:
            ordered[mid] = rec
    prompt = (
        f"# Фронт\n{front.title if front else obs.get('front_id')}: "
        f"{front.angle if front else ''}\n\n"
        f"# Зацепка, которую человек хочет разобрать\n«{obs.get('hook')}»\n"
        f"Сигнал под ней: {obs.get('signal') or '(нет)'}\n\n"
        f"# Снапшоты встреч (релевантные — первыми)\n"
        f"{_snapshot_block(ordered)}\n\n"
        "Напиши полный разбор по структуре из системного промпта.")
    try:
        from backend.core.llm.router import ModelTier, get_llm_router
        text = await get_llm_router().generate(
            prompt=prompt, system_prompt=EXPAND_SYSTEM_PROMPT,
            model_tier=ModelTier.PREMIUM, temperature=0.4)
    except Exception as e:
        logger.warning("observer expand LLM failed: %s", e)
        return {"status": "llm_failed"}
    text = str(text or "").strip()
    if not text:
        return {"status": "llm_failed"}
    set_report(user_id, observation_id, text)
    return {"status": "ok", "report": text[:12000], "cached": False}


async def propose_board(user_id: str, observation_id: str) -> Dict[str, Any]:
    """«Руки» агента: собрать доску-автоматизацию из наблюдения.

    NL-планировщик (тот же, что «процесс из естественного языка»: premium
    + код-валидатор графа) строит процесс, закрывающий наблюдение.
    Дисциплина «подготовил → человек утвердил»: автоматизация на триггере
    ВЫКЛЮЧЕНА — доска появляется в «Моих досках» черновиком, человек
    смотрит, правит и включает сам. Кэш: одна доска на наблюдение."""
    async def _board_exists(uid: str, board_id: str) -> bool:
        """Доска ещё лежит в board_workflows этого пользователя?

        Ошибка проверки трактуется как «существует»: хуже ложного
        пересоздания только дубль доски из-за моргнувшей сети."""
        try:
            from backend.db.supabase_client import get_supabase_client
            rows = await get_supabase_client()._request(
                "GET", "/rest/v1/board_workflows",
                params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}",
                        "select": "id", "limit": "1"})
            return bool(rows)
        except Exception:
            logger.debug("observer: board existence check failed",
                         exc_info=True)
            return True

    from backend.core.observer.state import get_observation, set_board
    obs = get_observation(user_id, observation_id)
    if obs is None:
        return {"status": "not_found"}
    if obs.get("board_id"):
        # Кэш обязан сверяться с реальностью: доску могли удалить (вручную
        # или дедупом копий), а карточка продолжала уверять «создана —
        # найдёте в „Моих досках“». Пропала — честно пересоздаём.
        if await _board_exists(user_id, str(obs["board_id"])):
            return {"status": "ok", "board_id": obs["board_id"],
                    "board_name": obs.get("board_name") or "", "cached": True}
        logger.info("👁 доска %s из наблюдения исчезла из board_workflows — "
                    "пересоздаю", obs["board_id"])
        set_board(user_id, observation_id, "", "")
    front = get_front(str(obs.get("front_id") or ""))
    request_text = (
        f"Наблюдение агента-наблюдателя (фронт «"
        f"{front.title if front else obs.get('front_id')}»): "
        f"{obs.get('hook')}\n"
        f"Факты под наблюдением: {obs.get('signal') or '(нет)'}\n\n"
        "Собери процесс-автоматизацию, которая закрывает эту проблему "
        "РЕГУЛЯРНО, а не разово: подходящий триггер (расписание или событие "
        "встречи), сбор нужных данных (встречи/CRM/веб — по смыслу), краткая "
        "обработка и доставка результата в Telegram.")
    try:
        from backend.core.board.nl_designer import design_process
        res = await design_process(user_id, request_text)
    except Exception as e:
        logger.warning("observer propose_board design failed: %s", e)
        return {"status": "design_failed", "error": str(e)[:300]}
    if not (isinstance(res, dict) and res.get("success")
            and res.get("workflow")):
        return {"status": "design_failed",
                "error": str((res or {}).get("error") or "план не собрался")[:300]}
    workflow = res["workflow"]
    # Черновик: триггер выключен — доска не начнёт стрелять без человека.
    for n in workflow.get("nodes") or []:
        if str(n.get("type")) == "trigger":
            n.setdefault("data", {})["enabled"] = False
    name = ("👁 " + str(workflow.get("name")
                        or (front.title if front else "Автоматизация")))[:200]
    workflow["name"] = name
    import uuid as _uuid
    board_id = f"board_{_uuid.uuid4().hex[:16]}"
    try:
        from backend.db.supabase_client import get_supabase_client
        await get_supabase_client()._request(
            "POST", "/rest/v1/board_workflows",
            json_data={"id": board_id, "user_id": user_id, "name": name,
                       "kind": "process", "graph": workflow})
    except Exception as e:
        logger.warning("observer propose_board save failed: %s", e)
        return {"status": "save_failed", "error": str(e)[:300]}
    set_board(user_id, observation_id, board_id, name)
    logger.info("👁 доска из наблюдения [%s]: %s (%s)",
                obs.get("front_id"), name, board_id)
    return {"status": "ok", "board_id": board_id, "board_name": name,
            "cached": False, "summary": str(res.get("summary") or "")[:500],
            "warnings": res.get("warnings") or []}


async def run_observer_cycle(user_id: str, *, force: bool = False,
                             ) -> Dict[str, Any]:
    """Один цикл наблюдателя для пользователя. Never-raise.

    force=True — ручной запуск (кнопка «осмотреться»): игнорирует каденс,
    но не флаг вкл/выкл (его проверяет вызывающий слой)."""
    cycle_days = float(os.getenv("OBSERVER_CYCLE_DAYS", "2"))
    st = load_state(user_id)
    last = float(st.get("last_cycle_at") or 0)
    if not force and last and (time.time() - last) < cycle_days * 86400:
        return {"status": "not_due"}

    records = list_snapshot_records(user_id, limit=40)
    if len(records) < _MIN_SNAPSHOTS:
        mark_cycle(user_id, "", "")
        return {"status": "few_snapshots", "snapshots": len(records)}

    from backend.core.llm.router import ModelTier, get_llm_router
    router = get_llm_router()

    # 0) OUTCOME (Ф4): созревшее «я говорил — проверил». Максимум одна
    # проверка за цикл; applied/worsened рождают follow-up в память/ленту/TG.
    outcome_res: Optional[Dict[str, Any]] = None
    try:
        from backend.core.observer.outcome import check_one_outcome
        outcome_res = await check_one_outcome(user_id, router, records)
        if outcome_res and outcome_res.get("follow_up_hook"):
            fid = str(outcome_res.get("observation_id") or "")
            front = get_front(next(
                (o.get("front_id") for o in recent_observations(user_id, 30)
                 if o.get("id") == fid), "")) or FRONTS[0]
            _push_to_insights(user_id, front.title,
                              str(outcome_res["follow_up_hook"]),
                              str(outcome_res.get("note") or ""), 0.6)
            await _deliver_tg(user_id, str(outcome_res["follow_up_hook"]),
                              front.title,
                              str(outcome_res.get("follow_up_id") or ""), 0.6)
    except Exception:
        logger.debug("observer outcome check skipped", exc_info=True)

    # 1) СКРИН: какие фронты горячие.
    screen_prompt = (
        f"# Снапшоты последних встреч\n{_snapshot_block(records)}\n\n"
        f"# История наблюдений\n{_history_block(user_id)}\n"
        f"{_watch_block(user_id)}\n"
        f"# Каталог фронтов\n{fronts_block()}\n\n"
        "Верни JSON с кандидатами по контракту системного промпта.")
    try:
        raw = await router.generate_json(
            prompt=screen_prompt, system_prompt=SCREEN_SYSTEM_PROMPT,
            model_tier=ModelTier.STANDARD, temperature=0.3)
    except Exception as e:
        logger.warning("observer screen LLM failed: %s", e)
        return {"status": "llm_failed", "stage": "screen"}
    candidates = (raw or {}).get("candidates") if isinstance(raw, dict) else None
    ranked = _rank(candidates or [], user_id)

    if not ranked:
        # Молчание — тоже цикл: присутствие обновляем фронтом-лидером скрина
        # (или первым из каталога), зацепки нет.
        f0 = get_front(str((candidates or [{}])[0].get("front_id") or "")) \
            if candidates else None
        front = f0 or FRONTS[0]
        mark_cycle(user_id, front.id, front.title)
        return {"status": "silent", "candidates": len(candidates or [])}

    # 2) ДИСПЕТЧЕР: одна живая зацепка из топ-кандидатов.
    top = ranked[:_TOP_K]
    cand_block = "\n".join(
        f"- front_id={c['front_id']} score={c.get('score')}: "
        f"{c.get('signal') or ''} (встречи: "
        f"{', '.join(str(m) for m in (c.get('meeting_ids') or [])[:5])})"
        for c in top)
    disp_prompt = (
        f"# Кандидаты (выбери один front_id из них)\n{cand_block}\n\n"
        f"# История наблюдений\n{_history_block(user_id)}\n\n"
        "Верни JSON по контракту системного промпта.")
    try:
        picked = await router.generate_json(
            prompt=disp_prompt, system_prompt=DISPATCHER_SYSTEM_PROMPT,
            model_tier=ModelTier.STANDARD, temperature=0.5)
    except Exception as e:
        logger.warning("observer dispatcher LLM failed: %s", e)
        front = get_front(top[0]["front_id"]) or FRONTS[0]
        mark_cycle(user_id, front.id, front.title)
        return {"status": "llm_failed", "stage": "dispatcher"}

    chosen_front = get_front(str((picked or {}).get("front_id") or "")) \
        if isinstance(picked, dict) else None
    hook = str((picked or {}).get("hook_text") or "").strip() \
        if isinstance(picked, dict) else ""
    skip = (not isinstance(picked, dict) or picked.get("skip") is True
            or chosen_front is None or not hook
            or chosen_front.id not in {c["front_id"] for c in top})

    if skip:
        front = get_front(top[0]["front_id"]) or FRONTS[0]
        mark_cycle(user_id, front.id, front.title)
        return {"status": "silent", "candidates": len(ranked)}

    cand = next(c for c in top if c["front_id"] == chosen_front.id)
    fmt = str(picked.get("format") or "hook")
    if fmt not in ("hook", "question"):
        fmt = "hook"
    oid = add_observation(
        user_id, front_id=chosen_front.id, fmt=fmt, hook=hook,
        signal=str(cand.get("signal") or ""),
        meeting_ids=[str(m) for m in (cand.get("meeting_ids") or [])],
        score=float(cand.get("score") or 0))
    mark_cycle(user_id, chosen_front.id, chosen_front.title)
    _push_to_insights(user_id, chosen_front.title, hook,
                      str(cand.get("signal") or ""),
                      float(cand.get("score") or 0))
    tg_status = await _deliver_tg(user_id, hook, chosen_front.title, oid,
                                  float(cand.get("score") or 0))
    logger.info("👁 наблюдение [%s] для %s: %s",
                chosen_front.id, user_id[:8], hook[:80])
    return {"status": "observed", "observation_id": oid,
            "front_id": chosen_front.id, "format": fmt, "hook": hook,
            **({"telegram": tg_status} if tg_status else {}),
            **({"outcome": outcome_res} if outcome_res else {})}
