# -*- coding: utf-8 -*-
"""Board «Мои доски» — серверное хранение досок (P1 BOARD_ROADMAP).

CRUD над board_workflows (миграция 264). Анти-IDOR: user_id из ВАЛИДНОГО токена
имеет приоритет над ?user_id= (как в documents.py). Хранилище — Supabase REST.
Граф хранится как JSONB; тяжёлые картинки в граф не кладём (только ссылки).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from litestar import Router, delete, get, patch, post
from litestar.params import Parameter

from backend.db.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

_KINDS = {"creative", "process"}
_LIST_COLS = "id,name,kind,created_at,updated_at"


def _new_id() -> str:
    return f"board_{uuid.uuid4().hex[:16]}"


def _model_id_override(model: str) -> Optional[str]:
    """Переопределение API-id модели из env BANANA_MODEL_IDS (JSON:
    {"<выбор с карточки>": "<реальный id провайдера>"}). Позволяет чинить
    несовпавший id БЕЗ правки кода. Never-raise."""
    import json
    import os
    raw = os.environ.get("BANANA_MODEL_IDS", "").strip()
    if not raw:
        return None
    try:
        m = json.loads(raw)
        v = m.get(model) if isinstance(m, dict) else None
        return str(v).strip() or None if v else None
    except Exception:
        return None


def _resolve_uid(authorization: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """Верифицированный токен > query (анти-IDOR). None = отказ/нет юзера."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning("boards: токен валиден, но запрошен чужой user_id — отказ")
        return None
    except Exception:
        logger.debug("boards: trusted_user_id unavailable", exc_info=True)
    return user_id or None


def _table_missing(err: str) -> bool:
    e = err.lower()
    return "does not exist" in e or "42p01" in e or ("board_workflows" in e and "404" in e)


@get("/")
async def list_boards(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    kind: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Список «Мои доски» (без тяжёлого graph). Пусто, если таблицы нет."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    try:
        params = {
            "user_id": f"eq.{uid}", "select": _LIST_COLS,
            "order": "updated_at.desc", "limit": max(1, min(int(limit), 500)),
        }
        if kind in _KINDS:
            params["kind"] = f"eq.{kind}"
        rows = await get_supabase_client()._request(
            "GET", "/rest/v1/board_workflows", params=params)
        return {"status": "success", "boards": rows or []}
    except Exception as e:
        if _table_missing(str(e)):
            return {"status": "success", "boards": [], "migration_required": True}
        logger.error("list_boards failed: %s", e)
        return {"status": "error", "message": str(e)}


@get("/schedules")
async def list_board_schedules(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Повторяющиеся процессы: процессные доски со schedule-триггером —
    спека расписания, последний запуск, оценка следующего. Для панели
    «за чем следить» на Доске."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    try:
        rows = await get_supabase_client()._request(
            "GET", "/rest/v1/board_workflows",
            params={"user_id": f"eq.{uid}", "kind": "eq.process",
                    "select": "id,name,graph,last_triggered_at,updated_at",
                    "order": "updated_at.desc", "limit": "200"}) or []
    except Exception as e:
        if _table_missing(str(e)):
            return {"status": "success", "schedules": []}
        logger.error("list_board_schedules failed: %s", e)
        return {"status": "error", "message": str(e)}

    from datetime import datetime, timedelta, timezone

    from backend.core.board.triggers import (
        is_due_spec,
        schedule_spec,
        triggers_enabled,
    )
    now = datetime.now(timezone.utc)

    _GENERIC = ("Процесс", "Без названия", "Untitled", "Process")

    def _auto_name(graph: Dict[str, Any]) -> str:
        """Имя из самой схемы, когда сохранённое имя — генерик: подпись
        узла, который кормит доставку («Инфографика встречи», «Комикс
        недели»), иначе — последний подписанный узел."""
        nodes = graph.get("nodes") or []
        by_id = {str(n.get("id")): n for n in nodes}
        deliv = {"notify", "output", "task", "action", "report_xlsx"}
        best = ""
        for e in (graph.get("edges") or []):
            t = by_id.get(str(e.get("target")))
            s = by_id.get(str(e.get("source")))
            if t is not None and str(t.get("type")) in deliv and s is not None:
                lbl = str(((s.get("data") or {}).get("label")) or "").strip()
                if lbl:
                    best = lbl
        if best:
            return best
        for n in reversed(nodes):
            if str(n.get("type")) == "note":
                continue
            lbl = str(((n.get("data") or {}).get("label")) or "").strip()
            if lbl:
                return lbl
        return ""

    def _display_name(b: Dict[str, Any]) -> str:
        """Имя для списка: сохранённое; генерик «Процесс» (авто-сохранение
        кнопки Run — он писал генерик и в имя доски, и в граф) заменяем
        именем графа, а если и оно генерик — именем из самой схемы."""
        raw = str(b.get("name") or "").strip()
        if raw and raw not in _GENERIC:
            return raw
        g = b.get("graph") or {}
        g_name = str(g.get("name") or "").strip()
        if g_name and g_name not in _GENERIC:
            return g_name
        return _auto_name(g) or raw or "Процесс"

    def _flow_summary(graph: Dict[str, Any]) -> str:
        """«Что делает»: цепочка узлов человеческими словами —
        «Данные встречи → Генерация → Уведомление». Различает доски
        с одинаковыми именами."""
        try:
            from backend.core.board.process_engine import _node_name
            parts = [
                _node_name(n) for n in (graph.get("nodes") or [])
                if str(n.get("type")) not in ("note", "trigger")]
            if not parts:
                return ""
            head = parts[:4]
            return " → ".join(head) + (" → …" if len(parts) > 4 else "")
        except Exception:
            return ""

    out = []
    for b in rows:
        spec = schedule_spec(b.get("graph") or {})
        if spec is None:
            continue
        last = b.get("last_triggered_at")
        # оценка следующего запуска (UTC): interval — от последнего;
        # daily — сегодня/завтра в заданное время
        next_at = None
        try:
            if spec["kind"] == "interval":
                base = datetime.fromisoformat(str(last).replace("Z", "+00:00")) \
                    if last else now
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
                next_at = base + timedelta(minutes=int(spec["minutes"]))
            else:
                h, m = str(spec["time"]).split(":")
                cand = now.replace(hour=int(h), minute=int(m),
                                   second=0, microsecond=0)
                if not is_due_spec(spec, last, now):
                    # сегодня уже бегали или время не наступило
                    if cand <= now:
                        cand = cand + timedelta(days=1)
                next_at = cand
        except Exception:
            logger.debug("schedule next estimate skipped", exc_info=True)
        out.append({
            "id": b.get("id"), "name": _display_name(b),
            "summary": _flow_summary(b.get("graph") or {}),
            "trigger": "schedule",
            "enabled": True,
            "schedule": spec, "last_triggered_at": last,
            "next_estimate": next_at.isoformat() if next_at else None,
            "due_now": is_due_spec(spec, last, now),
        })
    # Событийные автоматизации — в тот же список: пользователь должен видеть
    # ВСЁ, что сработает само, и уметь выключить в один клик.
    from backend.core.board.event_triggers import (
        event_trigger_enabled,
        event_trigger_type,
    )
    for b in rows:
        g = b.get("graph") or {}
        ev = event_trigger_type(g)
        if not ev:
            continue
        # выключенное расписание (enabled=false) тоже показываем — see below
        out.append({
            "id": b.get("id"), "name": _display_name(b),
            "summary": _flow_summary(g),
            "trigger": "event", "event_type": ev,
            "enabled": event_trigger_enabled(g),
            "last_triggered_at": b.get("last_triggered_at"),
        })
    # Выключенные schedule-доски: schedule_spec теперь возвращает None при
    # enabled=false — достаём их отдельно, чтобы их было видно и можно было
    # включить обратно.
    for b in rows:
        g = b.get("graph") or {}
        for n in (g.get("nodes") or []):
            if str(n.get("type")) == "trigger":
                d = n.get("data") or {}
                if (str(d.get("trigger_type")) == "schedule"
                        and d.get("enabled") is False):
                    out.append({
                        "id": b.get("id"),
                        "name": _display_name(b),
                        "summary": _flow_summary(g),
                        "trigger": "schedule", "enabled": False,
                        "last_triggered_at": b.get("last_triggered_at"),
                    })
                break
    return {"status": "success", "schedules": out,
            "triggers_enabled": triggers_enabled()}


@post("/{board_id:str}/toggle-automation")
async def toggle_board_automation(
    board_id: str,
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Вкл/выкл автоматизацию доски одним кликом (без открытия доски):
    ставит trigger.data.enabled в графе. body: {enabled: bool, user_id?}."""
    uid = _resolve_uid(authorization, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    enabled = bool((data or {}).get("enabled", True))
    try:
        sb = get_supabase_client()
        rows = await sb._request(
            "GET", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}",
                    "select": "id,graph"}) or []
        if not rows:
            return {"status": "error", "message": "доска не найдена"}
        graph = rows[0].get("graph") or {}
        touched = False
        for n in (graph.get("nodes") or []):
            if str(n.get("type")) == "trigger":
                n.setdefault("data", {})["enabled"] = enabled
                touched = True
                break
        if not touched:
            return {"status": "error", "message": "у доски нет триггера"}
        await sb._request(
            "PATCH", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}"},
            json_data={"graph": graph})
        return {"status": "success", "board_id": board_id, "enabled": enabled}
    except Exception as e:
        logger.error("toggle automation failed: %s", e)
        return {"status": "error", "message": str(e)}


@get("/{board_id:str}")
async def get_board(
    board_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Одна доска целиком (с graph). Только своя (scoped по user_id)."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    try:
        rows = await get_supabase_client()._request(
            "GET", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}", "select": "*"})
        if not rows:
            return {"status": "error", "message": "Board not found"}
        return {"status": "success", "board": rows[0]}
    except Exception as e:
        logger.error("get_board failed: %s", e)
        return {"status": "error", "message": str(e)}


@post("/", status_code=201)
async def create_board(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Создать доску. Body: {name?, kind?, graph}."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    kind = str(data.get("kind") or "creative").strip().lower()
    if kind not in _KINDS:
        kind = "creative"
    row = {
        "id": _new_id(),
        "user_id": uid,
        "name": (str(data.get("name") or "").strip() or "Без названия")[:200],
        "kind": kind,
        "graph": data.get("graph") or {},
    }
    try:
        res = await get_supabase_client()._request(
            "POST", "/rest/v1/board_workflows",
            json_data=row, headers={"Prefer": "return=representation"})
        return {"status": "success", "board": (res[0] if res else row)}
    except Exception as e:
        if _table_missing(str(e)):
            return {"status": "error", "message": "board_workflows table missing — run migration 264", "migration_required": True}
        logger.error("create_board failed: %s", e)
        return {"status": "error", "message": str(e)}


@patch("/{board_id:str}")
async def update_board(
    board_id: str,
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Обновить доску (partial). Меняем только переданные поля; всегда своя."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    patch_row: Dict[str, Any] = {"updated_at": "now()"}
    if "name" in data:
        patch_row["name"] = (str(data.get("name") or "").strip() or "Без названия")[:200]
    if "graph" in data:
        patch_row["graph"] = data.get("graph") or {}
    if "kind" in data:
        k = str(data.get("kind") or "").strip().lower()
        if k in _KINDS:
            patch_row["kind"] = k
    try:
        res = await get_supabase_client()._request(
            "PATCH", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}"},
            json_data=patch_row, headers={"Prefer": "return=representation"})
        if not res:
            return {"status": "error", "message": "Board not found"}
        return {"status": "success", "board": res[0]}
    except Exception as e:
        logger.error("update_board failed: %s", e)
        return {"status": "error", "message": str(e)}


@delete("/{board_id:str}", status_code=200)
async def delete_board(
    board_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Удалить свою доску."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    try:
        # return=representation → Supabase отдаёт удалённые строки: знаем,
        # СКОЛЬКО реально удалилось (0 = доски не было / чужая / уже удалена).
        deleted = await get_supabase_client()._request(
            "DELETE", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}"},
            headers={"Prefer": "return=representation"}) or []
        n = len(deleted) if isinstance(deleted, list) else 0
        name = str((deleted[0].get("name") if n else "") or board_id)
        # Наблюдаемость: удаление доски раньше было молчаливым — пользователь
        # не видел его в логах. Теперь пишем явно (и что именно ушло).
        logger.info("🗑 доска удалена: %s («%s») user=%s — удалено строк: %d",
                    board_id, name, str(uid)[:8], n)
        return {"status": "success", "id": board_id, "deleted": n}
    except Exception as e:
        logger.error("delete_board failed (%s): %s", board_id, e)
        return {"status": "error", "message": str(e)}


@post("/{board_id:str}/run", status_code=200)
async def run_board(
    board_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    lang: Optional[str] = Parameter(query="lang", default=None, required=False),
) -> Dict[str, Any]:
    """Прогнать доску-процесс на СЕРВЕРЕ (P3). kind=creative → отказ (креатив
    гоняется в браузере). Исполнение узлов — за флагом BOARD_PROCESS_EXEC;
    иначе dry-run (только порядок узлов)."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    try:
        rows = await get_supabase_client()._request(
            "GET", "/rest/v1/board_workflows",
            params={"id": f"eq.{board_id}", "user_id": f"eq.{uid}", "select": "*"})
        if not rows:
            return {"status": "error", "message": "Board not found"}
        board = rows[0]
        if str(board.get("kind")) != "process":
            return {"status": "error", "message": "Это не процесс-доска (kind≠process). Креатив гоняется в браузере."}
        from backend.core.board.process_engine import run_process_board
        # lang — язык интерфейса пользователя (ru|en): узлы генерации/мозга/
        # инфографики отвечают на нём. Пусто → прежнее поведение (ru).
        return await run_process_board(board.get("graph") or {}, user_id=uid,
                                       lang=lang)
    except Exception as e:
        logger.error("run_board failed: %s", e)
        return {"status": "error", "message": str(e)}


@post("/design", status_code=200)
async def design_board(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Процесс из естественного языка (F): body {request, current?} —
    описание процесса своими словами. Premium-модель раскладывает его в граф
    из существующих типов блоков, КОД валидирует (типы/связи/DAG/поля/
    расписание), каждый блок получает комментарий «что/зачем», сводка —
    note-узлом на холсте.

    current (опц., F.2) — текущий workflow доски {nodes, edges}: тогда это
    ПРАВКА словами («добавь ещё уведомление в почту»), позиции неизменных
    блоков сохраняются.

    Возвращает {success, workflow, title, summary, blocks, warnings, edited}
    — ничего не сохраняет: фронт кладёт workflow в редактор, пользователь
    смотрит, правит и осознанно запускает."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    request_text = str((data or {}).get("request") or "")
    current = (data or {}).get("current")
    if not isinstance(current, dict):
        current = None
    try:
        from backend.core.board.nl_designer import design_process
        return await design_process(uid, request_text, current_workflow=current)
    except Exception as e:
        logger.error("design_board failed: %s", e)
        return {"success": False, "error": str(e)}


@get("/brand")
async def get_brand_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Фирменный профиль тенанта для визуальных отчётов (палитра/стиль/лого/…)."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.board.brand_profile import get_brand
    return {"status": "success", "brand": get_brand(uid) or {}}


@post("/brand")
async def set_brand_route(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Сохранить фирменный профиль. Body: {name?, palette[], illustration_style?,
    tone?, logo?, forbidden_metaphors[], preferred_metaphors[]}. Пустой → удалить."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.board.brand_profile import set_brand
    brand = (data or {}).get("brand") if isinstance((data or {}).get("brand"), dict) else (data or {})
    saved = set_brand(uid, brand or {})
    return {"status": "success", "brand": saved or {}}


@get("/brand/assets")
async def list_brand_assets_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Файлы фирменного стиля (логотип/бланки). Для картинок отдаём data-URI
    превью (мелкие), для крупных — только метаданные."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.board.brand_assets import get_asset_data_uri, list_assets
    items = list_assets(uid)
    out = []
    for rec in items:
        row = {k: rec.get(k) for k in ("asset_id", "kind", "filename",
                                       "content_type", "size", "created_at")}
        ct = str(rec.get("content_type") or "")
        # Превью только для картинок и небольших файлов (не гоняем DOCX/большие PDF).
        if ct.startswith("image/") and int(rec.get("size") or 0) <= 2 * 1024 * 1024:
            row["preview"] = get_asset_data_uri(uid, str(rec.get("asset_id")))
        out.append(row)
    return {"status": "success", "assets": out}


@post("/brand/assets")
async def upload_brand_asset_route(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Загрузить логотип/бланк. Body: {kind: 'logo'|'letterhead', filename,
    content_type?, data: <base64 или data-URI>}."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    kind = str((data or {}).get("kind") or "").strip().lower()
    payload = str((data or {}).get("data") or "")
    if not payload:
        return {"status": "error", "message": "Пустой файл"}
    from backend.core.board.brand_assets import save_asset
    rec = save_asset(uid, kind, str((data or {}).get("filename") or ""),
                     payload, str((data or {}).get("content_type") or ""))
    if not rec:
        return {"status": "error", "message": (
            "Не удалось сохранить. Проверьте: назначение (logo/letterhead/"
            "style_ref), размер (до 8 МБ) и формат — принимаем PNG, JPEG, GIF, "
            "WebP, SVG, PDF и DOCX.")}
    return {"status": "success", "asset": rec}


@delete("/brand/assets/{asset_id:str}", status_code=200)
async def delete_brand_asset_route(
    asset_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Удалить файл фирменного стиля по asset_id."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.board.brand_assets import delete_asset
    ok = delete_asset(uid, asset_id)
    return {"status": "success" if ok else "error",
            "deleted": ok, "message": "" if ok else "Файл не найден"}


@post("/llm-generate")
async def board_llm_generate(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Генерация ТЕКСТА для креатив-узлов доски НА КЛЮЧАХ БЭКЕНДА (платформенная
    цепочка grok→deepseek→gemini + выбор тенанта). Фронтенд-роут /api/banana/llm
    проксирует сюда, когда во frontend/.env.local нет ключа провайдера — чтобы
    ключи жили в ОДНОМ месте (бэкенд), а не дублировались в двух env-файлах.
    Расход учитывается тут (cost_scope). Never-raise."""
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"success": False, "error": "Authentication required"}
    prompt = str((data or {}).get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt required"}
    model = str((data or {}).get("model") or "").strip()
    text = None

    # (1) Честно honor'им ВЫБРАННУЮ модель на ключах бэкенда (нативный API):
    # провайдер по префиксу id, ключ платформы из env. id берём из карты роутера
    # (env-override для anthropic) либо доверяем строке с карточки. Любой сбой —
    # тихо падаем в платформенную цепочку ниже (never-raise).
    if model:
        try:
            from backend.core.llm import model_router as R
            native = R.native_provider_for(model)
            model_id = _model_id_override(model) or R.native_model_id(model) or model
            if native and model_id:
                from backend.core.llm.extraction_route import _platform_key
                key = _platform_key(native)
                if key:
                    from backend.core.llm.native_callers import make_caller
                    from backend.core.llm.usage_tracker import cost_scope
                    caller = make_caller(native, model_id, key)
                    if caller:
                        async with cost_scope(user_id=uid, agent_mode="board",
                                              request_type="llm_generation",
                                              surface="board_creative"):
                            try:
                                text = await caller.generate(prompt)
                            finally:
                                # ВНУТРИ cost_scope: aclose() флашит расход в
                                # usage_tracker, а user_id берётся из контекста.
                                # Снаружи атрибуция терялась бы (user_id=NULL).
                                try:
                                    await caller.aclose()
                                except Exception:
                                    pass
        except Exception:
            logger.debug("board llm-generate native path skipped", exc_info=True)

    # (2) Фолбэк: платформенная цепочка (выбор тенанта → Google → дефолт).
    if not text or not str(text).strip():
        try:
            from backend.core.llm.usage_tracker import cost_scope
            from backend.core.llm.workload_policy import generate_for_workload
            async with cost_scope(user_id=uid, agent_mode="board",
                                  request_type="llm_generation", surface="board_creative"):
                text = await generate_for_workload(uid, "chat", prompt)
        except Exception as e:
            logger.warning("board llm-generate failed: %s", e)
            return {"success": False, "error": f"генерация не удалась: {e}"}
    if not text or not str(text).strip():
        return {"success": False,
                "error": ("модель не вернула текст — проверьте ключи провайдеров в "
                          ".env БЭКЕНДА (GEMINI/DEEPSEEK/XAI/OPENAI_API_KEY)")}
    return {"success": True, "text": str(text)}


@post("/image-generate")
async def board_image_generate(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Генерация КАРТИНКИ для узлов доски НА КЛЮЧАХ БЭКЕНДА (image_gen: выбранная
    модель + честный фолбэк на платформенные gemini/openai). Фронтенд-роут
    /api/banana/generate проксирует сюда, когда во frontend/.env.local нет ключа —
    единый источник ключей. Расход учитывается тут. Never-raise. Возврат
    {success, image: data-URL, provider} или {success:false, error}."""
    import base64 as _b64
    uid = _resolve_uid(authorization, user_id)
    if not uid:
        return {"success": False, "error": "Authentication required"}
    prompt = str((data or {}).get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt required"}
    preferred = str((data or {}).get("model") or "").strip() or None
    # Референс-картинки (правка/логотип) — data-URL или голый base64 → bytes.
    ref_images = []
    for im in ((data or {}).get("images") or [])[:4]:
        s = str(im or "")
        if "base64," in s:
            s = s.split("base64,", 1)[1]
        if s:
            try:
                ref_images.append(_b64.b64decode(s))
            except Exception:
                logger.debug("board image ref decode skipped", exc_info=True)
    try:
        from backend.core.board.image_gen import generate_image_png
        from backend.core.llm.usage_tracker import cost_scope
        async with cost_scope(user_id=uid, agent_mode="board",
                              request_type="image_generation", surface="board_creative"):
            res = await generate_image_png(prompt, uid, preferred=preferred,
                                           ref_images=ref_images or None)
    except Exception as e:  # never-raise наружу
        logger.warning("board image-generate failed: %s", e)
        return {"success": False, "error": f"генерация картинки не удалась: {e}"}
    png = (res or {}).get("png")
    if not png:
        return {"success": False,
                "error": ((res or {}).get("error")
                          or "модель не вернула изображение — проверьте ключи "
                             "GEMINI/OPENAI_API_KEY в .env БЭКЕНДА")}
    b64 = _b64.b64encode(png).decode("ascii")
    return {"success": True, "image": f"data:image/png;base64,{b64}",
            "provider": (res or {}).get("provider")}


router = Router(
    path="/boards",
    route_handlers=[list_boards, list_board_schedules, toggle_board_automation,
                    get_board, create_board,
                    update_board, delete_board, run_board, design_board,
                    board_llm_generate, board_image_generate,
                    get_brand_route, set_brand_route,
                    list_brand_assets_route, upload_brand_asset_route,
                    delete_brand_asset_route],
    tags=["Boards"],
)
