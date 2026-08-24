# -*- coding: utf-8 -*-
"""
Task Actions — единый слой РЕДАКТИРОВАНИЯ задач в задачниках (YouGile/Trello/
Jira) из мозга. Недеструктивно: комментарий/вложение результата и смена
статуса (закрытие), БЕЗ удаления.

Запрос: «если агент выполнил задачу пользователя — он может по команде
добавить свой файл/текст выполнения к задаче из задачника; если просят
закрыть задачу — меняет статус, без удаления».

Реализация переиспользует УНИВЕРСАЛЬНЫЕ функции meetflow
(automation_tools_async: add_task_comment / move_task_universal /
update_task_universal) — они уже умеют все три задачника. Здесь только
тонкий мост (set_user_context + единый контракт) + резолв «done»-колонки
для закрытия и формат результата-выполнения.

Удаление сознательно НЕ проксируется (delete_task_universal не вызываем):
закрытие = перенос в done-колонку / transition, история задачи сохраняется.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from backend.core.utils.meetflow_path import default_meetflow_path as _dmp
MEETFLOW_PATH = _dmp()
SUPPORTED_SYSTEMS = ("yougile", "trello", "jira")
# слова, по которым ищем «готово»-колонку для закрытия задачи
_DONE_WORDS = ("done", "готово", "completed", "complete", "closed",
               "выполнено", "закрыт")


def _load_meetflow(user_id: str):
    """Подключить meetflow-функции в контексте пользователя."""
    if MEETFLOW_PATH not in sys.path:
        sys.path.insert(0, MEETFLOW_PATH)
    import automation_tools_async as mf
    mf.set_user_context(user_id=user_id)
    return mf


def _parse(result: str) -> Dict[str, Any]:
    try:
        return json.loads(result) if isinstance(result, str) else (result or {})
    except Exception:
        return {"raw": result}


async def comment_task(user_id: str, system: str, task_id: str,
                       text: str) -> Dict[str, Any]:
    """Добавить комментарий/заметку к задаче (текст выполнения и т.п.)."""
    if system not in SUPPORTED_SYSTEMS:
        return {"status": "error", "message": f"system must be one of {SUPPORTED_SYSTEMS}"}
    if not task_id or not text:
        return {"status": "error", "message": "task_id и text обязательны"}
    try:
        mf = _load_meetflow(user_id)
        res = _parse(await mf.add_task_comment(system, task_id, text))
        if res.get("error"):
            return {"status": "error", "message": res["error"]}
        return {"status": "success", "system": system, "task_id": task_id,
                "result": res}
    except Exception as e:
        logger.warning(f"comment_task failed: {e}")
        return {"status": "error", "message": str(e)}


async def attach_result(user_id: str, system: str, task_id: str, *,
                        result_text: str, author: str = "Tessbrain agent",
                        title: str = "Результат выполнения") -> Dict[str, Any]:
    """Прикрепить РЕЗУЛЬТАТ выполнения задачи (текст/файл-контент) как
    структурированный комментарий. Если агент сделал работу — фиксируем её
    в самой задаче задачника."""
    body = (f"✅ {title} ({author}):\n\n{result_text}").strip()
    return await comment_task(user_id, system, task_id, body)


async def attach_files(user_id: str, system: str, task_id: str,
                       file_paths: list, *,
                       max_files: int = 5) -> Dict[str, Any]:
    """Прикрепить ГОТОВЫЕ ФАЙЛЫ (артефакты исполнителя: КП/финмодель/отчёт)
    к задаче задачника. Честно по возможностям API:
      - trello: POST /cards/{id}/attachments (multipart);
      - jira:   POST /issue/{key}/attachments (multipart + no-check);
      - yougile: file-API отсутствует → files_unsupported=True, вызывающий
        упоминает файлы текстом (комментарий/описание).
    Never-raise: по-файловые ошибки собираются в errors, остальные грузятся."""
    if system not in SUPPORTED_SYSTEMS:
        return {"status": "error", "message": f"system must be one of {SUPPORTED_SYSTEMS}"}
    if not task_id or not file_paths:
        return {"status": "error", "message": "task_id и file_paths обязательны"}

    attached, errors = [], []
    files_unsupported = False
    try:
        mf = _load_meetflow(user_id)
        if not hasattr(mf, "attach_task_file"):
            return {"status": "error",
                    "message": "meetflow без attach_task_file (обновите модуль)"}
        for fp in list(file_paths)[:max_files]:
            name = os.path.basename(str(fp))
            res = _parse(await mf.attach_task_file(system, task_id, str(fp)))
            if res.get("error"):
                if "does not support file attachments" in str(res["error"]):
                    files_unsupported = True
                errors.append({"file": name, "error": res["error"]})
            else:
                attached.append({"file": name, "url": res.get("url")})
    except Exception as e:
        logger.warning(f"attach_files failed: {e}")
        return {"status": "error", "message": str(e),
                "attached": attached, "errors": errors}

    status = "success" if attached and not errors else \
             ("partial" if attached else "error")
    return {"status": status, "system": system, "task_id": task_id,
            "attached": attached, "errors": errors,
            "files_unsupported": files_unsupported}


async def attach_or_describe(user_id: str, system: str, task_id: str, *,
                             text: str, author: str = "Tessbrain agent",
                             title: str = "ТЗ") -> Dict[str, Any]:
    """Приложить текст (ТЗ/результат) к задаче ЧЕСТНО по возможностям трекера.

    YouGile не имеет comment-API (add_task_comment вернёт ошибку) → кладём
    текст в описание задачи. Trello/Jira → комментарий (attach_result).
    Файлы — отдельной функцией attach_files (trello/jira; yougile — нет)."""
    if system == "yougile":
        return await update_task(user_id, system, task_id, {
            "description": f"{title} ({author}):\n\n{text}"})
    return await attach_result(user_id, system, task_id,
                               result_text=text, author=author, title=title)


async def _find_done_column(mf, system: str, board_hint: str = "") -> Optional[str]:
    """Найти id «готово»-колонки (для закрытия). Best-effort по названию."""
    try:
        cols = _parse(await mf.list_task_columns(system=system))
        items = cols.get("columns") or []
        matches = []
        for c in items:
            # Адаптеры отдают заголовок колонки под ключом `name` (yougile/
            # trello/jira — task_adapters get_columns), а id — под `id`.
            # Старые ключи column_title/title оставлены для совместимости.
            title = str(c.get("name") or c.get("column_title")
                        or c.get("title") or "").lower()
            if board_hint and board_hint.lower() not in str(
                    c.get("board_title", "")).lower():
                continue
            if any(w in title for w in _DONE_WORDS) and not c.get("archived"):
                cid = c.get("id") or c.get("column_id")
                if cid:
                    matches.append(cid)
        # Возвращаем ТОЛЬКО при однозначном совпадении. Несколько done-колонок
        # (разные доски / Done+Closed) → None, чтобы не перенести задачу на
        # чужую доску — пусть вызывающий передаст явный target_column_id.
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.debug("find_done_column: %d matches, need explicit id",
                         len(matches))
    except Exception:
        logger.debug("find_done_column failed", exc_info=True)
    return None


async def close_task(user_id: str, system: str, task_id: str, *,
                     target_column_id: str = "",
                     transition_name: str = "",
                     result_text: str = "") -> Dict[str, Any]:
    """Закрыть задачу СМЕНОЙ СТАТУСА (без удаления).

    - yougile/trello: перенос в done-колонку (target_column_id или
      автопоиск по названию);
    - jira: transition в статус (transition_name, default 'Done');
    - result_text (опц.): сначала прикрепляем результат комментарием.
    """
    if system not in SUPPORTED_SYSTEMS:
        return {"status": "error", "message": f"system must be one of {SUPPORTED_SYSTEMS}"}
    if not task_id:
        return {"status": "error", "message": "task_id обязателен"}
    try:
        mf = _load_meetflow(user_id)

        attached = None
        if result_text:
            attached = await attach_result(user_id, system, task_id,
                                           result_text=result_text)

        if system == "jira":
            # jira-адаптер move_task(task_id, status_name) делает transition
            res = _parse(await mf.move_task_universal(
                system, task_id, transition_name or "Done"))
        else:
            col = target_column_id or await _find_done_column(mf, system)
            if not col:
                return {"status": "error", "task_id": task_id,
                        "message": "не найдена done-колонка; передай target_column_id",
                        "attached": attached}
            res = _parse(await mf.move_task_universal(system, task_id, col))

        if res.get("error"):
            return {"status": "error", "message": res["error"],
                    "attached": attached}
        return {"status": "success", "system": system, "task_id": task_id,
                "closed": True, "result": res, "attached": attached}
    except Exception as e:
        logger.warning(f"close_task failed: {e}")
        return {"status": "error", "message": str(e)}


async def update_task(user_id: str, system: str, task_id: str,
                      fields: Dict[str, Any]) -> Dict[str, Any]:
    """Обновить поля задачи (title/description/priority/assignee/due_date).
    Недеструктивно. fields с пустыми значениями игнорируются."""
    if system not in SUPPORTED_SYSTEMS:
        return {"status": "error", "message": f"system must be one of {SUPPORTED_SYSTEMS}"}
    try:
        mf = _load_meetflow(user_id)
        res = _parse(await mf.update_task_universal(
            system, task_id,
            title=fields.get("title", ""),
            description=fields.get("description", ""),
            priority=fields.get("priority", ""),
            assignee=fields.get("assignee", ""),
            due_date=fields.get("due_date", "")))
        if res.get("error"):
            return {"status": "error", "message": res["error"]}
        return {"status": "success", "system": system, "task_id": task_id,
                "result": res}
    except Exception as e:
        logger.warning(f"update_task failed: {e}")
        return {"status": "error", "message": str(e)}


async def create_and_prepare_task(
    user_id: str, system: str, *, title: str, tz_text: str,
    column_id: str = "", description: str = "", priority: str = "medium",
    assignee: str = "", due_date: str = "",
    mark_done: bool = False, transition_name: str = "Done",
    target_column_id: str = "",
) -> Dict[str, Any]:
    """L2: создать задачу в трекере + приложить ТЗ + (опц.) пометить «done».

    Создаёт задачу (`create_task_universal`), прикладывает ТЗ честным способом
    (`attach_or_describe`: yougile → описание, trello/jira → комментарий), и —
    если `mark_done` — закрывает ЧЕСТНО там, где API это позволяет:
    - jira: transition в статус `transition_name` (default «Done»);
    - trello: перенос в done-list (`target_column_id`, или автопоиск по имени);
    - yougile: перенос в done-колонку (`target_column_id`/автопоиск) — работает
      после фикса адаптера (columnId).

    column_id — куда создавать: yougile column / trello list / jira project_key
    (обязателен для create во всех трёх). Linear не поддержан (нет адаптера)."""
    if system not in SUPPORTED_SYSTEMS:
        return {"status": "error",
                "message": f"system must be one of {SUPPORTED_SYSTEMS} (linear не поддержан)"}
    try:
        mf = _load_meetflow(user_id)
        created = _parse(await mf.create_task_universal(
            system, title, column_id=column_id, description=description,
            priority=priority, assignee=assignee, due_date=due_date))
    except Exception as e:
        logger.warning(f"create_and_prepare_task create failed: {e}")
        return {"status": "error", "message": str(e)}

    task_id = created.get("task_id") or created.get("id")
    if created.get("error") or not task_id:
        return {"status": "error",
                "message": created.get("error") or "create failed",
                "result": created}

    attached = await attach_or_describe(user_id, system, str(task_id),
                                        text=tz_text)
    closed = None
    if mark_done:
        closed = await close_task(user_id, system, str(task_id),
                                  transition_name=transition_name,
                                  target_column_id=target_column_id)

    # Честный статус: задача создана, но приложение ТЗ / пометка «done» могли
    # молча не сработать (напр. yougile без done-колонки). Не врём «success».
    warnings = []
    if isinstance(attached, dict) and attached.get("status") == "error":
        warnings.append(f"ТЗ не приложено: {attached.get('message')}")
    if isinstance(closed, dict) and closed.get("status") == "error":
        warnings.append(f"не помечено done: {closed.get('message')}")
    status = "partial" if warnings else "success"
    return {"status": status, "system": system, "task_id": str(task_id),
            "attached": attached, "closed": closed, "warnings": warnings}
