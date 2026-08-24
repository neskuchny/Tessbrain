# -*- coding: utf-8 -*-
"""Движок прогона доски-процесса (P3 BOARD_ROADMAP).

Берёт граф процесс-доски (nodes/edges) и исполняет его НА СЕРВЕРЕ: топологический
порядок, передача выхода узла вниз по рёбрам, ветвление через узел `condition`.
Курируемый набор блоков (НЕ полный BPMN) поверх существующих примитивов:
trigger / ask_brain / report / task / notify / condition.

Ядро (`_topo_order`, `run_graph`) — ЧИСТОЕ, handler инъектируется → юнит-тест
без сети. Реальные обработчики (`_process_handler`) — тонкие best-effort адаптеры
к агентам/отчётам/задачам/уведомлениям; при сбое узел даёт {"error": ...}, прогон
не падает. Исполнение за флагом BOARD_PROCESS_EXEC (по умолчанию ВЫКЛ).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.core.llm.untrusted import wrap_untrusted

logger = logging.getLogger(__name__)

# handler(node_type, node_data, inputs, ctx) -> output (str|dict)
Handler = Callable[[str, Dict[str, Any], List[Any], Dict[str, Any]], Awaitable[Any]]

_CONDITION = "condition"
_TRUE = {"true", "1", "yes", "on", "да"}


# ── Бюджет прогона: лимиты объявлены ДО старта, остановка — честная ──
# При исчерпании ЛЮБОГО лимита прогон останавливается, возвращает лучшее
# готовое (выходы уже исполненных узлов) и причину в логе (node="__budget__");
# итоговый статус — "partial", а не гладкий «успех». Дефолты щедрые: обычные
# доски (единицы–десятки узлов) их никогда не почувствуют.

def _budget_limits() -> Dict[str, int]:
    """Лимиты прогона из env (кривое/пустое значение → дефолт)."""
    def _env_int(name: str, default: int) -> int:
        try:
            v = int(str(os.getenv(name, "") or "").strip() or default)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default
    return {"max_nodes": _env_int("BOARD_MAX_NODES", 200),
            "max_seconds": _env_int("BOARD_MAX_SECONDS", 900),
            "max_llm_calls": _env_int("BOARD_MAX_LLM_CALLS", 120)}


# Типы узлов, обработчики которых внутри зовут LLM (проверено по _process_handler):
#   ask_brain / generate    — generate_for_workload / router.generate;
#   llmGenerate             — ProfileBackedClient / generate_for_workload / router;
#   report                  — generate_methodology_report (LLM внутри сервиса);
#   translate / doc_edit    — router.generate;
#   document, kp            — compose_document (LLM);
#   report_xlsx, xlsx       — _structure_xlsx_sheets → router.generate;
#   infographic             — extract/readable_caption (LLM) + image-модель;
#   coding_agent, handoff   — vibe-tasking конвейер (LLM-документ/агент).
# condition LLM НЕ зовёт (чистый предикат по upstream) — его в списке нет.
_LLM_NODE_TYPES = frozenset({
    "ask_brain", "generate", "llmGenerate", "report", "translate", "doc_edit",
    "document", "kp", "report_xlsx", "xlsx", "infographic",
    "coding_agent", "handoff",
})


def process_exec_enabled() -> bool:
    return os.getenv("BOARD_PROCESS_EXEC", "on").strip().lower() in ("1", "on", "true", "yes")


def board_wait_enabled() -> bool:
    """Флаг человек-в-цикле (§C). По умолчанию ВЫКЛ — узел «дождаться ответа»
    без флага ведёт себя как обычная отправка (не подвешивает прогон)."""
    return os.getenv("ENABLE_BOARD_WAIT", "").strip().lower() in ("1", "on", "true", "yes")


def _topo_order(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> List[str]:
    """Топологический порядок (Kahn). Узлы в циклах — в конец (best-effort)."""
    ids = [str(n.get("id")) for n in nodes if n.get("id") is not None]
    idset = set(ids)
    indeg = {i: 0 for i in ids}
    adj: Dict[str, List[str]] = {i: [] for i in ids}
    for e in edges:
        s, t = str(e.get("source")), str(e.get("target"))
        if s in idset and t in idset:
            adj[s].append(t)
            indeg[t] += 1
    q = deque([i for i in ids if indeg[i] == 0])
    order: List[str] = []
    while q:
        i = q.popleft()
        order.append(i)
        for t in adj[i]:
            indeg[t] -= 1
            if indeg[t] == 0:
                q.append(t)
    for i in ids:  # оставшиеся (циклы) — в конец
        if i not in order:
            order.append(i)
    return order


def _short(v: Any, n: int = 300) -> Any:
    if isinstance(v, str):
        return v[:n]
    if isinstance(v, dict):
        return {k: _short(x, 120) for k, x in list(v.items())[:12]}
    return v


async def run_graph(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    handler: Handler,
    ctx: Optional[Dict[str, Any]] = None,
    resume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Исполнить граф. Возвращает {"outputs", "log",
    "status": "done"|"waiting"|"partial"} — "partial" при исчерпании бюджета
    прогона (BOARD_MAX_NODES / BOARD_MAX_SECONDS / BOARD_MAX_LLM_CALLS).

    Человек-в-цикле (§C): если handler узла вернул {"__wait__": {...}} — прогон
    ПРИОСТАНАВЛИВАЕТСЯ: возвращаем status="waiting" + resume_state (JSON-safe), из
    которого позже продолжаем через resume=. Существующие узлы никогда не
    возвращают __wait__ → для них поведение идентично прежнему однопроходному.

    resume: сохранённое resume_state + {"reply": текст ответа человека}. Продолжаем
    с сохранённой позиции, подставив ответ как выход wait-узла."""
    ctx = ctx or {}
    by_id = {str(n.get("id")): n for n in nodes if n.get("id") is not None}
    incoming: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        incoming.setdefault(str(e.get("target")), []).append(e)

    if resume:
        outputs = dict(resume.get("outputs") or {})
        active = dict(resume.get("active") or {})
        branch = dict(resume.get("branch") or {})
        order = list(resume.get("order") or _topo_order(nodes, edges))
        start_index = int(resume.get("next_index") or 0)
        # Подставляем ответ человека как выход wait-узла и активируем его.
        wnid = str(resume.get("wait_node_id") or "")
        if wnid:
            outputs[wnid] = {"text": str(resume.get("reply") or ""), "reply": str(resume.get("reply") or "")}
            active[wnid] = True
        log: List[Dict[str, Any]] = list(resume.get("log") or [])
    else:
        outputs, active, branch = {}, {}, {}
        order = _topo_order(nodes, edges)
        start_index = 0
        log = []

    # Бюджет прогона (см. _budget_limits): читаем env на КАЖДЫЙ прогон, а не
    # при импорте — иначе лимиты нельзя менять без рестарта (и тестировать).
    budget = _budget_limits()
    t_start = time.monotonic()
    executed = 0  # реально исполненных узлов (вызовов handler)
    llm_calls = int(ctx.get("llm_calls") or 0)

    for idx in range(start_index, len(order)):
        nid = order[idx]
        node = by_id.get(nid)
        if not node:
            continue
        # Заметка — аннотация холста, не исполняемый узел: не гоняем и не
        # шумим в логе прогона («неизвестный тип узла: note» пугал людей).
        # НО: если заметка оказалась ВНУТРИ потока (trigger → note → …), она
        # обязана быть ПРОЗРАЧНОЙ — пропустить активность и вход дальше. Иначе
        # у downstream-узла единственное входящее ребро (от note) окажется
        # неактивным, он «повиснет» (skipped), и вся схема «не сработает»,
        # хотя выглядит правильной. (Планировщик из слов вполне может вставить
        # note-блок в цепочку.)
        if str(node.get("type")) == "note":
            inc_n = incoming.get(nid, [])
            up = [outputs.get(str(e.get("source"))) for e in inc_n
                  if active.get(str(e.get("source")))]
            # активна, если это старт (нет входов) или есть активный вход
            active[nid] = bool((not inc_n) or up)
            if active[nid]:
                passv = next((v for v in up if v not in (None, "")), None)
                outputs[nid] = passv if passv is not None else {"text": ""}
            log.append({"node": nid, "type": "note", "skipped": True})
            continue
        inc = incoming.get(nid, [])

        # Активность: стартовый узел (без входов) — всегда; иначе — если есть
        # активное входящее ребро (для condition-источника хэндл должен совпасть
        # с выбранной веткой).
        if not inc:
            node_active = True
        else:
            node_active = False
            for e in inc:
                s = str(e.get("source"))
                if not active.get(s):
                    continue
                src = by_id.get(s, {})
                if str(src.get("type")) == _CONDITION:
                    h = str(e.get("sourceHandle") or "").strip().lower()
                    if h in ("true", "false") and branch.get(s) and h != branch.get(s):
                        continue
                node_active = True
                break

        if not node_active:
            log.append({"node": nid, "type": node.get("type"), "skipped": True})
            continue

        inputs = [outputs.get(str(e.get("source"))) for e in inc
                  if active.get(str(e.get("source")))]

        # Честный отказ вместо галлюцинаций: ВСЕ активные входы узла упали
        # ({"error": …} без текста) → узел НЕ запускаем, ошибку тянем дальше.
        # Иначе LLM-узлы (generate/ask_brain) на пустом входе сочиняли
        # правдоподобный «отчёт о встрече» с заглушками, а notify реально
        # отправлял его людям — при том, что meeting_data честно сообщил
        # «не задана встреча».
        def _input_failed(v: Any) -> bool:
            return (isinstance(v, dict) and bool(v.get("error"))
                    and not (v.get("text") or v.get("output")))
        if inputs and all(_input_failed(v) for v in inputs):
            root = next((str(v.get("error")) for v in inputs), "")
            # не наслаиваем префикс при протяжке через цепочку узлов
            msg = root if root.startswith("пропущен из-за ошибки входа") else \
                f"пропущен из-за ошибки входа: {root}"
            out: Any = {"error": msg, "input_failed": True}
            outputs[nid] = out
            active[nid] = True
            log.append({"node": nid, "type": node.get("type"), "output": _short(out)})
            continue

        # ── Бюджет: проверяем ПЕРЕД исполнением очередного узла ──
        # Превышен любой лимит → останавливаемся ЧЕСТНО: запись-остановка в
        # лог, статус "partial", выходы уже исполненных узлов сохранены.
        node_type = str(node.get("type") or "")
        over = None
        if executed >= budget["max_nodes"]:
            over = f"узлы: лимит BOARD_MAX_NODES={budget['max_nodes']}"
        elif (time.monotonic() - t_start) >= budget["max_seconds"]:
            over = f"время: лимит BOARD_MAX_SECONDS={budget['max_seconds']} с"
        elif node_type in _LLM_NODE_TYPES and llm_calls >= budget["max_llm_calls"]:
            over = f"LLM-вызовы: лимит BOARD_MAX_LLM_CALLS={budget['max_llm_calls']}"
        if over:
            remaining = [by_id[j] for j in order[idx:]
                         if j in by_id and str(by_id[j].get("type")) != "note"]
            names = ", ".join(_node_name(n) for n in remaining[:5])
            msg = (f"Бюджет прогона исчерпан ({over}): выполнено {executed} "
                   f"узлов, не выполнено {len(remaining)}"
                   + (f" ({names})" if names else "")
                   + ". Результаты выполненных узлов сохранены.")
            log.append({"node": "__budget__", "type": "__budget__",
                        "status": "stopped", "message": msg,
                        "output": {"text": msg}})
            return {"outputs": outputs, "log": log, "status": "partial",
                    "budget": {"reason": over, "executed": executed,
                               "not_executed": len(remaining)}}
        if node_type in _LLM_NODE_TYPES:
            llm_calls += 1
            ctx["llm_calls"] = llm_calls  # наблюдаемость: виден обработчикам
        executed += 1

        try:
            out = await handler(node_type,
                                node.get("data") or {}, inputs, ctx)
        except Exception as ex:  # узел упал — не роняем прогон
            out = {"error": str(ex)}
            logger.debug("process node %s failed: %s", nid, ex)

        # Пауза «дождаться ответа человека»: сохраняем состояние и выходим.
        if isinstance(out, dict) and out.get("__wait__"):
            outputs[nid] = {"__waiting__": True}
            active[nid] = True
            log.append({"node": nid, "type": node.get("type"), "waiting": True})
            return {"status": "waiting", "wait": out.get("__wait__"), "log": log,
                    "resume_state": {"outputs": outputs, "active": active,
                                     "branch": branch, "order": order,
                                     "next_index": idx + 1, "wait_node_id": nid}}

        outputs[nid] = out
        active[nid] = True
        if str(node.get("type")) == _CONDITION:
            b = out.get("branch") if isinstance(out, dict) else out
            branch[nid] = "true" if (b is True or str(b).strip().lower() in _TRUE) else "false"
        log.append({"node": nid, "type": node.get("type"), "output": _short(out)})

    return {"outputs": outputs, "log": log, "status": "done"}


# ── Реальные обработчики (best-effort адаптеры к примитивам) ────────────────

def _split_caption(caption: str, limit: int = 1000) -> tuple:
    """Telegram ограничивает подпись к медиа ~1024 символами. Раньше мы просто
    резали text[:1000] — человек получал обрубок вида «…🧠 Е». Теперь режем по
    границе строки/слова и возвращаем (подпись, полный_текст): полный текст
    досылается ОТДЕЛЬНЫМ сообщением сразу после медиа, чтобы ничего не терялось.

    Markdown приводим к чистому тексту: подпись к медиа идёт без parse_mode,
    сырые ** и ## в ней выглядят мусором."""
    from backend.core.notifications.telegram_format import markdown_to_telegram
    cap = markdown_to_telegram((caption or "").strip(), mode="plain")
    if len(cap) <= limit:
        return cap, ""
    cut = cap[:limit]
    sp = max(cut.rfind("\n"), cut.rfind(" "))
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…", cap


async def _send_caption_overflow(client, token: str, target: str, full_text: str) -> None:
    """Дослать полный текст отдельными сообщениями (лимит sendMessage 4096)."""
    for i in range(0, len(full_text), 4000):
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target, "text": full_text[i:i + 4000],
                      "disable_web_page_preview": True})
        except Exception:
            logger.debug("telegram overflow chunk skipped", exc_info=True)


async def _send_telegram_photo(user_id: Optional[str], image_file: str,
                               caption: str = "",
                               chat_id: Optional[str] = None) -> bool:
    """Фото в Telegram (sendPhoto multipart). Never-raise.

    chat_id задан явно (узел «Уведомление» → адресат: группа/личка/канал) →
    шлём туда. Иначе — привязанный по умолчанию (link → Default Chat ID)."""
    try:
        if not user_id:
            return False
        # Токен и chat_id через общий резолвер: платформенный env-токен ИЛИ BYO
        # bot_token из «Интеграций»; chat_id — явный ИЛИ привязка/Default Chat ID.
        from backend.core.messengers.links import (
            resolve_telegram_bot_token,
            resolve_telegram_chat_id,
        )
        token = await resolve_telegram_bot_token(user_id)
        target = str(chat_id or "").strip() or await resolve_telegram_chat_id(user_id)
        if not (token and target):
            return False
        with open(image_file, "rb") as f:
            png = f.read()
        cap, full = _split_caption(caption)
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": target, "caption": cap},
                files={"photo": ("image.png", png, "image/png")})
            ok = r.status_code == 200
            if ok and full:
                await _send_caption_overflow(client, token, target, full)
            return ok
    except Exception:
        logger.debug("telegram photo skipped", exc_info=True)
        return False


async def _send_telegram_audio(user_id: Optional[str], audio_file: str,
                               caption: str = "",
                               chat_id: Optional[str] = None) -> bool:
    """Аудио в Telegram (sendAudio). Never-raise. chat_id — как у фото."""
    try:
        if not user_id:
            return False
        from backend.core.messengers.links import (
            resolve_telegram_bot_token,
            resolve_telegram_chat_id,
        )
        token = await resolve_telegram_bot_token(user_id)
        target = str(chat_id or "").strip() or await resolve_telegram_chat_id(user_id)
        if not (token and target):
            return False
        with open(audio_file, "rb") as f:
            blob = f.read()
        cap, full = _split_caption(caption)
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendAudio",
                data={"chat_id": target, "caption": cap},
                files={"audio": ("report.mp3", blob, "audio/mpeg")})
            ok = r.status_code == 200
            if ok and full:
                await _send_caption_overflow(client, token, target, full)
            return ok
    except Exception:
        logger.debug("telegram audio skipped", exc_info=True)
        return False


async def _send_telegram_text(user_id: Optional[str], text: str,
                              chat_id: Optional[str] = None) -> bool:
    """Текст в Telegram на явный chat_id (узел «Уведомление» → адресат).
    Токен резолвится общим способом. Never-raise."""
    try:
        if not (user_id and text):
            return False
        from backend.core.messengers.links import (
            resolve_telegram_bot_token,
            resolve_telegram_chat_id,
        )
        token = await resolve_telegram_bot_token(user_id)
        target = str(chat_id or "").strip() or await resolve_telegram_chat_id(user_id)
        if not (token and target):
            return False
        # markdown → Telegram-HTML с фолбэком в чистый текст; длинный текст
        # режется на куски, а не обрубается на 4000 символов
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        res = await post_telegram_text(token, target, text)
        return bool(res["ok"])
    except Exception:
        logger.debug("telegram text skipped", exc_info=True)
        return False


async def _send_telegram_document(user_id: Optional[str], doc_file: str,
                                  caption: str = "",
                                  chat_id: Optional[str] = None,
                                  filename: str = "") -> bool:
    """Файл-документ в Telegram (sendDocument multipart). Never-raise."""
    try:
        if not user_id:
            return False
        from backend.core.messengers.links import (
            resolve_telegram_bot_token,
            resolve_telegram_chat_id,
        )
        token = await resolve_telegram_bot_token(user_id)
        target = str(chat_id or "").strip() or await resolve_telegram_chat_id(user_id)
        if not (token and target):
            return False
        with open(doc_file, "rb") as f:
            blob = f.read()
        import os as _os
        name = filename or _os.path.basename(doc_file) or "document"
        cap, full = _split_caption(caption)
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": target, "caption": cap},
                files={"document": (name, blob, "application/octet-stream")})
            ok = r.status_code == 200
            if ok and full:
                await _send_caption_overflow(client, token, target, full)
            return ok
    except Exception:
        logger.debug("telegram document skipped", exc_info=True)
        return False


def _save_board_doc(blob: bytes, ext: str, user_id: Optional[str]) -> str:
    """Сохранить документ (docx/pdf) в data/board_docs/<uid>/ и вернуть путь."""
    import uuid

    from backend.core.store.tenant_paths import _DATA_ROOT
    safe = "".join(c for c in str(user_id or "anon") if c.isalnum() or c == "-")[:40] or "anon"
    d = _DATA_ROOT / "board_docs" / safe
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex[:16]}.{ext.lstrip('.')}"
    path.write_bytes(blob)
    return str(path)


def _parse_tg_channels(q: str) -> List[str]:
    """Перечень публичных TG-каналов из запроса узла «Веб-поиск»:
    @name / t.me/name / t.me/s/name (через запятую/пробел). Если ХОТЬ ОДИН
    токен не похож на канал — это не перечень каналов, возвращаем []."""
    import re as _re
    toks = [tk for tk in _re.split(r"[,\s]+", (q or "").strip()) if tk]
    if not toks:
        return []
    chans: List[str] = []
    for tk in toks:
        m = (_re.match(r"^@([A-Za-z0-9_]{4,32})$", tk)
             or _re.match(r"^(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{4,32})/?$", tk))
        if not m:
            return []
        chans.append(m.group(1))
    return chans[:5]


async def _fetch_tg_channel_posts(channels: List[str]) -> Dict[str, Any]:
    """Свежие посты публичных TG-каналов (t.me/s/-скрейп, без API) — тем же
    модулем telegram_tools, что и research-автоматизация. Never-raise."""
    try:
        import sys as _sys

        from backend.core.utils.meetflow_path import default_meetflow_path
        _mp = default_meetflow_path()
        if _mp and _mp not in _sys.path:
            _sys.path.insert(0, _mp)
        from telegram_tools import get_telegram_channel_posts
    except Exception as e:
        return {"error": f"чтение TG-каналов недоступно на сервере ({e})"}
    parts: List[str] = []
    ok = 0
    for ch in channels:
        try:
            raw = await get_telegram_channel_posts(ch, limit=10)
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if d.get("success"):
                posts = d.get("posts") or []
                txt = "\n".join(f"- {str(p.get('text', ''))[:400]}"
                                for p in posts[:10])
                parts.append(f"### @{ch}\n{txt or '(постов нет)'}")
                ok += 1
            else:
                parts.append(f"### @{ch}\n(не прочитан: {d.get('error')})")
        except Exception as e:
            parts.append(f"### @{ch}\n(не прочитан: {e})")
    if not ok:
        return {"error": ("ни один канал не прочитан — проверьте, что каналы "
                          "публичные: " + " · ".join(parts)[:400])}
    # Пометка «внешний текст»: посты каналов пойдут в LLM-узлы через рамку
    # wrap_untrusted (см. _llm_text_of) — это данные, не инструкции.
    _src = (("посты Telegram-канала " if len(channels) == 1
             else "посты Telegram-каналов ")
            + ", ".join("@" + c for c in channels))
    return {"text": "\n\n".join(parts)[:20000], "untrusted_source": _src}


async def _fetch_page_text(user_id: Optional[str], url: str) -> Dict[str, Any]:
    """Содержимое веб-страницы для узла «Веб-поиск», когда на вход дали URL.

    Firecrawl (api_key из «Интеграции → Firecrawl»: рендерит JS-сайты,
    отдаёт markdown) → фолбэк: встроенный фетчер url_ingest (без JS,
    с SSRF-защитой). Never-raise: любые сбои — {"error": ...}.

    Успешный результат помечен untrusted_source: текст страницы уйдёт в
    LLM-узлы через рамку «данные, не инструкции» (см. _llm_text_of)."""
    from urllib.parse import urlsplit
    _dom = urlsplit(url).netloc or url[:60]
    key = ""
    try:
        from backend.core.integrations.user_keys_service import get_keys_service
        keys = await get_keys_service().get_keys(user_id or "", "firecrawl")
        key = str((keys or {}).get("api_key") or "").strip()
    except Exception:
        logger.debug("firecrawl keys lookup failed", exc_info=True)
    if key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as cl:
                r = await cl.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"url": url, "formats": ["markdown"]})
                if r.status_code == 200:
                    d = r.json() or {}
                    md = str(((d.get("data") or {}).get("markdown")) or "").strip()
                    if md:
                        return {"text": md[:20000],
                                "untrusted_source": f"веб-страница {_dom}",
                                "note": "страница прочитана через Firecrawl"}
                logger.info("firecrawl scrape HTTP %s — фолбэк на встроенный "
                            "фетчер", r.status_code)
        except Exception as e:
            logger.info("firecrawl scrape failed (%r) — фолбэк на встроенный "
                        "фетчер", e)
    try:
        from backend.core.documents.url_ingest import fetch_url_text
        res = await fetch_url_text(url)
    except Exception as e:
        return {"error": str(e)}
    if not (res or {}).get("ok"):
        return {"error": str((res or {}).get("error") or "страница не прочитана")}
    title = str(res.get("title") or "").strip()
    text = str(res.get("text") or "").strip()
    if not text:
        return {"error": "страница пуста или не отдала текст"}
    return {"text": (f"# {title}\n\n{text}" if title else text)[:20000],
            "untrusted_source": f"веб-страница {_dom}"}


def _notify_targets(data: Dict[str, Any]) -> List[str]:
    """Явные адресаты узла «Уведомление»: chat_id / chat_ids (через запятую).
    Пусто → доставка по умолчанию (привязка/Default Chat ID)."""
    raw = data.get("chat_ids") or data.get("chat_id") or ""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,\n;]+", str(raw))
    return [s.strip() for s in items if s and s.strip()]


def _answer_text(res: Any) -> str:
    """Текст-ответ из dataset_service (ask_dataset/try_dataset_route). Never-raise."""
    if not isinstance(res, dict):
        return str(res or "").strip()
    if res.get("success") is False:
        return ""
    for k in ("answer", "text", "summary", "result"):
        v = res.get(k)
        if v and str(v).strip():
            return str(v).strip()
    try:
        return json.dumps(res, ensure_ascii=False)[:2000]
    except Exception:
        return ""


def _parse_markdown_tables(text: str) -> List[Dict[str, Any]]:
    """Выделить markdown-таблицы (| a | b |) из текста в листы для xlsx.
    Детерминированно, без LLM. Разделительная строка (|---|---|) пропускается."""
    sheets: List[Dict[str, Any]] = []
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        is_row = ln.startswith("|") and ln.count("|") >= 2
        if not is_row:
            i += 1
            continue
        block: List[str] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            block.append(lines[i].strip())
            i += 1
        if len(block) < 2:
            continue

        def _cells(row: str) -> List[str]:
            parts = row.split("|")[1:-1] if row.endswith("|") else row.split("|")[1:]
            return [c.strip() for c in parts]

        header = _cells(block[0])
        body = block[1:]
        # вторая строка — разделитель (---|:--:) → пропускаем
        if body and re.fullmatch(r"[\s:|-]+", body[0]):
            body = body[1:]
        rows = [_cells(r) for r in body if r.strip("| ")]
        if header and rows:
            sheets.append({"name": f"Таблица {len(sheets) + 1}",
                           "columns": header, "rows": rows})
    return sheets


async def _structure_xlsx_sheets(upstream: str, instruction: str,
                                 user_id: Optional[str]) -> List[Dict[str, Any]]:
    """Данные предыдущих шагов → структурированные листы для .xlsx.

    Сначала LLM превращает текст/данные в строгий JSON (листы/колонки/строки,
    факты только из входа). Если LLM недоступен или вернул мусор — детермини-
    рованный фолбэк: разобрать markdown-таблицы из входа. Никаких выдуманных
    данных: и LLM-путь, и фолбэк работают ТОЛЬКО с содержимым входа."""
    sheets: List[Dict[str, Any]] = []
    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=user_id, session_id="board-xlsx", agent_mode="board")
        sys = (
            "Ты структурируешь данные в таблицу для Excel. На вход — текст/отчёт. "
            "Верни СТРОГО JSON без пояснений: "
            '{"sheets":[{"name":"...","columns":["..."],"rows":[["..."]]}]}. '
            "Каждая строка rows — массив значений в порядке columns. Числа — числами "
            "(без пробелов-разделителей и валютных знаков). Бери ТОЛЬКО факты из "
            "входа, ничего не придумывай. Если данных на несколько таблиц — сделай "
            "несколько листов с говорящими именами."
        )
        prompt = (f"Пожелание к отчёту: {instruction}\n\n" if instruction else "") + \
                 f"Данные:\n{upstream[:12000]}"
        raw = await get_llm_router().generate(prompt, system_prompt=sys, personalize=False)
        sheets = _sheets_from_json(raw)
    except Exception:
        logger.debug("xlsx LLM structuring skipped", exc_info=True)
    if not sheets:
        sheets = _parse_markdown_tables(upstream)
    return sheets


def _sheets_from_json(raw: Any) -> List[Dict[str, Any]]:
    """Достать [{name,columns,rows}] из ответа LLM (возможно в ```json-обёртке)."""
    s = str(raw or "").strip()
    if not s:
        return []
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return []
    items = obj.get("sheets") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for sh in items:
        if not isinstance(sh, dict):
            continue
        cols = [str(c) for c in (sh.get("columns") or []) if c is not None]
        rows_in = sh.get("rows") or []
        rows: List[List[Any]] = []
        for r in rows_in:
            if isinstance(r, list):
                rows.append(list(r))
            elif isinstance(r, dict):
                rows.append([r.get(c) for c in cols])
        if cols or rows:
            out.append({"name": str(sh.get("name") or f"Лист {len(out) + 1}"),
                        "columns": cols, "rows": rows})
    return out


async def _doc_requisites(user_id: Optional[str]) -> Dict[str, str]:
    """Реквизиты компании тенанта (для узла «Документ/КП»). Never-raise."""
    if not user_id:
        return {}
    try:
        from backend.db.supabase_client import SupabaseClient
        rows = await SupabaseClient()._request(
            "GET", "/rest/v1/user_integrations",
            params={"user_id": f"eq.{user_id}", "provider": "eq.document_requisites",
                    "select": "meta", "limit": "1"})
        if rows and isinstance(rows[0].get("meta"), dict):
            return {str(k): str(v) for k, v in rows[0]["meta"].items() if v is not None}
    except Exception:
        logger.debug("doc requisites load skipped", exc_info=True)
    return {}


def _with_ask_brain(user_id: Optional[str], data: Dict[str, Any],
                    key_facts: str, lang: str) -> str:
    """Замыкание воронки визуального отчёта: запомнить контекст отчёта и
    пригласить спросить бота (VISUAL_REPORTS §2.2, шаг «диалог»).

    Контекст (заголовок + факты) сохраняется per-user — следующий вопрос
    привязанному Telegram-боту получит его и будет понят («что за динамит у
    отдела продаж?»). Приглашение — строкой в текст-дубль. Never-raise."""
    try:
        from backend.core.board.report_context import remember_report
        title = str(data.get("label") or data.get("format") or "отчёт").strip()
        remember_report(user_id, title, key_facts)
        invite = ("\n\n🧠 Есть вопрос по отчёту? Просто напишите его боту — "
                  "он знает контекст." if lang != "en" else
                  "\n\n🧠 Question about this report? Just message the bot — "
                  "it knows the context.")
        return key_facts + invite
    except Exception:
        logger.debug("ask-brain funnel skipped", exc_info=True)
        return key_facts


_LANG_NAMES = {"en": "English", "ru": "русский", "de": "Deutsch",
               "es": "español", "fr": "français"}


def _lang_instruction(ctx: Dict[str, Any]) -> str:
    """Строка-инструкция «отвечай на языке интерфейса пользователя».

    Аудит: узлы generate/ask_brain всегда отвечали по-русски, каким бы ни был
    язык пользователя. lang приходит из UI при запуске (run_board?lang=..) и
    из расписаний/триггеров, если там задан. Пусто/ru → пустая строка
    (прежнее поведение байт-в-байт)."""
    lang = str(ctx.get("lang") or "").strip().lower()
    if not lang or lang == "ru":
        return ""
    name = _LANG_NAMES.get(lang, lang)
    return f"\nОтвечай на языке пользователя: {name} ({lang})."


def _text_of(inputs: List[Any]) -> str:
    parts = []
    for v in inputs:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            parts.append(str(v.get("text") or v.get("output") or ""))
    return "\n".join(p for p in parts if p).strip()


def _llm_text_of(inputs: List[Any]) -> str:
    """Как _text_of, но входы из ВНЕШНИХ источников — в рамке «данные, не
    инструкции» (wrap_untrusted). Внешний вход помечает сам узел-источник
    ключом untrusted_source в своём выходе (web_search: веб-страница /
    TG-каналы / результаты поиска). Рамка попадает ТОЛЬКО в LLM-промпты:
    notify/output/condition и прочие «человеческие» потребители продолжают
    брать сырой _text_of — рамка не уезжает людям в Telegram.

    Ограничение (честно): промежуточные узлы (prompt/textCombiner/note)
    пометку не переносят — рамка работает на ПРЯМОМ ребре источник → LLM-узел."""
    parts = []
    for v in inputs:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            t = str(v.get("text") or v.get("output") or "")
            src = str(v.get("untrusted_source") or "").strip()
            if t and src:
                t = wrap_untrusted(t, source=src)
            parts.append(t)
    return "\n".join(p for p in parts if p).strip()


# Явно выбранный на узле llmGenerate провайдер доски →
#   (серверный провайдер, id провайдера в «Интеграциях», env-ключи платформы).
# Ключ берём СНАЧАЛА из вкладки «Интеграции» тенанта (get_user_integration_keys),
# и только затем — из env платформы. DeepSeek/Qwen/xAI/OpenAI — OpenAI-совместимо,
# Anthropic — нативно, Google — Gemini SDK. Всё через ProfileBackedClient —
# реальные вызовы, без заглушек.
_BOARD_LLM_PROVIDERS: Dict[str, tuple] = {
    "google":    ("gemini",    "google_ai", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "openai":    ("openai",    "openai",    ("OPENAI_API_KEY",)),
    "anthropic": ("anthropic", "anthropic", ("ANTHROPIC_API_KEY",)),
    "deepseek":  ("deepseek",  "deepseek",  ("DEEPSEEK_API_KEY",)),
    "qwen":      ("qwen",      "qwen",      ("DASHSCOPE_API_KEY", "QWEN_API_KEY")),
    "xai":       ("xai",       "xai",       ("XAI_API_KEY",)),
}


async def _resolve_board_key(user_id: Optional[str], integ_provider: Optional[str],
                             env_names: tuple) -> Optional[str]:
    """Ключ провайдера: сперва вкладка «Интеграции» тенанта, затем env платформы."""
    if user_id and integ_provider:
        try:
            from backend.api.routes.integrations import get_user_integration_keys
            keys = await get_user_integration_keys(user_id, integ_provider) or {}
            k = str(keys.get("api_key") or "").strip()
            if k:
                return k
        except Exception:
            logger.debug("board key: integration lookup failed", exc_info=True)
    return next((os.environ.get(n) for n in env_names if os.environ.get(n)), None)


async def _generate_with_board_model(
    user_id: Optional[str], provider: str, model: str, prompt: str, *,
    temperature: float = 0.7, max_tokens: int = 2048,
) -> Optional[str]:
    """Сгенерировать текст ИМЕННО выбранными на узле провайдером/моделью.

    Ключ — из «Интеграций» тенанта (или env платформы как фолбэк). Возвращает
    текст при успехе; None — если ключа нет / провайдер не поддержан / генерация
    упала (вызывающий тогда откатывается на модель тенанта по умолчанию, чтобы
    существующие доски не сломались). Реальный вызов провайдера через
    ProfileBackedClient — без заглушек."""
    spec = _BOARD_LLM_PROVIDERS.get((provider or "").strip().lower())
    if not spec or not model:
        return None
    server_provider, integ_provider, env_names = spec
    api_key = await _resolve_board_key(user_id, integ_provider, env_names)
    if not api_key:
        return None  # ключа нет — мягкий откат на дефолт тенанта (не падаем)
    try:
        from backend.core.llm.profile_client import ProfileBackedClient
        client = ProfileBackedClient(
            provider=server_provider, base_url=None, model=model, api_key=api_key)
        if not client.enabled:
            return None
        text = await client.generate(
            prompt, temperature=temperature, max_tokens=max_tokens)
        return (text or "").strip() or None
    except Exception as e:
        logger.warning("llmGenerate board-model %s/%s failed: %s", provider, model, e)
        return None


async def _process_handler(node_type: str, data: Dict[str, Any],
                           inputs: List[Any], ctx: Dict[str, Any]) -> Any:
    """Диспатч типа узла на существующий примитив. Всё best-effort."""
    user_id = ctx.get("user_id")
    upstream = _text_of(inputs)
    # Тот же текст для LLM-промптов: внешние входы (веб/TG-каналы/поиск,
    # помеченные untrusted_source) — в рамке «данные, не инструкции».
    # upstream (сырой) остаётся для notify/output/condition и всего, что
    # уходит людям, — рамка не должна попадать в Telegram-сообщения.
    upstream_llm = _llm_text_of(inputs)

    if node_type in ("trigger", "start"):
        # Событийный триггер: данные события (напр. завершившейся встречи)
        # приходят в ctx["trigger_payload"] и текут в граф. Для manual/schedule
        # используется вписанный payload. Порядок: явный payload > событие.
        ev = ctx.get("trigger_payload")
        if str(data.get("trigger_type")) == "event" and ev:
            return {"text": str(data.get("payload") or ev)}
        return {"text": str(data.get("payload") or data.get("text") or upstream or "")}

    if node_type == "ask_brain":
        question = str(data.get("prompt") or data.get("question") or upstream_llm or "").strip()
        if not question:
            return {"error": "нет вопроса"}
        try:
            from backend.core.llm.router import get_llm_router, set_llm_context
            set_llm_context(user_id=user_id, session_id="board-process", agent_mode="board")

            # «Спросить МОЗГ» должен спрашивать мозг: раньше вопрос уходил
            # в голую LLM без данных компании, и «недельный дайджест»
            # отвечал «вставьте сюда свои заметки». Собираем контекст:
            # снапшот компании + релевантные знания (BM25) + цифры.
            ctx_parts: List[str] = []
            # Доска запущена СОБЫТИЕМ конкретной встречи → «последняя/эта
            # встреча» в вопросе — именно она. Без этого BM25 находил самую
            # «жирную» встречу индекса: инцидент — доски, сработавшие по
            # свежей tendee (3 чанка), прислали карту про ГигаЧат (34 чанка).
            _tmid = str(ctx.get("trigger_meeting_id") or "").strip()
            if _tmid:
                try:
                    from backend.core.board.meeting_artifacts import fetch_artifact
                    _art = await fetch_artifact(user_id or "", _tmid, "report")
                    if _art.get("empty") or _art.get("error"):
                        _art = await fetch_artifact(user_id or "", _tmid, "summary")
                    _atext = str(_art.get("text") or "").strip()
                    if _atext and not _art.get("error"):
                        ctx_parts.append(
                            "=== ВСТРЕЧА, ПО КОТОРОЙ ЗАПУЩЕН ПРОЦЕСС: "
                            f"«{_art.get('title') or _tmid}» ===\n"
                            "ВАЖНО: если вопрос про «последнюю встречу» / "
                            "«эту встречу» — речь именно о ней, НЕ о других "
                            "встречах из знаний ниже.\n" + _atext[:6000])
                except Exception:
                    logger.debug("ask_brain: trigger meeting ctx skipped",
                                 exc_info=True)
            try:
                from backend.core.sleep.enhanced_snapshot import (
                    get_enhanced_snapshot_generator,
                )
                from backend.core.store.graph_view import (
                    merged_graph_view_for_user,
                )
                gb = await merged_graph_view_for_user(user_id, use_networkx=None)
                gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
                gen.user_id = user_id or ""
                snap = await gen.get_company_snapshot_text()
                if snap:
                    ctx_parts.append("=== СНАПШОТ КОМПАНИИ ===\n" + snap[:4000])
                await gb.close(save=False)
            except Exception:
                logger.debug("ask_brain: snapshot ctx skipped", exc_info=True)
            try:
                # Раньше здесь был голый BM25 — один канал из трёх: вопрос
                # «покажи всех, кто…» узлу доски терял участников, которых
                # brain-чат находил (SEARCH_CONSUMERS_AUDIT, правка №2).
                # Теперь тот же гибрид, что у чата, с BM25-фолбэком внутри
                # хелпера; TESSENT_CTX_HYBRID=off возвращает прежний путь.
                from backend.core.search.context_fragments import topic_fragments
                frags, _engine = await topic_fragments(
                    user_id, question, top_k=6, build_if_missing=True)
                if frags:
                    ctx_parts.append("=== ЗНАНИЯ ПО ТЕМЕ ===\n" + "\n---\n".join(frags))
            except Exception:
                logger.debug("ask_brain: knowledge ctx skipped", exc_info=True)
            try:
                from backend.core.ontology.numbers_context import numbers_block
                nb = numbers_block(user_id or "", question)
                nb_text = (nb or {}).get("text") or (nb if isinstance(nb, str) else "")
                if nb_text:
                    ctx_parts.append("=== ЦИФРЫ КОМПАНИИ ===\n" + str(nb_text)[:2000])
            except Exception:
                logger.debug("ask_brain: numbers ctx skipped", exc_info=True)

            prompt = (
                ("\n\n".join(ctx_parts) + "\n\n") if ctx_parts else ""
            ) + (
                "Ответь на вопрос ПО ДАННЫМ выше (это база знаний компании). "
                "Опирайся ТОЛЬКО на эти данные: не выдумывай фактов, цифр, имён "
                "и выводов, которых в них нет. Если данных не хватает — скажи, "
                "каких именно, не проси пользователя что-то вставлять и не "
                "заполняй пробел правдоподобным."
                + _lang_instruction(ctx) + "\n\nВопрос: " + question)

            # Дайджест/анализ — работа для большой модели (workload-политика)
            text = None
            try:
                from backend.core.llm.workload_policy import generate_for_workload
                text = await generate_for_workload(
                    user_id or "", "search_deep_synthesis", prompt)
            except Exception:
                logger.debug("ask_brain: workload route skipped", exc_info=True)
            if not text:
                text = await get_llm_router().generate(prompt, personalize=False)
            return {"text": text, "context_used": len(ctx_parts)}
        except Exception as e:
            return {"error": f"ask_brain: {e}"}

    if node_type == "report":
        rtype = str(data.get("report_type") or "summary").strip()
        # Человеческие алиасы → реальные типы методологии. Раньше «summary»
        # (дефолт шаблона «недельный дайджест»!) падал «unknown report_type»;
        # незнакомый тип теперь уходит в custom, а не в ошибку.
        _ALIAS = {"summary": "project_summary", "саммари": "project_summary",
                  "digest": "project_summary", "дайджест": "project_summary",
                  "weekly": "project_summary", "progress": "project_progress",
                  "decisions": "key_decisions", "решения": "key_decisions"}
        try:
            from backend.core.reports.methodology_service import (
                METHODOLOGIES,
                generate_methodology_report,
            )
            rtype = _ALIAS.get(rtype.lower(), rtype)
            if rtype not in METHODOLOGIES:
                rtype = "custom"
            rep = await generate_methodology_report(
                user_id or "", rtype,
                days_back=int(data.get("days_back") or 30),
                custom_prompt=(upstream_llm or None),
                model_tier="standard",
                lang=str(ctx.get("lang") or ""))
            # Сервис отдаёт {"status": ..., "report": {...}} — раньше ключи
            # брались с ВНЕШНЕГО словаря, поэтому узел всегда возвращал
            # литерал «отчёт готов» и report_id=None вместо самого отчёта.
            if rep.get("status") == "no_data":
                return {"text": str(rep.get("message")
                                    or "данных за период нет"),
                        "report_type": rtype, "no_data": True}
            body = rep.get("report") or {}
            # Отчёт отдаём ЦЕЛИКОМ: раньше text[:2000] рубил его посреди фразы,
            # и следующий узел (или письмо в TG) получал обрубок вместо отчёта.
            text = str(body.get("content_text") or body.get("summary")
                       or "отчёт готов")
            return {"text": text, "report_id": body.get("id"),
                    "report_type": rtype}
        except Exception as e:
            return {"error": f"report: {e}"}

    if node_type == "notify":
        # {{input}} в тексте → подставляем результат предыдущего шага (как в
        # generate). Без подстановки шаблон «📊 Отчёт\n{{input}}» уходил в
        # Telegram С ЛИТЕРАЛЬНЫМ {{input}} и пустым телом — «ничего не пришло».
        raw_text = str(data.get("text") or "")
        if "{{input}}" in raw_text:
            text = raw_text.replace("{{input}}", upstream or "").strip()
        else:
            text = (raw_text or upstream or "").strip()
        channel = str(data.get("channel") or "telegram").lower()
        # Вложения с предыдущих шагов: картинка (nanoBanana/визуальный отчёт) и
        # аудио (озвучка). notify сам решает, ЧТО отправить — так один узел
        # доставки собирает «текст + картинку/аудио» (композиция доставки).
        image_file = next((str(v.get("image_file")) for v in inputs
                           if isinstance(v, dict) and v.get("image_file")), None)
        audio_file = next((str(v.get("audio_file")) for v in inputs
                           if isinstance(v, dict) and v.get("audio_file")), None)
        doc_file = next((str(v.get("doc_file")) for v in inputs
                         if isinstance(v, dict) and v.get("doc_file")), None)
        doc_name = next((str(v.get("doc_name")) for v in inputs
                         if isinstance(v, dict) and v.get("doc_name")), "") or ""

        # Наблюдаемость: раньше успешная отправка НИЧЕГО не писала в лог
        # (сендеры логируют только сбой на debug) — «не видно, что доска
        # вообще что-то шлёт». Пишем явно, ЧТО и КУДА уходит.
        _kind = ("документ" if doc_file else "аудио" if audio_file
                 else "фото+подпись" if image_file else "текст")
        logger.info("📨 notify: канал=%s тип=%s адресатов=%s текст=%dсимв (user=%s)",
                    channel, _kind, len(_notify_targets(data)) or "по умолчанию",
                    len(text or ""), user_id)

        async def _tg_send_one(cid: Optional[str]) -> bool:
            # Приоритет вложения: документ → аудио → фото → текст.
            if doc_file:
                ok = await _send_telegram_document(user_id, doc_file,
                                                   caption=text, chat_id=cid,
                                                   filename=doc_name)
            elif audio_file:
                ok = await _send_telegram_audio(user_id, audio_file,
                                                caption=text, chat_id=cid)
            elif image_file:
                ok = await _send_telegram_photo(user_id, image_file,
                                                caption=text, chat_id=cid)
            else:
                ok = await _send_telegram_text(user_id, text, chat_id=cid)
            logger.info("📨 notify→telegram: %s chat=%s (%s)", _kind,
                        cid or "по умолчанию", "✓ отправлено" if ok else "✗ НЕ отправлено")
            return ok

        # Явные адресаты (узел «Уведомление» → chat_id/chat_ids): шлём в КАЖДЫЙ.
        # Так одна доска может слать и в группу (публично), и лично руководителю —
        # разными узлами с разными ID. Пусто → доставка по умолчанию (ниже).
        targets = _notify_targets(data)
        if targets and channel == "telegram":
            results = []
            for cid in targets:
                ok = await _tg_send_one(cid)
                results.append({"chat_id": cid, "ok": ok})
            sent_any = any(r["ok"] for r in results)
            failed = [r["chat_id"] for r in results if not r["ok"]]
            out = {"ok": sent_any, "channel": "telegram", "text": text,
                   "targets": results, "photo": bool(image_file), "audio": bool(audio_file)}
            if failed:
                out["error"] = ("Не доставлено в: " + ", ".join(failed) +
                                ". Проверьте: бот добавлен в эти группы/каналы, "
                                "Chat ID верный, токен бота задан («Интеграции»).")
            return out

        if audio_file and channel == "telegram":
            sent = await _send_telegram_audio(user_id, audio_file, caption=text)
            if sent:
                return {"ok": True, "sent": True, "channel": "telegram",
                        "audio": True, "text": text or "аудио отправлено"}
            return {"ok": False, "channel": "telegram", "text": text,
                    "audio_file": audio_file,
                    "error": ("Аудио НЕ отправлено: нет токена бота или Chat ID. "
                              "Задайте их в «Интеграции → Telegram» и запустите снова.")}

        if image_file and channel == "telegram":
            sent = await _send_telegram_photo(user_id, image_file,
                                              caption=text)
            logger.info("📨 notify→telegram: фото+подпись chat=по умолчанию (%s)",
                        "✓ отправлено" if sent else "✗ НЕ отправлено")
            if sent:
                return {"ok": True, "sent": True, "channel": "telegram",
                        "photo": True, "text": text or "изображение отправлено"}
            # фото НЕ ушло → честно ✗, а не зелёный ✓ (это и есть «ничего не
            # произошло»): картинка есть, но доставка не настроена.
            return {"ok": False, "channel": "telegram", "text": text,
                    "image_file": image_file,
                    "error": ("Фото НЕ отправлено: нет токена бота или Chat ID. "
                              "Впишите Bot Token и Default Chat ID в «Интеграции → "
                              "Telegram» (или задайте TELEGRAM_BOT_TOKEN в окружении) "
                              "и запустите снова.")}

        # Документ-вложение (КП/договор из узла «Документ») → sendDocument.
        if doc_file and channel == "telegram":
            sent = await _send_telegram_document(user_id, doc_file, caption=text,
                                                 filename=doc_name)
            if sent:
                return {"ok": True, "sent": True, "channel": "telegram", "document": True,
                        "text": text or "документ отправлен"}
            return {"ok": False, "channel": "telegram", "text": text, "doc_file": doc_file,
                    "error": ("Документ НЕ отправлен: нет токена бота или Chat ID "
                              "(«Интеграции → Telegram»).")}

        # Email-канал: письмо на явный адрес(а) (клиент), с вложением-документом.
        if channel == "email":
            atts: List[Dict[str, Any]] = []
            if doc_file:
                try:
                    import base64 as _b64
                    with open(doc_file, "rb") as f:
                        blob = f.read()
                    _low = doc_file.lower()
                    if _low.endswith(".pdf"):
                        mime = "application/pdf"
                    elif _low.endswith(".xlsx"):
                        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    else:
                        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    atts = [{"filename": doc_name or "document",
                             "content_b64": _b64.b64encode(blob).decode("ascii"),
                             "mime": mime}]
                except Exception:
                    logger.debug("email attachment read skipped", exc_info=True)
            from backend.core.help.advice_push import notify_user_email
            subject = str(data.get("subject") or "Документ")
            body = text or " "
            recipients = _notify_targets(data)  # для email адрес(а) — в поле chat_id
            if recipients:
                sent_any = False
                for em in recipients:
                    sent_any = await notify_user_email(user_id, subject, body,
                                                       to=em, attachments=atts) or sent_any
            else:
                sent_any = await notify_user_email(user_id, subject, body, attachments=atts)
            if sent_any:
                return {"ok": True, "sent": True, "channel": "email",
                        "document": bool(atts), "text": text}
            return {"ok": False, "channel": "email", "text": text,
                    "error": ("Письмо НЕ отправлено: не настроен email-транспорт "
                              "(SMTP/SendGrid) или не указан адрес получателя.")}

        if not text:
            return {"error": "нечего отправлять"}
        try:
            from backend.core.help.advice_push import notify_user
            res = await notify_user(user_id, subject=data.get("subject") or "Процесс Tessbrain",
                                    text=text)
            # notify_user отдаёт {telegram: bool, email: bool}. Если НИЧЕГО не
            # ушло — не рапортуем «отправлено» (раньше dict был truthy → ложный ✓).
            delivered = (bool(res.get("telegram") or res.get("email"))
                         if isinstance(res, dict) else bool(res))
            if not delivered:
                return {"ok": False, "channel": channel, "text": text,
                        "error": ("Сообщение НЕ доставлено: канал не настроен. "
                                  "Привяжите Telegram (или почту) во вкладке "
                                  "«Интеграции» и запустите снова.")}
            return {"ok": True, "sent": res, "channel": channel, "text": text}
        except Exception as e:
            return {"error": f"notify: {e}"}

    # ── Человек-в-цикле (§C): «Отправить и дождаться ответа» ──
    # Узел шлёт сообщение в Telegram и ПРИОСТАНАВЛИВАЕТ прогон до ответа
    # человека (движок ловит {"__wait__": ...}). Без флага ENABLE_BOARD_WAIT —
    # ведёт себя как обычная отправка (не подвешивает): абсолютно безопасно,
    # старые доски с таким узлом (если появятся) просто доставят сообщение.
    if node_type in ("wait_reply", "ask_human"):
        # {{input}} → результат предыдущего шага (как в notify/generate).
        raw_text = str(data.get("text") or data.get("message") or "")
        if "{{input}}" in raw_text:
            text = raw_text.replace("{{input}}", upstream or "").strip()
        else:
            text = (raw_text or upstream or "").strip()
        # Куда шлём и откуда ждём ответ — один и тот же chat_id.
        targets = _notify_targets(data)
        cid = targets[0] if targets else None
        if not cid:
            try:
                from backend.core.messengers.links import resolve_telegram_chat_id
                cid = await resolve_telegram_chat_id(user_id)
            except Exception:
                cid = None
        sent = await _send_telegram_text(user_id, text, chat_id=cid) if text else False
        if not board_wait_enabled():
            # Флаг выключен → просто доставка, без ожидания.
            if sent:
                return {"ok": True, "sent": True, "channel": "telegram", "text": text,
                        "note": "ожидание ответа выключено (ENABLE_BOARD_WAIT)"}
            return {"ok": False, "channel": "telegram", "text": text,
                    "error": ("Сообщение НЕ отправлено: нет токена бота или Chat ID. "
                              "Настройте «Интеграции → Telegram» и запустите снова.")}
        if not (sent and cid):
            # Не смогли отправить/некуда ждать → не подвешиваем «в никуда».
            return {"ok": False, "channel": "telegram", "text": text,
                    "error": ("Не удалось отправить вопрос: нет токена бота или Chat ID. "
                              "Прогон не приостановлен. Настройте «Интеграции → Telegram».")}
        timeout_min = 0
        try:
            timeout_min = max(0, int(data.get("timeout_min") or 0))
        except (TypeError, ValueError):
            timeout_min = 0
        # Движок приостановит прогон и вернёт resume_state; run_process_board
        # сохранит его через board_wait_store и продолжит по ответу человека.
        return {"__wait__": {"chat_id": str(cid), "message": text,
                             "timeout_min": timeout_min}}

    # ── Данные встречи (§ «тянуть из встреч») ──
    # Узел вытягивает из встречи конкретный артефакт: саммари/задачи/решения/
    # участники/транскрипт/повестка/отчёт. Встреча — явный ID на узле ИЛИ та,
    # что запустила триггер (ctx["trigger_meeting_id"]), ИЛИ ID из входа.
    # Выход (text) течёт дальше — в отчёт/карту/Telegram.
    if node_type in ("meeting_data", "meeting_source"):
        kind = str(data.get("kind") or "report").strip().lower()
        _trigger_mid = str(ctx.get("trigger_meeting_id") or "").strip()
        _node_mid = str(data.get("meeting_id") or "").strip()
        # Событийный запуск: приоритет у встречи, ЗАПУСТИВШЕЙ триггер.
        # Раньше сохранённый на узле id побеждал — а он остаётся от ручной
        # настройки/теста шаблона и протухает (встреча удалена/чужая) →
        # живое событие «встреча завершена» падало «Встреча не найдена»,
        # хотя свежая встреча существовала и была прямо в событии.
        mid = _trigger_mid or _node_mid
        latest_note = None
        # Вход предыдущего шага дал короткий ID встречи?
        upstream_id = (upstream.strip()
                       if (not mid and upstream and len(upstream) < 80
                           and "\n" not in upstream) else "")
        # «Последняя встреча» берётся, если: явный sentinel __latest__ ИЛИ узел
        # пустой и это РУЧНОЙ запуск (нет триггера, нет входа-ID). Смысл
        # автоматизации — «дай отчёт по только что прошедшей встрече», поэтому
        # пустой узел на ручном Run = последняя встреча, а не ошибка «выбери».
        want_latest = mid in ("__latest__", "latest") or (not mid and not upstream_id)
        if want_latest:
            # Живой запрос в MeetFlow НА МОМЕНТ ЗАПУСКА (часовой синк ни при чём —
            # встреча, закончившаяся 10 минут назад, уже видна). Для отчёта нужна
            # встреча С транскриптом: берём свежайшую такую (иначе самая свежая
            # без контента — напр. только что созданная пустышка — дала бы
            # честную, но бесполезную ошибку «нет данных»). В note пишем какую.
            try:
                from backend.db.supabase_client import get_supabase_client
                rows = await get_supabase_client()._request(
                    "GET", "/rest/v1/meetings",
                    params={"user_id": f"eq.{user_id}",
                            "select": "id,title,created_at,transcription_text",
                            "order": "created_at.desc", "limit": "12"})
            except Exception as e:
                return {"error": f"последняя встреча: MeetFlow недоступен ({e})"}
            rows = rows or []
            if not rows:
                return {"error": "последняя встреча: встреч в MeetFlow не найдено"}
            pick = next((r for r in rows
                         if len(str(r.get("transcription_text") or "")) > 100), rows[0])
            mid = str(pick.get("id") or "")
            latest_note = (f"последняя встреча: «{pick.get('title') or mid}» "
                           f"({str(pick.get('created_at') or '')[:16]})")
        elif upstream_id:
            mid = upstream_id  # предыдущий шаг вернул ID встречи
        if not mid:
            return {"error": ("не задана встреча: выберите встречу на узле или "
                              "запустите доску по триггеру встречи")}
        try:
            from backend.core.board.meeting_artifacts import fetch_artifact
            art = await fetch_artifact(user_id or "", mid, kind)
        except Exception as e:
            return {"error": f"данные встречи: {e}"}
        if art.get("error"):
            return {"error": art["error"]}
        if art.get("empty"):
            # ЧЕСТНАЯ ошибка вместо тихой пустоты: раньше узел отдавал
            # text="" с note, report дальше собирал болванку «[уточнить]»
            # и notify ОТПРАВЛЯЛ её клиенту. Типовой случай — «последняя
            # встреча» указывает на свежую, ещё не обработанную мозгом.
            _t = str(art.get("title") or mid)
            _hint = (" Встреча свежая — мозг мог её ещё не обработать: "
                     "подождите завершения синка или возьмите «транскрипт»."
                     if kind != "transcript" else "")
            return {"error": (f"по встрече «{_t}» ещё нет данных ({kind})"
                              + (f" · {latest_note}" if latest_note else "")
                              + "." + _hint
                              + " Доска не отправит пустой отчёт.")}
        return {"text": art.get("text") or "", "kind": art.get("kind"),
                "meeting_title": art.get("title"),
                **({"note": latest_note} if latest_note else {})}

    # ── Документ / КП (РЕАЛЬНЫЙ модуль создания документов по встрече) ──
    # Не голый LLM: doc_kind-промпты (kp/contract/card/free), реквизиты компании,
    # образец стиля. Вход (саммари встречи + CRM/веб-контекст) → черновик КП.
    if node_type in ("document", "kp"):
        doc_kind = str(data.get("doc_kind") or "kp").strip().lower()
        if not upstream.strip():
            return {"error": "нет входных данных для документа — подайте саммари встречи/контекст"}
        try:
            from backend.core.documents.meeting_doc_service import compose_document
            from backend.core.llm.router import get_llm_router
            req = await _doc_requisites(user_id)
            md = await compose_document(
                get_llm_router(), doc_kind=doc_kind, meeting_text=upstream_llm,
                style_example=str(data.get("style_example") or ""),
                custom_prompt=str(data.get("custom_prompt") or ""),
                extra_context="", requisites=req)
        except Exception as e:
            return {"error": f"документ: {e}"}
        if not md:
            return {"error": "документ не собран (LLM недоступен)"}
        out: Dict[str, Any] = {"text": md, "doc_kind": doc_kind}
        # Рендер в файл (docx/pdf) с фирменным бланком → вложение для «Уведомления».
        render = str(data.get("render") or "").strip().lower()
        if render in ("docx", "pdf"):
            try:
                lh = None
                try:
                    from backend.core.board.brand_assets import get_letterhead_image_bytes
                    got = get_letterhead_image_bytes(user_id or "")
                    if got and got[0]:
                        lh = got[0]
                except Exception:
                    logger.debug("doc letterhead skipped", exc_info=True)
                title = {"kp": "Коммерческое предложение", "contract": "Договор",
                         "card": "Карточка встречи"}.get(doc_kind, "Документ")
                if render == "pdf":
                    from backend.core.analysis.export import markdown_to_pdf_bytes
                    blob = markdown_to_pdf_bytes(md, title=title, letterhead_png=lh)
                    if blob:
                        out["doc_file"] = _save_board_doc(blob, "pdf", user_id)
                        out["doc_name"] = f"{title}.pdf"
                    else:
                        out["note"] = "PDF недоступен на сервере — приложить не удалось"
                if render == "docx" or (render == "pdf" and not out.get("doc_file")):
                    from backend.core.analysis.export import markdown_to_docx_bytes
                    blob = markdown_to_docx_bytes(md, title=title, letterhead_png=lh)
                    out["doc_file"] = _save_board_doc(blob, "docx", user_id)
                    out["doc_name"] = f"{title}.docx"
            except Exception as e:
                logger.debug("document render failed: %s", e)
        return out

    # ── Отчёт → Excel (нативный .xlsx-файл из данных предыдущих шагов) ──
    if node_type in ("report_xlsx", "xlsx"):
        if not upstream.strip():
            return {"error": "нет данных для Excel-отчёта — подайте отчёт/таблицу/данные на вход"}
        instruction = str(data.get("prompt") or data.get("instruction") or "").strip()
        try:
            sheets = await _structure_xlsx_sheets(upstream_llm, instruction, user_id)
        except Exception as e:
            return {"error": f"excel-отчёт: {e}"}
        if not sheets:
            return {"error": "не удалось выделить таблицу из входных данных"}
        try:
            from backend.core.analysis.export import rows_to_xlsx_bytes
            blob = rows_to_xlsx_bytes(sheets)
        except Exception as e:
            return {"error": f"excel-отчёт: {e}"}
        if not blob:
            return {"error": "Excel недоступен на сервере (openpyxl) — файл не собран"}
        title = str(data.get("title") or "Отчёт").strip() or "Отчёт"
        n_rows = sum(len((s or {}).get("rows") or []) for s in sheets)
        out = {"text": f"Excel-отчёт готов: листов {len(sheets)}, строк {n_rows}.",
               "doc_file": _save_board_doc(blob, "xlsx", user_id),
               "doc_name": f"{title}.xlsx"}
        return out

    # ── Перевод (мультиязычность: текст/документ → целевой язык) ──
    if node_type == "translate":
        if not upstream.strip():
            return {"error": "нет текста для перевода — подайте документ/текст на вход"}
        lang = str(data.get("target_lang") or data.get("lang") or "en").strip()
        _LANGS = {"en": "английский", "ru": "русский", "de": "немецкий",
                  "fr": "французский", "es": "испанский", "zh": "китайский",
                  "it": "итальянский", "tr": "турецкий", "ar": "арабский",
                  "kk": "казахский", "pt": "португальский", "ja": "японский"}
        target = _LANGS.get(lang.lower(), lang)
        try:
            from backend.core.llm.router import get_llm_router, set_llm_context
            set_llm_context(user_id=user_id, session_id="board-translate", agent_mode="board")
            _sys = (f"Ты профессиональный переводчик. Переведи текст на {target} язык. "
                    "Сохрани форматирование (заголовки, списки, markdown-таблицы), а "
                    "числа, цены, единицы измерения, имена собственные и названия "
                    "компаний оставь без искажений. Верни ТОЛЬКО перевод, без пояснений.")
            out_text = await get_llm_router().generate(upstream_llm[:16000], system_prompt=_sys,
                                                       personalize=False)
        except Exception as e:
            return {"error": f"перевод: {e}"}
        res = str(out_text or "").strip()
        return {"text": res} if res else {"error": "перевод не выполнен (LLM недоступен)"}

    # ── Правка документа командой («убери раздел», «сделай короче») ──
    if node_type == "doc_edit":
        if not upstream.strip():
            return {"error": "нет документа для правки — подайте документ на вход"}
        instr = str(data.get("instruction") or data.get("prompt") or "").strip()
        if not instr:
            return {"error": "не задана инструкция правки (что именно изменить)"}
        try:
            from backend.core.llm.router import get_llm_router, set_llm_context
            set_llm_context(user_id=user_id, session_id="board-doc-edit", agent_mode="board")
            _sys = ("Ты редактируешь готовый документ по инструкции пользователя. Верни "
                    "ПОЛНЫЙ документ целиком с внесённой правкой, сохранив формат и стиль. "
                    "Не добавляй новых фактов, цифр или цен — работай только с содержимым "
                    "документа и инструкцией. Верни только документ, без комментариев.")
            prompt = f"Инструкция правки: {instr}\n\nДокумент:\n{upstream_llm[:16000]}"
            edited = await get_llm_router().generate(prompt, system_prompt=_sys,
                                                     personalize=False)
        except Exception as e:
            return {"error": f"правка документа: {e}"}
        res = str(edited or "").strip()
        return {"text": res} if res else {"error": "правка не выполнена (LLM недоступен)"}

    # ── Запись в CRM (создать сделку/лид, добавить комментарий) ──
    # Deny-by-default: весь путь под ENABLE_CRM_WRITEBACK. Выключено → ошибка,
    # ни одного внешнего вызова. Учётные данные — только из env.
    if node_type == "crm_write":
        from backend.core.ontology.crm_writeback import get_writer, writeback_enabled
        if not writeback_enabled():
            return {"error": "Запись в CRM выключена (включите ENABLE_CRM_WRITEBACK на сервере)"}
        provider = str(data.get("provider") or "").strip().lower()
        op = str(data.get("op") or "create").strip().lower()
        writer = get_writer(provider)
        if not writer:
            return {"error": f"CRM-провайдер не поддержан для записи: {provider or '—'}"}
        if (env_err := writer.env_check()):
            return {"error": f"CRM {provider}: {env_err}"}
        fields = {
            "name": str(data.get("name") or data.get("title") or "").strip(),
            "value": data.get("value"),
            "entity_id": str(data.get("entity_id") or "").strip(),
            # текст заметки: явное поле или текст с прошлого шага
            "text": str(data.get("note_text") or upstream or "").strip(),
        }
        try:
            res = await writer.write(op, fields)
        except Exception as e:
            return {"error": f"запись в CRM: {e}"}
        rid = (res or {}).get("id")
        url = (res or {}).get("url") or ""
        label = "Комментарий добавлен" if op == "note" else "Запись создана"
        msg = f"{label} в CRM ({provider}). ID: {rid}." + (f" {url}" if url else "")
        return {"text": msg, "crm_id": rid, "crm_url": url}

    # ── Данные CRM / датасет (контекст для КП: клиент, цены) ──
    if node_type in ("crm_data", "dataset_query"):
        q = str(data.get("query") or upstream or "").strip()
        if not q:
            return {"error": "не задан запрос к CRM/датасету"}
        ds = str(data.get("dataset_id") or "").strip()
        try:
            from backend.core.ontology.dataset_service import (
                ask_dataset,
                try_dataset_route,
            )
            res = (await ask_dataset(user_id or "", ds, q)) if ds \
                else (await try_dataset_route(user_id or "", q))
        except Exception as e:
            return {"error": f"CRM/датасет: {e}"}
        txt = _answer_text(res)
        if not txt:
            # ЧЕСТНАЯ пустота вместо тихой: раньше узел отдавал text="" с
            # note, report дальше собирал «отчёт из ничего» — три пустых
            # отчёта по выручке именно отсюда. Теперь это ОШИБКА с причиной
            # и списком доступных таблиц: ветка ниже пропустится с
            # «пропущен из-за ошибки входа», доска упадёт честно.
            if ds:
                why = str((res or {}).get("error") or "") if isinstance(res, dict) else ""
                return {"error": ("датасет не дал ответа на вопрос"
                                  + (f": {why}" if why else "")
                                  + " — уточните вопрос (названия колонок помогают)")}
            titles: List[str] = []
            try:
                from backend.core.ontology.dataset_service import registry_for_user
                titles = [str(d0.get("title") or d0.get("dataset_id") or "")
                          for d0 in (registry_for_user(user_id or "").list() or [])]
                titles = [t for t in titles if t][:8]
            except Exception:
                logger.debug("crm_data: datasets list failed", exc_info=True)
            if not titles:
                return {"error": ("нет подключённых таблиц/CRM-данных — загрузите "
                                  "таблицу или подключите CRM во вкладке "
                                  "«Онтология → Датасеты», либо укажите датасет "
                                  "прямо на узле")}
            return {"error": ("вопрос не совпал ни с одной таблицей (есть: "
                              + ", ".join(f"«{t}»" for t in titles)
                              + ") — выберите датасет на узле или "
                              "переформулируйте вопрос с названием таблицы/колонки")}
        return {"text": txt}

    # ── Веб-поиск (сайт клиента / рынок → контекст для КП) ──
    if node_type in ("web_search", "web"):
        q = str(data.get("query") or upstream or "").strip()
        if not q:
            return {"error": "не задан поисковый запрос"}
        # Публичные TG-каналы вместо запроса (@name / t.me/name, до 5 через
        # запятую) → свежие посты каналов: «дайджест каналов конкурентов»
        # без отдельного узла. Проверяем ДО ветки URL — t.me это тоже ссылка.
        _chans = _parse_tg_channels(q)
        if _chans:
            _tg = await _fetch_tg_channel_posts(_chans)
            if _tg.get("error"):
                return {"error": f"TG-каналы: {_tg['error']}"}
            return {"text": _tg.get("text") or "", "channels": _chans,
                    **({"untrusted_source": _tg["untrusted_source"]}
                       if _tg.get("untrusted_source") else {})}
        # Ссылка вместо запроса → читаем СТРАНИЦУ, а не ищем: Firecrawl по
        # ключу из «Интеграций» (рендерит JS, отдаёт чистый markdown), иначе —
        # встроенный лёгкий фетчер (без JS, с SSRF-защитой). Так узел закрывает
        # «прочитай сайт клиента / прайс конкурента» без отдельного узла.
        if q.lower().startswith(("http://", "https://")):
            page = await _fetch_page_text(user_id, q)
            if page.get("error"):
                return {"error": f"чтение страницы: {page['error']}"}
            return {"text": page.get("text") or "", "url": q,
                    **({"untrusted_source": page["untrusted_source"]}
                       if page.get("untrusted_source") else {}),
                    **({"note": page["note"]} if page.get("note") else {})}
        try:
            from backend.core.search.web_search import format_results_md, web_search
            mx = 5
            try:
                mx = max(1, min(10, int(data.get("max_results") or 5)))
            except (TypeError, ValueError):
                mx = 5
            results = await web_search(q, max_results=mx)
            md = format_results_md(q, results)
        except Exception as e:
            return {"error": f"веб-поиск: {e}"}
        # Сниппеты поиска — тоже внешний текст: та же рамка для LLM-узлов.
        return {"text": md or "",
                **({"untrusted_source": "результаты веб-поиска"} if md else {}),
                **({} if md else {"note": "ничего не найдено"})}

    # ── Поделиться встречей (MeetFlow): публичная ссылка «сторонний вход» ──
    # Создаёт share-ссылку через MeetFlow (его же права) и отдаёт её вниз —
    # в notify/Telegram/письмо. Встреча — явный ID или из триггера. Серверный
    # вызов идёт под PAT пользователя («Интеграции → MeetFlow»). За флагом.
    if node_type == "meeting_share":
        from backend.core.board.meeting_share import (
            create_public_share,
            grant_meeting_permission,
            meetflow_pat,
            share_enabled,
        )
        if not share_enabled():
            return {"error": "Расшаривание встреч выключено (ENABLE_MEETFLOW_SHARE)"}
        mid = (str(data.get("meeting_id") or "").strip()
               or str(ctx.get("trigger_meeting_id") or "").strip())
        if mid in ("__latest__", "latest"):
            # «Последняя встреча» — живой запрос в MeetFlow (как в meeting_data).
            try:
                from backend.db.supabase_client import get_supabase_client
                rows = await get_supabase_client()._request(
                    "GET", "/rest/v1/meetings",
                    params={"user_id": f"eq.{user_id}", "select": "id",
                            "order": "created_at.desc", "limit": "1"})
                mid = str((rows or [{}])[0].get("id") or "")
            except Exception as e:
                return {"error": f"последняя встреча: MeetFlow недоступен ({e})"}
        if not mid and upstream and len(upstream) < 80 and "\n" not in upstream:
            mid = upstream.strip()
        if not mid:
            return {"error": "не задана встреча для расшаривания (выберите на узле или триггер встречи)"}
        token = await meetflow_pat(user_id)
        if not token:
            return {"error": ("нет доступа к MeetFlow: добавьте Personal Access Token "
                              "(mf_pat_…) в «Интеграции → MeetFlow»")}
        kind = str(data.get("share_kind") or "link").strip().lower()
        if kind == "grant":
            # Доступ по email(ам): внутренние права (read/write/admin).
            emails = [e.strip() for e in str(data.get("emails") or "").replace(";", ",").split(",") if e.strip()]
            if not emails:
                return {"error": "укажите email(ы) получателей для выдачи доступа"}
            pt = str(data.get("permission_type") or "read")
            granted, errors, link = [], [], None
            for em in emails:
                res = await grant_meeting_permission(mid, auth_token=token,
                                                     grantee_id=em, permission_type=pt)
                if res.get("ok"):
                    granted.append(em)
                    link = res.get("meeting_link") or link
                else:
                    errors.append(f"{em}: {res.get('error')}")
            if not granted:
                return {"error": "MeetFlow grant: " + "; ".join(errors)}
            txt = (link or "") + ("\nДоступ выдан: " + ", ".join(granted) if granted else "")
            out = {"text": txt.strip(), "meeting_link": link,
                   "granted_to": granted, "permission_type": pt}
            if errors:
                out["note"] = "часть не выдана: " + "; ".join(errors)
            return out
        # Публичная ссылка (view/comment + срок).
        domains = [d.strip() for d in str(data.get("allowed_domains") or "").split(",") if d.strip()]
        exp = data.get("expires_in_days", 7)
        try:
            exp = int(exp) if exp not in (None, "") else None
        except (TypeError, ValueError):
            exp = 7
        res = await create_public_share(
            mid, auth_token=token,
            access_level=str(data.get("access_level") or "view"),
            expires_in_days=exp,
            max_views=data.get("max_views"),
            allowed_domains=domains,
            password=str(data.get("password") or ""))
        if not res.get("ok"):
            return {"error": f"MeetFlow share: {res.get('error')}"}
        url = res.get("share_url") or ""
        return {"text": url, "share_url": url, "access_level": res.get("access_level")}

    # ── Креативная цепочка (те же карточки, что в креатив-режиме) ──
    # Процесс запускает цепочку креативных узлов: передаёт данные в prompt,
    # цепочка исполняется сервером (prompt → combiner/LLM → картинка), после
    # чего снова подключаются процессные узлы (notify/task/...).

    if node_type == "prompt":
        # Креатив-карточка «Промпт»: её текст + данные процесса. {{input}}
        # подставляется; без плейсхолдера upstream дописывается блоком данных.
        text = str(data.get("prompt") or "").strip()
        if "{{input}}" in text:
            text = text.replace("{{input}}", upstream or "")
        elif upstream:
            text = (text + "\n\nДанные:\n" + upstream) if text else upstream
        if not text.strip():
            return {"error": "prompt: пустой промпт и нет входных данных"}
        return {"text": text}

    if node_type == "textCombiner":
        # Комбинатор: шаблон с {{input1}}..{{inputN}} и {{input}} (все вместе)
        template = str(data.get("template") or "").strip()
        texts = [str(v.get("text") or v) if isinstance(v, dict) else str(v)
                 for v in inputs if v]
        if not template:
            return {"text": "\n".join(t for t in texts if t).strip()}
        out_text = template
        for i, t in enumerate(texts, start=1):
            out_text = out_text.replace("{{input" + str(i) + "}}", t)
        out_text = out_text.replace("{{input}}", "\n".join(texts))
        return {"text": out_text}

    if node_type == "llmGenerate":
        # Креатив-карточка «LLM»: генерация текста из входа цепочки
        prompt = upstream_llm or str(data.get("inputPrompt") or "").strip()
        if not prompt:
            return {"error": "llmGenerate: нет входного промпта"}
        # Явно выбранные на узле провайдер+модель → генерируем ИМЕННО ими
        # (реальный вызов провайдера). Нет ключа/провайдера → падать нельзя:
        # откатываемся на модель тенанта по умолчанию (как было раньше).
        try:
            _temp = float(data.get("temperature", 0.7) or 0.7)
        except Exception:
            _temp = 0.7
        try:
            _mt = int(data.get("maxTokens", 2048) or 2048)
        except Exception:
            _mt = 2048
        _prov = str(data.get("provider") or "").strip().lower()
        picked = await _generate_with_board_model(
            user_id, _prov, str(data.get("model") or ""),
            prompt, temperature=_temp, max_tokens=_mt)
        # Фолбэк по просьбе: выбранная модель не сработала (напр. DeepSeek) →
        # пробуем Gemini, прежде чем уходить на общий дефолтный роутер тенанта.
        if not picked and _prov and _prov != "google":
            picked = await _generate_with_board_model(
                user_id, "google", "gemini-2.5-flash",
                prompt, temperature=_temp, max_tokens=_mt)
        if picked:
            return {"text": picked}
        try:
            text = None
            try:
                from backend.core.llm.workload_policy import generate_for_workload
                text = await generate_for_workload(user_id or "",
                                                   "tz_generation", prompt)
            except Exception:
                logger.debug("llmGenerate: workload route skipped", exc_info=True)
            if not text:
                text = await get_llm_router().generate(prompt, personalize=False)
            return {"text": (text or "").strip()}
        except Exception as e:
            return {"error": f"llmGenerate: {e}"}

    if node_type in ("nanoBanana", "image"):
        # Креатив-карточка «Картинка»: серверная генерация. Промпт приходит
        # ИЗ ЦЕПОЧКИ (prompt-узел/LLM), либо из data.prompt с {{input}}.
        # Модель карточки уважается: nano-banana* → Gemini, gpt-image*/dall-e →
        # OpenAI с этой моделью.
        model = str(data.get("model") or "").strip()
        if model.startswith("veo"):
            return {"error": "image: видео (Veo) в серверном прогоне процессов "
                             "пока не поддерживается — уберите узел или "
                             "замените модель на картиночную"}
        if data.get("inputImages"):
            return {"error": "image: входные изображения в серверном прогоне "
                             "пока не поддерживаются — используйте "
                             "text-to-image (без входной картинки)"}
        prompt = str(data.get("prompt") or "").strip()
        if "{{input}}" in prompt:
            prompt = prompt.replace("{{input}}", upstream or "")
        elif upstream:
            prompt = (prompt + "\n\nСодержание:\n" + upstream) if prompt else upstream
        if not prompt.strip():
            return {"error": "image: нет промпта и входных данных"}
        preferred = None
        if model.startswith("nano-banana"):
            preferred = "gemini"
        elif model.startswith("gpt-image") or model.startswith("dall-e"):
            # Сохраняем ВЫБРАННУЮ модель (gpt-image-2 раньше молча съезжал в -1).
            _om = "dall-e-3" if model.startswith("dall-e") else (
                "gpt-image-2" if model.startswith("gpt-image-2") else "gpt-image-1")
            preferred = f"openai:{_om}"
        try:
            from backend.core.board.image_gen import (
                generate_image_png,
                save_board_image,
            )
            res = await generate_image_png(prompt, user_id, preferred=preferred)
            if not res.get("png"):
                return {"error": f"image: {res.get('error') or 'генерация не удалась'}"}
            path = save_board_image(res["png"], user_id)
            return {"text": f"🖼 Изображение готово ({res.get('provider')})",
                    "image_file": path,
                    "image_prompt": prompt[:300]}
        except Exception as e:
            return {"error": f"image: {e}"}

    if node_type == "infographic":
        # Визуальный отчёт: image-модель рисует всё по плотному промпту (с точным
        # русским текстом), а ключевые факты ДУБЛИРУЮТСЯ текстом в выход/подпись.
        # Нет ключа/упала — запасной Pillow-рендер (без моков). Формат выбирается
        # на узле (data.format): инфографика встречи / карта-организм компании / …
        source = upstream or str(data.get("prompt") or data.get("text") or "").strip()
        # Событийный запуск (meeting_ended): если вход пуст ИЛИ это сырой
        # payload события (короткий JSON c id/title, без содержимого) — карта
        # выходила без саммари, из мусора. Подтягиваем отчёт встречи сами.
        _mid = str(ctx.get("trigger_meeting_id") or "").strip()
        if _mid and (not source or (source.lstrip().startswith("{")
                                    and len(source) < 800)):
            try:
                from backend.core.board.meeting_artifacts import fetch_artifact
                _art = await fetch_artifact(user_id or "", _mid, "report")
                if _art.get("text"):
                    source = _art["text"]
            except Exception:
                logger.debug("infographic: meeting report fetch skipped",
                             exc_info=True)
        if not source:
            return {"error": "визуальный отчёт: нет входных данных (подключите «Вопрос мозгу»/текст)"}
        # Персонализация ЛИЧНОЙ версии: если аудитория private и задан получатель —
        # префиксуем директиву в источник (универсально для всех форматов, без
        # правки сигнатур). LLM обратится к человеку и выделит, что касается его.
        _recipient = str(data.get("recipient") or "").strip()
        if _recipient and str(data.get("audience") or "").strip().lower() == "private":
            source = (f"[Этот отчёт — ЛИЧНО для: {_recipient}. Обращайся к нему на «ты», "
                      f"выдели в фокусе то, что касается именно его роли/задач.]\n\n{source}")
        from backend.core.board.image_gen import generate_image_png, save_board_image
        from backend.core.board.meeting_infographic import _seed_from

        # Формат визуального отчёта → набор функций (extract/prompt/render/facts).
        # Единый контракт у всех форматов — добавить новый = один elif + модуль.
        fmt = str(data.get("format") or "infographic").strip().lower()
        if fmt == "organism":
            from backend.core.board import company_organism as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_organism_data, _fm.build_organism_prompt,
                _fm.render_organism_png, _fm.build_organism_facts)
        elif fmt == "character":
            from backend.core.board import character_portrait as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_character_data, _fm.build_character_prompt,
                _fm.render_character_png, _fm.build_character_facts)
        elif fmt == "comic":
            from backend.core.board import comic_strip as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_comic_data, _fm.build_comic_prompt,
                _fm.render_comic_png, _fm.build_comic_facts)
        elif fmt == "meme":
            from backend.core.board import meme_card as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_meme_data, _fm.build_meme_prompt,
                _fm.render_meme_png, _fm.build_meme_facts)
        elif fmt == "metaphor":
            from backend.core.board import metaphor_scene as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_metaphor_data, _fm.build_metaphor_prompt,
                _fm.render_metaphor_png, _fm.build_metaphor_facts)
        else:
            fmt = "infographic"
            from backend.core.board import meeting_infographic as _fm
            _extract, _prompt, _render, _facts = (
                _fm.extract_infographic_data, _fm.build_infographic_prompt,
                _fm.render_infographic_png, _fm.build_key_facts_text)

        # Язык отчёта: селектор узла (auto/ru/en). "auto" → язык интерфейса из
        # контекста запуска (run_board?lang=..) → персона → ru. Весь текст —
        # картинка, факты, дубль — на нём.
        lang = str(data.get("lang") or "").strip().lower()
        if lang not in ("ru", "en"):
            _ui = str(ctx.get("lang") or "").strip().lower()
            if _ui in ("ru", "en"):
                lang = _ui
        if lang not in ("ru", "en"):
            lang = "ru"
            try:
                from backend.core.persona.store import get_persona_store
                _p = await get_persona_store().get(user_id)
                _cc = getattr(_p, "communication_cognitive", None)
                _pl = getattr(_cc, "preferred_language", None) or getattr(_p, "preferred_language", None)
                if _pl:
                    lang = "en" if str(_pl).strip().lower().startswith("en") else "ru"
            except Exception:
                lang = "ru"

        # Серия отчётов (память образов/хвостов): scope склеивает серию —
        # сюжеты-нити, накопление ×N и анти-повтор сцены живут на нём.
        _scope = str(data.get("thread_scope") or "default").strip() or "default"
        graph = await _extract(user_id, source, lang=lang)
        if not graph:
            return {"error": f"визуальный отчёт ({fmt}): не удалось разложить материал в структуру"}
        # Стиль-пресет с карточки (buzan/poster/neon/editorial/auto) — в граф, чтобы
        # промпт-билдер инфографики задал сильный цельный вид. Прочие форматы игнорят.
        logo_ref: Optional[List[bytes]] = None
        if isinstance(graph, dict):
            graph["_style"] = str(data.get("style") or "auto").strip().lower()
            # Фирменный стиль тенанта (если включён на узле и задан) → в промпт.
            if data.get("use_brand", True):
                try:
                    from backend.core.board.brand_profile import get_brand
                    _brand = get_brand(user_id)
                    if _brand:
                        graph["_brand"] = _brand
                except Exception:
                    logger.debug("brand load skipped", exc_info=True)
                # Логотип-файл (если загружен) → референс-картинка для image-модели.
                try:
                    from backend.core.board.brand_assets import get_logo_bytes
                    _logo = get_logo_bytes(user_id or "")
                    if _logo and _logo[0]:
                        logo_ref = [_logo[0]]
                except Exception:
                    logger.debug("brand logo load skipped", exc_info=True)
                # Style-reference (§9 фаза 3): загруженные ОБРАЗЦЫ фирменного
                # стиля → референсы image-модели; отчёт выходит «в нашем стиле».
                # Директива в промпт: подражать СТИЛЮ (палитра/линии/типографика/
                # настроение), содержание — ТОЛЬКО из данных, не копировать.
                try:
                    from backend.core.board.brand_assets import get_style_ref_bytes
                    _refs = get_style_ref_bytes(user_id or "", limit=2)
                    if _refs:
                        logo_ref = (logo_ref or []) + [b for b, _ct in _refs]
                        graph["_style_ref"] = True
                except Exception:
                    logger.debug("brand style refs load skipped", exc_info=True)
            # Сериальность (§17): сверить сквозные сюжеты с прошлыми отчётами серии
            # (scope) — стабильный символ и статус переходят из отчёта в отчёт,
            # тянущийся хвост НАКАПЛИВАЕТСЯ (×N в промпте), решённый завершается.
            if graph.get("threads"):
                try:
                    from backend.core.board.report_threads import reconcile
                    _th = reconcile(user_id, _scope, graph.get("threads") or [])
                    if _th:
                        graph["_threads"] = _th
                except Exception:
                    logger.debug("threads reconcile skipped", exc_info=True)
        # Политика эмоций: аудитория узла. "public" (команде) — чистим личное/
        # чувствительное; "private" (лично руководителю) — сохраняем всё.
        from backend.core.board.emotion_policy import apply_emotion_policy, audience_of
        audience = audience_of(data.get("audience"))
        graph = apply_emotion_policy(graph, audience)
        key_facts = _facts(graph, lang=lang)
        # Читаемая подпись (по выбору пользователя): картинка остаётся на
        # ключевых словах-карте, а ТЕКСТ под ней — полными предложениями,
        # понятными любому. Отдельный LLM-вызов; при сбое/пустом — keyword-текст
        # выше (никогда не роняем доставку). Только для инфографики; отключаемо
        # флагом на узле readable_caption:false.
        if fmt == "infographic" and str(
                data.get("readable_caption", "on")).strip().lower() not in ("off", "0", "false", "no"):
            try:
                _readable = await _fm.build_readable_caption(user_id, source, graph, lang=lang)
                if _readable and len(_readable) > 40:
                    key_facts = _readable
            except Exception:
                logger.debug("readable caption skipped", exc_info=True)
        # Privacy-гейт (§8.4): для ПУБЛИЧНОЙ версии — второй рубеж поверх политики
        # эмоций. Просканировать итоговый текст/граф на остаточное чувствительное.
        # По умолчанию — предупреждение в выходе; жёсткая блокировка за флагом.
        privacy_warnings = []
        if audience == "public":
            try:
                from backend.core.board.report_privacy import (
                    privacy_block_enabled,
                    scan_public_leak,
                )
                _scan = scan_public_leak(key_facts, graph=graph)
                privacy_warnings = _scan.get("warnings") or []
                if _scan.get("sensitive") and privacy_block_enabled():
                    return {"error": "визуальный отчёт: доставка публичной версии "
                                     "заблокирована — обнаружено чувствительное содержимое "
                                     "(" + ", ".join(_scan.get("topics") or []) + "). "
                                     "Смените аудиторию на «лично» или уберите чувствительное.",
                            "text": key_facts, "privacy_warnings": privacy_warnings}
            except Exception:
                logger.debug("privacy scan skipped", exc_info=True)
        # Движок разнообразия: сид из содержимого — один и тот же материал
        # узнаваем, разный — выглядит по-разному. См. VISUAL_REPORTS §3.
        seed = _seed_from(source, graph.get("title") or "", fmt)
        # "code" — детерминированно кодом БЕЗ image-модели; "model" — рисует
        # image-модель, с фолбэком на код, если ключа нет/упала.
        render_mode = str(data.get("render") or "model").strip().lower()
        if render_mode == "code":
            try:
                png = _render(graph, seed=seed, lang=lang)
                path = save_board_image(png, user_id)
                return {"text": key_facts, "image_file": path,
                        "note": "отрисовано кодом (без LLM-картинки)",
                        **({"privacy_warnings": privacy_warnings} if privacy_warnings else {})}
            except Exception as e:
                return {"error": f"визуальный отчёт: {e}", "text": key_facts}
        # id выбранной модели прокидываем как есть — image_gen сам резолвит
        # провайдера (gemini/openai/ideogram/recraft/replicate) и делает фолбэк.
        preferred = str(data.get("model") or "nano-banana").strip()
        img_prompt = _prompt(graph, seed=seed, lang=lang)
        # ТИШИНА — ТОЖЕ ИНФОРМАЦИЯ (§3): аудит показал, что ни один формат не
        # имел предохранителя от выдуманной драмы (а мем безусловно требовал
        # юмора). Универсальная директива всем форматам: громкость образа
        # заслуживается фактами.
        img_prompt += (
            " ГРОМКОСТЬ ОБРАЗА = СЕРЬЁЗНОСТЬ ФАКТОВ: если материал спокойный/"
            "рутинный — сцена спокойная и тёплая, БЕЗ выдуманной драмы, тревоги "
            "и форсированного юмора; драматичные/тревожные образы — только для "
            "реально серьёзных событий из данных. Ровный период — это хорошая "
            "новость, покажи её одним спокойным образом."
            if lang == "ru" else
            " IMAGE LOUDNESS = SEVERITY OF FACTS: if the material is calm/"
            "routine, the scene is calm and warm — NO invented drama, alarm or "
            "forced humor; dramatic/alarming imagery only for genuinely serious "
            "events from the data. A quiet period is good news — show it as one "
            "calm image.")
        # Сюжеты-нити для НЕ-дефолтных форматов: билдер инфографики вплетает
        # threads_prompt_block сам, остальные 5 форматов его не звали — хвосты
        # ×N не доезжали до комикса/организма/метафоры. Добавляем в движке.
        try:
            if fmt != "infographic" and isinstance(graph, dict) \
                    and graph.get("_threads"):
                from backend.core.board.report_threads import threads_prompt_block
                _tb = threads_prompt_block(graph.get("_threads"),
                                           lang_ru=(lang == "ru"))
                if _tb:
                    img_prompt += _tb
        except Exception:
            logger.debug("threads block append skipped", exc_info=True)
        # Память серии (анти-повтор): следующий отчёт того же scope знает, как
        # выглядел прошлый, и обязан сменить композицию/палитру, сохранив язык
        # метафор и символы сюжетов. Универсально для всех форматов.
        try:
            from backend.core.board.report_threads import (
                anti_repeat_block,
                previous_scene,
            )
            _prev = previous_scene(user_id, _scope)
            if _prev:
                img_prompt += anti_repeat_block(_prev, lang_ru=(lang == "ru"))
        except Exception:
            logger.debug("series memory read skipped", exc_info=True)
        try:
            res = await generate_image_png(img_prompt, user_id, preferred=preferred,
                                           ref_images=logo_ref)
        except Exception as e:
            logger.debug("visual report image gen error: %s", e)
            res = {"png": None}
        if res.get("png"):
            # Запомнить сцену серии: хвост промпта несёт вариацию (стиль/
            # текстура/акцент) — этого достаточно для «сделай иначе» в следующем
            # отчёте. Пишем только при УСПЕШНОЙ генерации.
            try:
                from backend.core.board.report_threads import remember_scene
                remember_scene(user_id, _scope,
                               f"{fmt}/{graph.get('_style') or 'auto'}: "
                               + img_prompt[-220:])
            except Exception:
                logger.debug("series memory write skipped", exc_info=True)
            path = save_board_image(res["png"], user_id)
            actual = str(res.get("provider") or "")
            # Честно показываем, КАКАЯ модель реально нарисовала: выбранная могла
            # быть недоступна (неверный id/нет ключа) → сработал фолбэк по цепочке.
            note = f"нарисовано моделью: {actual}" if actual else None
            fell_back = bool(actual and preferred and preferred not in actual
                             and preferred not in ("nano-banana", "gemini"))
            if fell_back:
                _why = str(res.get("preferred_error") or "").strip()
                note = (f"выбранная модель «{preferred}» не сработала"
                        + (f" ({_why})" if _why else "")
                        + f" — нарисовано фолбэком: {actual}")
            key_facts = _with_ask_brain(user_id, data, key_facts, lang)
            return {"text": key_facts, "image_file": path,
                    "image_prompt": img_prompt[:300], "provider": actual,
                    **({"note": note} if note else {}),
                    **({"privacy_warnings": privacy_warnings} if privacy_warnings else {})}
        # Фолбэк: рисуем сами (Pillow), текст-дубль тот же. Причину честно
        # показываем — «система проигнорировала мой выбор» на деле значит
        # «модель упала вот почему» (таймаут/ключ/квота).
        try:
            png = _render(graph, seed=seed, lang=lang)
            path = save_board_image(png, user_id)
            key_facts = _with_ask_brain(user_id, data, key_facts, lang)
            _why = str((res or {}).get("error") or "").strip()
            return {"text": key_facts, "image_file": path,
                    "note": ("image-модель недоступна — отрисовано кодом (запасной вид)"
                             + (f". {_why}" if _why else "")),
                    **({"privacy_warnings": privacy_warnings} if privacy_warnings else {})}
        except Exception as e:
            return {"error": f"визуальный отчёт: {e}", "text": key_facts}

    if node_type == "audio":
        # Озвучка: текст (сводка от мозга/отчёт) → аудио для доставки. Люди не
        # любят читать длинные отчёты — слушают как подкаст. Провайдер/голос на
        # узле. Текст пробрасываем дальше — чтобы «Уведомление» дало и подпись.
        src = upstream or str(data.get("text") or "").strip()
        if not src:
            return {"error": "озвучка: нет входного текста (подключите «Вопрос мозгу»/текст)"}
        from backend.core.board.tts import save_board_audio, synthesize_speech
        provider = str(data.get("provider") or "openai").strip().lower()
        voice = str(data.get("voice") or "").strip()
        res = await synthesize_speech(user_id, src, provider=provider, voice=voice)
        if not res.get("audio"):
            return {"error": f"озвучка: {res.get('error') or 'не удалась'}", "text": src[:1000]}
        path = save_board_audio(res["audio"], user_id, ext=res.get("ext") or "mp3")
        return {"audio_file": path, "text": src[:1000], "provider": res.get("provider")}

    if node_type in ("imageInput", "annotation"):
        # Креатив-узлы «Изображение (вход)» / «Аннотация» в серверном прогоне
        # процесса: прозрачный проброс (раньше падали в «неизвестный тип узла»,
        # и картиночная половина смешанной схемы молча ничего не отдавала).
        # Картинку передаём дальше ТОЛЬКО если это серверный путь — клиентский
        # data:-URL как файл не откроется (_send_telegram_photo его не примет).
        res: Dict[str, Any] = {"text": upstream}
        ref = str(data.get("image_file") or data.get("imageFile") or "").strip()
        if not ref:
            ref = next((str(v.get("image_file")) for v in inputs
                        if isinstance(v, dict) and v.get("image_file")), "")
        if ref and not ref.startswith("data:"):
            res["image_file"] = ref
        return res

    if node_type == "task":
        title = str(data.get("title") or upstream[:80] or "Задача из процесса").strip()
        system = str(data.get("system") or "").strip().lower()
        column = str(data.get("column_id") or "").strip()
        if not (system and column):
            # Трекер не указан — не выдумываем создание, честно возвращаем намерение.
            return {"text": f"[task] {title}",
                    "note": "трекер не задан (system/column_id) — задача не создана"}
        try:
            from backend.core.tasks.task_actions import create_and_prepare_task
            res = await create_and_prepare_task(
                user_id or "", system, title=title,
                tz_text=(upstream or title), column_id=column,
                description=upstream[:1000])
            return {"task": res, "title": title}
        except Exception as e:
            return {"error": f"task: {e}"}

    if node_type in ("coding_agent", "handoff"):
        # Исполнитель задачи: тот же vibe-tasking конвейер, что и в очереди/
        # Minitest, но как узел процесса. Вход (upstream) = ТЗ/бриф; режим:
        #   document — LLM собирает ГОТОВЫЙ документ (КП/статью), текст идёт
        #              дальше по схеме (можно в notify/report/pdf);
        #   code     — CLI-агент по подписке (claude/codex/grok/qwen/…,
        #              включая добавленных через TESSENT_AGENT_COMMANDS_JSON);
        #              без repo_path — artifact-режим. Запуск в фоне, статус —
        #              в очереди Vibe Tasking + webhook (гейт человека — сам
        #              запуск доски/подтверждённый триггер).
        mode = str(data.get("mode") or "document").strip().lower()
        agent = str(data.get("agent") or "claude").strip().lower()
        title = str(data.get("title") or (upstream or "")[:80]
                    or "Задача с доски").strip()
        if not (upstream or data.get("description")):
            return {"error": "coding_agent: нет входного ТЗ (соедините с "
                             "узлом, который его формирует)"}
        try:
            from backend.core.tasks.task_analysis import (
                coding_handoff,
                confirm_handoff,
                execute_content_handoff,
            )
            rec = await coding_handoff(
                user_id or "", {"title": title,
                                "description": str(data.get("description") or "")},
                agent=agent,
                repo_path=str(data.get("repo_path") or "") or None,
                spec_text=(upstream or None),
                artifact_mode=not data.get("repo_path"),
                source={"kind": "board", "board_run": ctx.get("run_id") or ""})
            hid = rec.get("id") or ""
            if mode == "document":
                res = await execute_content_handoff(user_id or "", hid)
                doc = str(res.get("result_document") or "").strip()
                if doc:
                    return {"text": doc, "handoff_id": hid}
                return {"error": f"coding_agent(document): "
                                 f"{res.get('message') or res.get('status')}",
                        "handoff_id": hid}
            conf = await confirm_handoff(
                user_id or "", hid,
                repo_path=str(data.get("repo_path") or "") or None,
                background=True)
            return {"text": (f"🖥 Отдано агенту {agent}: "
                             f"{conf.get('status')} · handoff {hid}. "
                             f"Итог — в очереди Vibe Tasking."),
                    "handoff_id": hid, "status": conf.get("status")}
        except Exception as e:
            return {"error": f"coding_agent: {e}"}

    if node_type == "generate":
        # Креатив внутри автоматизации: серверная генерация ТЕКСТА (пост,
        # подпись, сценарий, ответ) из промпта + данных предыдущего шага.
        # Картинки/видео остаются за креативной доской (server-side image-gen —
        # отдельная возможность). {{input}} в промпте → текст с прошлого шага.
        prompt = str(data.get("prompt") or "").strip()
        if "{{input}}" in prompt:
            prompt = prompt.replace("{{input}}", upstream_llm or "")
        elif upstream_llm:
            prompt = (prompt + "\n\nВходные данные:\n" + upstream_llm) if prompt else upstream_llm
        if not prompt.strip():
            return {"error": "нечего генерировать (пустой промпт)"}
        try:
            from backend.core.llm.router import ModelTier, get_llm_router, set_llm_context
            set_llm_context(user_id=user_id, session_id="board-generate", agent_mode="board")
            _sys = ("Ты пишешь готовый контент по заданию. Стиль и форма — свободно, "
                    "но проверяемые факты (цифры, цены, названия, результаты, "
                    "фичи) бери ТОЛЬКО из задания/входных данных, не выдумывай. "
                    "Не хватает факта — оставь плейсхолдер [уточнить], а не "
                    "правдоподобное значение." + _lang_instruction(ctx))
            # Контент (пост/ответ/отчёт) — работа для сильной модели: premium-tier
            # даёт заметно лучший текст, чем flash-lite (standard). Безопасно
            # деградирует к standard, если premium-модель провайдера недоступна.
            try:
                _tier_kw = {"model_tier": ModelTier.PREMIUM}
            except Exception:
                _tier_kw = {}
            text = await get_llm_router().generate(
                prompt, system_prompt=_sys, personalize=False, **_tier_kw)
            return {"text": str(text or "").strip()}
        except Exception as e:
            return {"error": f"generate: {e}"}

    if node_type == "action":
        # Универсальный мост к любой подключённой интеграции (Slack/Gmail/
        # Notion/Jira/Trello/GitHub/Linear/…): «n8n уровня компании». Инструмент
        # исполняется тем же путём, что и автоматизации (registry.execute_tool),
        # СТРОГО на ключах текущего юзера (load_for_user) — тенант-безопасно.
        tool_name = str(data.get("tool_name") or "").strip()
        if not tool_name:
            return {"error": "action: не выбран инструмент (tool_name)"}
        raw_params = data.get("params") or {}
        if not isinstance(raw_params, dict):
            return {"error": "action: params должен быть объектом"}
        # {{input}} в любом строковом поле → текст с предыдущего шага.
        params: Dict[str, Any] = {}
        for k, v in raw_params.items():
            params[str(k)] = (v.replace("{{input}}", upstream)
                              if isinstance(v, str) else v)
        try:
            from backend.integrations.registry import IntegrationRegistry
            registry = IntegrationRegistry()
            await registry.load_for_user(user_id or "")
            result_json = await registry.execute_tool(tool_name, **params)
            try:
                result = json.loads(result_json) if result_json else {}
            except Exception:
                result = {"raw": str(result_json)[:400]}
            ok = bool(result.get("success", not result.get("error")))
            summary = str(result.get("message") or result.get("error")
                          or (result_json or "")[:300])
            return {"text": summary, "ok": ok, "tool": tool_name, "result": result}
        except Exception as e:
            return {"error": f"action ({tool_name}): {e}"}

    if node_type == _CONDITION:
        # Предикат по upstream. Оператор op расширяет прежнее «содержит»
        # (по умолчанию — contains, обратная совместимость): equals,
        # not_contains, regex, gt/lt (числовое сравнение), is_empty.
        op = str(data.get("op") or "contains").strip().lower()
        needle = str(data.get("contains") or "").strip()
        hay = upstream or ""
        val = False
        try:
            if op == "is_empty":
                val = not hay.strip()
            elif op == "equals":
                val = hay.strip().lower() == needle.lower()
            elif op == "not_contains":
                val = needle.lower() not in hay.lower() if needle else not hay
            elif op == "regex":
                import re as _re
                val = bool(_re.search(needle, hay)) if needle else False
            elif op in ("gt", "lt"):
                # Первое число из upstream vs число-порог в needle.
                import re as _re
                m = _re.search(r"-?\d+(?:[.,]\d+)?", hay)
                left = float(m.group(0).replace(",", ".")) if m else None
                right = float(needle.replace(",", ".")) if needle else None
                if left is not None and right is not None:
                    val = (left > right) if op == "gt" else (left < right)
            else:  # contains (дефолт)
                val = (needle.lower() in hay.lower()) if needle else bool(hay)
        except Exception as e:
            logger.debug("condition eval failed: %s", e)
            val = False
        return {"branch": "true" if val else "false"}

    if node_type == "output":
        # терминальный узел-приёмник: прозрачно отдаёт то, что пришло
        # (включая маркер изображения — чтобы фото было видно дальше)
        image_file = next((str(v.get("image_file")) for v in inputs
                           if isinstance(v, dict) and v.get("image_file")), None)
        out: Dict[str, Any] = {"text": upstream}
        if image_file:
            out["image_file"] = image_file
        return out

    return {"text": upstream, "note": f"неизвестный тип узла: {node_type}"}


# Узлы «доставки/результата»: должны получать вход. Если у такого узла нет
# входящей стрелки — он отработает, но данные не получит (ложный «успех»).
_DELIVERY_TYPES = {"notify", "output", "action", "report", "task"}
_TYPE_RU = {
    "trigger": "Триггер", "ask_brain": "Вопрос мозгу", "report": "Отчёт",
    "notify": "Уведомление", "task": "Задача", "action": "Действие",
    "generate": "Генерация", "condition": "Условие", "output": "Результат",
    "nanoBanana": "Картинка", "image": "Картинка", "llmGenerate": "LLM",
    "prompt": "Промпт", "textCombiner": "Комбинатор", "infographic": "Инфографика",
    "audio": "Озвучка", "document": "Документ", "crm_data": "Данные CRM",
    "web_search": "Веб-поиск", "report_xlsx": "Отчёт Excel", "translate": "Перевод",
    "doc_edit": "Правка документа", "crm_write": "Запись в CRM",
    "coding_agent": "Исполнитель (агент)", "handoff": "Исполнитель (агент)",
}


def _node_name(n: Dict[str, Any]) -> str:
    d = n.get("data") or {}
    return str(d.get("label") or _TYPE_RU.get(str(n.get("type")),
                                              n.get("type") or "узел"))


def _connectivity_warnings(nodes: List[Dict[str, Any]],
                           edges: List[Dict[str, Any]]) -> List[str]:
    """Предупреждения о НЕсоединённых блоках: без них прогон рапортует ✓ по
    каждому узлу, хотя данные между блоками не текут (узлы исполняются по
    отдельности). Возвращаем человекочитаемые подсказки для пользователя."""
    execs = [n for n in nodes if str(n.get("type")) != "note"]
    if len(execs) <= 1:
        return []
    ids = {str(n.get("id")) for n in execs}
    indeg = {i: 0 for i in ids}
    outdeg = {i: 0 for i in ids}
    for e in edges:
        s, t = str(e.get("source")), str(e.get("target"))
        if s in ids:
            outdeg[s] += 1
        if t in ids:
            indeg[t] += 1
    warnings: List[str] = []
    isolated = [n for n in execs
                if indeg[str(n.get("id"))] == 0 and outdeg[str(n.get("id"))] == 0]
    if isolated:
        names = ", ".join(_node_name(n) for n in isolated[:6])
        warnings.append(
            f"Блоки висят отдельно (ни одной стрелки): {names}. "
            "Соедините их — иначе данные между блоками не передаются, и «успех» "
            "по каждому блоку ничего не значит.")
    iso_ids = {str(n.get("id")) for n in isolated}
    dangling = [n for n in execs
                if str(n.get("type")) in _DELIVERY_TYPES
                and indeg[str(n.get("id"))] == 0
                and str(n.get("id")) not in iso_ids]
    if dangling:
        names = ", ".join(_node_name(n) for n in dangling[:6])
        warnings.append(
            f"Узлы доставки/результата без входящей стрелки: {names}. "
            "Они отработали, но НЕ получили данные — подключите к ним источник "
            "(напр. «Картинка» → «Уведомление»).")
    return warnings


async def run_process_board(graph: Dict[str, Any], *, user_id: Optional[str],
                            trigger_payload: Optional[str] = None,
                            trigger_meeting_id: Optional[str] = None,
                            lang: Optional[str] = None) -> Dict[str, Any]:
    """Прогнать процесс-доску (kind=process). Флаг BOARD_PROCESS_EXEC гейтит
    исполнение (иначе — dry-run: только порядок узлов, без вызовов).

    trigger_payload — данные события для event-триггера (текст, который
    подставится на выход триггер-узла и потечёт в граф).
    trigger_meeting_id — id встречи, запустившей триггер (узел «Данные встречи»
    без явного ID тянет артефакты именно этой встречи).
    lang — язык интерфейса пользователя (ru|en): узлы генерации/мозга/
    инфографики отвечают на нём (пусто → прежнее поведение, ru)."""
    nodes = (graph or {}).get("nodes") or []
    edges = (graph or {}).get("edges") or []
    if not nodes:
        return {"status": "error", "message": "пустой граф"}
    # Язык: явный (ручной запуск из UI) → язык на узле-триггере ДОСКИ (его
    # используют расписания и событийные триггеры — у них нет браузера) → None
    # (прежнее поведение, ru). Единая точка для всех трёх путей запуска.
    _lang = (lang or "").strip().lower()[:2]
    if not _lang:
        for n in nodes:
            if str(n.get("type")) in ("trigger", "start"):
                _nl = str((n.get("data") or {}).get("lang") or "").strip().lower()[:2]
                if _nl in ("ru", "en"):
                    _lang = _nl
                break
    ctx = {"user_id": user_id, "trigger_payload": trigger_payload,
           "trigger_meeting_id": trigger_meeting_id,
           "lang": _lang or None}

    if not process_exec_enabled():
        # Dry-run: показать порядок исполнения, ничего не вызывая.
        order = _topo_order(nodes, edges)
        return {"status": "dry_run", "order": order,
                "message": "BOARD_PROCESS_EXEC выключен — показан только порядок, узлы не запускались"}

    result = await run_graph(nodes, edges, _process_handler, ctx)

    # Человек-в-цикле (§C): прогон приостановлен на узле «дождаться ответа».
    # Сохраняем снимок; продолжим по ответу человека (resume_board). Только за
    # флагом — без него ни один узел не вернёт waiting, ветка недостижима.
    if result.get("status") == "waiting" and board_wait_enabled():
        wait_info = result.get("wait") or {}
        rs = dict(result.get("resume_state") or {})
        rs["log"] = result.get("log") or []
        wid = None
        try:
            from backend.core.board import board_wait_store as _bw
            wid = _bw.create_wait(
                str(user_id or ""),
                board_id=str((graph or {}).get("id") or "") or None,
                chat_id=str(wait_info.get("chat_id") or ""),
                nodes=nodes, edges=edges, resume_state=rs, wait_info=wait_info)
        except Exception:
            logger.warning("board wait persist failed", exc_info=True)
        return {"status": "waiting", "wait_id": wid, "wait": wait_info,
                "log": result.get("log") or [],
                "message": "Прогон ждёт ответа человека в Telegram."}

    # Честный статус: если блоки не соединены — не рапортуем зелёный «успех»
    # (иначе ✓ по каждому узлу вводит в заблуждение, хотя данные не текли).
    warnings = _connectivity_warnings(nodes, edges)
    out: Dict[str, Any] = {"status": "warning" if warnings else "success", **result}
    if warnings:
        out["warnings"] = warnings
    # Частичный провал НЕ прячем за гладким статусом: прогон дошёл до конца,
    # но в логе есть упавшие узлы → итог "partial" + errors_count. Протяжку
    # ошибки по цепочке (input_failed) не считаем — это тень одного корневого
    # сбоя, а не отдельные провалы. Бюджет-стоп уже пришёл как "partial" из
    # run_graph и переживает merge выше.
    errors_count = sum(
        1 for l in (result.get("log") or [])
        if isinstance(l, dict) and isinstance(l.get("output"), dict)
        and l["output"].get("error") and not l["output"].get("input_failed"))
    if errors_count:
        out["errors_count"] = errors_count
        if out.get("status") in ("done", "success", "warning"):
            out["status"] = "partial"
    return out


async def resume_board(user_id: str, wait_id: str, reply: str) -> Dict[str, Any]:
    """Продолжить приостановленный прогон (§C): ответ человека втекает как выход
    узла-ожидания, граф доигрывается с сохранённой позиции. Never-raise.

    Может снова остановиться (несколько ожиданий в цепочке) — тогда снова
    сохраняем новый wait и возвращаем status=waiting."""
    if not board_wait_enabled():
        return {"status": "error", "message": "ENABLE_BOARD_WAIT выключен"}
    try:
        from backend.core.board import board_wait_store as _bw
    except Exception:
        return {"status": "error", "message": "wait-store недоступен"}
    store = _bw._load(str(user_id or ""))
    rec = store.get(wait_id) if isinstance(store, dict) else None
    if not isinstance(rec, dict):
        return {"status": "error", "message": "прогон не найден"}
    if rec.get("status") != _bw.WAITING:
        return {"status": "error", "message": "прогон уже завершён/просрочен"}
    # Помечаем сразу — защита от двойного резюма (два ответа подряд).
    _bw.mark(str(user_id), wait_id, _bw.DONE)

    nodes = rec.get("nodes") or []
    edges = rec.get("edges") or []
    resume = dict(rec.get("resume_state") or {})
    resume["reply"] = str(reply or "")
    ctx = {"user_id": user_id, "trigger_payload": None}
    try:
        result = await run_graph(nodes, edges, _process_handler, ctx, resume=resume)
    except Exception as e:
        logger.warning("resume_board run failed", exc_info=True)
        return {"status": "error", "message": f"resume: {e}"}

    if result.get("status") == "waiting":
        wait_info = result.get("wait") or {}
        rs = dict(result.get("resume_state") or {})
        rs["log"] = result.get("log") or []
        wid = None
        try:
            wid = _bw.create_wait(
                str(user_id or ""), board_id=rec.get("board_id"),
                chat_id=str(wait_info.get("chat_id") or ""),
                nodes=nodes, edges=edges, resume_state=rs, wait_info=wait_info)
        except Exception:
            logger.warning("board wait re-persist failed", exc_info=True)
        return {"status": "waiting", "wait_id": wid, "wait": wait_info,
                "log": result.get("log") or []}
    return {"status": "success", **result}


async def reap_expired_waits(now: Optional[float] = None) -> Dict[str, Any]:
    """Добить просроченные ожидания (§C): человек не ответил за timeout_min.
    Помечаем EXPIRED и шлём в тот же чат вежливое «время истекло». НЕ доигрываем
    граф — чтобы не доставить клиенту не одобренный результат (безопасный дефолт).

    Вызывается планировщиком за флагом ENABLE_BOARD_WAIT. Never-raise."""
    if not board_wait_enabled():
        return {"enabled": False, "reaped": 0}
    reaped = 0
    try:
        from backend.core.board import board_wait_store as _bw
        due = _bw.due_expired_all(now=now)
        for rec in due:
            uid = str(rec.get("user_id") or "")
            wid = str(rec.get("wait_id") or "")
            if not (uid and wid):
                continue
            _bw.mark(uid, wid, _bw.EXPIRED)
            cid = str(rec.get("chat_id") or "") or None
            try:
                from backend.core.i18n import t as _t
                await _send_telegram_text(uid, _t("messenger.board_wait_expired"),
                                          chat_id=cid)
            except Exception:
                logger.debug("expired notice send skipped", exc_info=True)
            reaped += 1
    except Exception:
        logger.warning("reap_expired_waits failed", exc_info=True)
    if reaped:
        logger.info("⏳ board waits: просрочено и закрыто %d ожиданий", reaped)
    return {"enabled": True, "reaped": reaped}


__all__ = [
    "_topo_order",
    "board_wait_enabled",
    "process_exec_enabled",
    "reap_expired_waits",
    "resume_board",
    "run_graph",
    "run_process_board",
]
