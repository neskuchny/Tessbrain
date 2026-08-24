# -*- coding: utf-8 -*-
"""Входящие ответы сотрудников на пуши — замыкание петли.

Сотрудник отвечает боту в Telegram («готово», «упёрся в доступы»,
«2 нужен перенос на пятницу») — и его ответ БЕЗ единого захода в задачник:
  1. привязывается к задаче из его последнего пуша (номер в начале ответа
     выбирает задачу из списка; одна задача — выбирается сама);
  2. уходит КОММЕНТАРИЕМ в карточку задачника (task_actions.comment_task),
     если у задачи есть зеркало в трекере — трекер актуален, человек его
     не открывал;
  3. логируется в состоянии пульса — руководитель в отчёте видит «почему не
     сделано» словами самого сотрудника.

Ничего не закрывается автоматически: «готово» — это комментарий и пометка,
закрытие карточки подтверждает человек/руководитель (авто-закрытие по одному
слову в чате — слишком легко ошибиться).

Контекст chat_id → (владелец, человек, задачи последнего пуша) пишется при
успешной отправке пуша в общий индекс (у webhook'а нет тенанта — бот один).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CTX_TTL_DAYS = 14


def _index_path() -> Path:
    base = Path(os.environ.get("PULSE_PUSH_DIR", "").strip()
                or "data/pulse_push")
    return base / "_chat_index.json"


def _load_index() -> Dict[str, Any]:
    try:
        p = _index_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("pulse chat index unreadable", exc_info=True)
    return {}


def _save_index(idx: Dict[str, Any]) -> None:
    p = _index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def record_chat_context(chat_id: str, owner_uid: str, person_key: str,
                        tasks: List[Dict[str, Any]]) -> None:
    """Запомнить, о каких задачах мы только что спросили этот чат."""
    idx = _load_index()
    idx[str(chat_id)] = {
        "owner_uid": owner_uid,
        "person_key": person_key,
        "at": int(time.time()),
        "tasks": [{"key": (t.get("_pulse") or {}).get("key") or "",
                   "title": str(t.get("title") or "")[:120],
                   "tracker": t.get("tracker") or "",
                   "tracker_task_id": t.get("tracker_task_id") or ""}
                  for t in tasks],
    }
    # чистка протухших чатов
    cutoff = time.time() - _CTX_TTL_DAYS * 86400
    for k in list(idx.keys()):
        if float(idx[k].get("at") or 0) < cutoff:
            del idx[k]
    _save_index(idx)


def classify_reply(text: str) -> str:
    """→ done | blocked | postpone | in_progress | comment."""
    low = " " + str(text or "").lower() + " "
    if any(w in low for w in ("✅", "готово", "готов", "сделал", "сделано",
                              "закрыл", "выполнил")):
        return "done"
    if any(w in low for w in ("🧱", "упёрся", "уперся", "блокер", "застрял",
                              "не могу", "мешает")):
        return "blocked"
    if any(w in low for w in ("📅", "перенос", "перенести", "позже",
                              "не успеваю")):
        return "postpone"
    if any(w in low for w in ("🏃", "в работе", "делаю", "работаю")):
        return "in_progress"
    return "comment"


def select_task(tasks: List[Dict[str, Any]], text: str
                ) -> Optional[Dict[str, Any]]:
    """Номер в начале ответа выбирает задачу; одна задача — сама себя."""
    t = str(text or "").strip()
    if t[:1].isdigit():
        n = int(t.split()[0].rstrip(".):"))
        if 1 <= n <= len(tasks):
            return tasks[n - 1]
        return None
    return tasks[0] if len(tasks) == 1 else None


async def handle_reply(chat_id: str, text: str) -> Dict[str, Any]:
    """Обработать ответ сотрудника. Возврат: {"reply": <что ответить>, ...}."""
    ctx = _load_index().get(str(chat_id))
    if not ctx:
        return {"reply": ("Я — бот сверки задач вашей компании. Сейчас у меня "
                          "нет открытых вопросов к вам — отвечать нужно на "
                          "сообщения со списком задач."),
                "matched": False}

    tasks = ctx.get("tasks") or []
    task = select_task(tasks, text)
    if task is None:
        listing = "\n".join(f"{i + 1}. {t['title']}"
                            for i, t in enumerate(tasks))
        return {"reply": ("Про какую задачу это? Ответь с номером в начале, "
                          f"например «2 готово»:\n{listing}"),
                "matched": False}

    kind = classify_reply(text)
    owner_uid = ctx["owner_uid"]

    # ответ → комментарий в карточку задачника (если зеркало есть)
    tracker_ok = False
    if task.get("tracker") and task.get("tracker_task_id"):
        try:
            from backend.core.tasks.task_actions import comment_task
            res = await comment_task(
                owner_uid, task["tracker"], task["tracker_task_id"],
                f"[Пульс] Ответ исполнителя: {str(text).strip()}")
            tracker_ok = bool((res or {}).get("success", True))
        except Exception:
            logger.warning("pulse inbound: comment_task failed", exc_info=True)

    # лог ответа — для отчёта руководителю («почему не сделано» его словами)
    try:
        from backend.core.pulse.push_engine import (
            _load_push_state,
            _save_push_state,
        )
        state = _load_push_state(owner_uid)
        state.setdefault("responses", []).append({
            "at": int(time.time()),
            "person_key": ctx.get("person_key"),
            "task_key": task.get("key"),
            "task_title": task.get("title"),
            "kind": kind,
            "text": str(text).strip()[:500],
        })
        state["responses"] = state["responses"][-500:]
        _save_push_state(owner_uid, state)
    except Exception:
        logger.warning("pulse inbound: response log failed", exc_info=True)

    human = {"done": "Отлично, передам! Закрытие карточки подтвердит руководитель.",
             "blocked": "Понял, передам руководителю, что нужен разбор блокера.",
             "postpone": "Понял, передам запрос на перенос срока.",
             "in_progress": "Принято, работаешь — не мешаю.",
             "comment": "Записал."}[kind]
    if tracker_ok:
        human += " Комментарий уже в карточке задачи."
    return {"reply": f"{human}", "matched": True, "kind": kind,
            "task_title": task.get("title"), "tracker_commented": tracker_ok}
