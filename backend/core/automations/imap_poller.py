# -*- coding: utf-8 -*-
"""IMAP-поллер входящей почты → событие `new_email` (как вебхук /inbound/email).

Опциональный, БЕЗОПАСНЫЙ путь на случай, когда нет внешнего mail-провайдера
(SendGrid Inbound Parse / Mailgun), который сам шлёт вебхук. Поллер раз в N минут
заходит в почтовый ящик по IMAP, читает НЕПРОЧИТАННЫЕ письма и эмитит по каждому
событие `new_email` — доски с триггером «Входящее письмо» запускаются, текст
письма течёт в граф. Тот же payload, что у вебхука → поведение идентично.

БЕЗОПАСНОСТЬ (deny-by-default):
- Весь путь под env-гейтом ENABLE_IMAP_POLLER. Выключено → poll_once() сразу
  возвращает {"enabled": False}, ни одного подключения не происходит.
- Учётные данные — только из env (IMAP_HOST/PORT/USER/PASSWORD), никогда в
  коде/данных. IMAP_USER_ID — тенант, чьи доски запускаются.
- Только stdlib (imaplib + email) — новых зависимостей нет.
- Письма читаются через BODY.PEEK (флаг \\Seen НЕ ставится при чтении); \\Seen
  выставляется ТОЛЬКО после успешного эмита события — так письмо не теряется
  при сбое и не обрабатывается повторно (дедуп по «непрочитанности»).
- Клиент IMAP инжектируется (client_factory) — форма разбора тестируется без
  реального сервера. Never-raise на уровне джобы.
"""
from __future__ import annotations

import asyncio
import email
import logging
import os
from email.header import decode_header, make_header
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def poller_enabled() -> bool:
    """Глобальный гейт. Пусто/0/false/no → выключено (безопасный дефолт)."""
    return str(os.environ.get("ENABLE_IMAP_POLLER", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _config() -> Optional[Dict[str, Any]]:
    """IMAP-конфиг из env. None (с логом), если не хватает обязательного."""
    host = (os.environ.get("IMAP_HOST") or "").strip()
    user = (os.environ.get("IMAP_USER") or "").strip()
    pwd = os.environ.get("IMAP_PASSWORD") or ""
    uid_tenant = (os.environ.get("IMAP_USER_ID") or "").strip()
    missing = [n for n, v in (("IMAP_HOST", host), ("IMAP_USER", user),
                              ("IMAP_PASSWORD", pwd), ("IMAP_USER_ID", uid_tenant)) if not v]
    if missing:
        logger.warning("IMAP-поллер: не заданы %s", ", ".join(missing))
        return None
    try:
        port = int(os.environ.get("IMAP_PORT") or 993)
    except (TypeError, ValueError):
        port = 993
    try:
        maxn = max(1, min(100, int(os.environ.get("IMAP_MAX") or 20)))
    except (TypeError, ValueError):
        maxn = 20
    ssl = str(os.environ.get("IMAP_SSL", "true")).strip().lower() not in ("0", "false", "no", "off")
    return {"host": host, "port": port, "user": user, "password": pwd,
            "folder": (os.environ.get("IMAP_FOLDER") or "INBOX").strip() or "INBOX",
            "user_id": uid_tenant, "max": maxn, "ssl": ssl}


def _default_client(cfg: Dict[str, Any]):
    import imaplib
    if cfg["ssl"]:
        return imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    return imaplib.IMAP4(cfg["host"], cfg["port"])


def _decode(raw: Any) -> str:
    """MIME-заголовок (=?utf-8?...) → человекочитаемая строка. Never-raise."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _body_text(msg: "email.message.Message") -> str:
    """Текстовое тело письма (предпочитаем text/plain, без вложений)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, "replace").strip()
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace").strip()
    except Exception:
        return ""


def _parse_message(raw_bytes: bytes) -> Dict[str, str]:
    """Сырое письмо (RFC822) → {from, subject, body}."""
    msg = email.message_from_bytes(raw_bytes)
    return {"from": _decode(msg.get("From")),
            "subject": _decode(msg.get("Subject")),
            "body": _body_text(msg)}


def _fetch_unseen(cfg: Dict[str, Any],
                  client_factory: Optional[Callable] = None
                  ) -> List[Tuple[bytes, Dict[str, str]]]:
    """БЛОКИРУЮЩЕЕ: зайти в ящик, взять НЕПРОЧИТАННЫЕ (BODY.PEEK — без \\Seen),
    вернуть [(uid, {from,subject,body})]. Выполняется в отдельном потоке."""
    factory = client_factory or _default_client
    cli = factory(cfg)
    out: List[Tuple[bytes, Dict[str, str]]] = []
    try:
        cli.login(cfg["user"], cfg["password"])
        cli.select(cfg["folder"])
        typ, data = cli.uid("SEARCH", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return out
        uids = data[0].split()[: cfg["max"]]
        for uid in uids:
            typ, fetched = cli.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not fetched:
                continue
            raw = next((part[1] for part in fetched
                        if isinstance(part, tuple) and len(part) >= 2), None)
            if not raw:
                continue
            try:
                out.append((uid, _parse_message(raw)))
            except Exception:
                logger.debug("IMAP: не удалось разобрать письмо", exc_info=True)
    finally:
        try:
            cli.logout()
        except Exception:
            pass
    return out


def _mark_seen(cfg: Dict[str, Any], uids: List[bytes],
               client_factory: Optional[Callable] = None) -> None:
    """БЛОКИРУЮЩЕЕ: пометить письма прочитанными (\\Seen) — дедуп."""
    if not uids:
        return
    factory = client_factory or _default_client
    cli = factory(cfg)
    try:
        cli.login(cfg["user"], cfg["password"])
        cli.select(cfg["folder"])
        for uid in uids:
            try:
                cli.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            except Exception:
                logger.debug("IMAP: не удалось пометить \\Seen", exc_info=True)
    finally:
        try:
            cli.logout()
        except Exception:
            pass


def _payload(parsed: Dict[str, str]) -> Dict[str, Any]:
    """Тот же формат, что у вебхука /inbound/email."""
    parts = []
    if parsed.get("from"):
        parts.append(f"От: {parsed['from']}")
    if parsed.get("subject"):
        parts.append(f"Тема: {parsed['subject']}")
    if parsed.get("body"):
        parts.append("\n" + parsed["body"])
    return {"text": "\n".join(parts).strip() or (parsed.get("subject") or "письмо"),
            "from": parsed.get("from", ""), "subject": parsed.get("subject", ""),
            "body": parsed.get("body", "")}


async def _emit_new_email(user_id: str, payload: Dict[str, Any]) -> bool:
    """Эмит `new_email` в EventBus (+ идемпотентная подписка досок). Never-raise."""
    try:
        from backend.core.automations.event_bus import get_event_bus
        try:
            from backend.core.board.event_triggers import register_board_event_handlers
            register_board_event_handlers()  # идемпотентно
        except Exception as _e:
            logger.debug("imap: board handlers registration skipped: %s", _e)
        await get_event_bus().emit(event_type="new_email", payload=payload,
                                   user_id=user_id, source="imap_poller")
        return True
    except Exception as e:
        logger.warning("imap emit failed: %s", e)
        return False


async def poll_once(*, client_factory: Optional[Callable] = None,
                    emit: Optional[Callable] = None) -> Dict[str, Any]:
    """Один проход поллера: UNSEEN → эмит `new_email` → \\Seen. Never-raise.

    client_factory/emit инжектируются в тестах. Возвращает сводку
    {enabled, fetched, emitted}."""
    if not poller_enabled():
        return {"enabled": False, "fetched": 0, "emitted": 0}
    cfg = _config()
    if not cfg:
        return {"enabled": True, "fetched": 0, "emitted": 0, "error": "config"}
    emit_fn = emit or _emit_new_email
    try:
        messages = await asyncio.to_thread(_fetch_unseen, cfg, client_factory)
    except Exception as e:
        logger.warning("IMAP-поллер: чтение не удалось: %s", e)
        return {"enabled": True, "fetched": 0, "emitted": 0, "error": str(e)}
    done_uids: List[bytes] = []
    for uid, parsed in messages:
        ok = await emit_fn(cfg["user_id"], _payload(parsed))
        if ok:
            done_uids.append(uid)
    if done_uids:
        try:
            await asyncio.to_thread(_mark_seen, cfg, done_uids, client_factory)
        except Exception as e:
            logger.debug("IMAP-поллер: \\Seen не проставлен: %s", e)
    if done_uids:
        logger.info("📬 IMAP-поллер: обработано %d писем", len(done_uids))
    return {"enabled": True, "fetched": len(messages), "emitted": len(done_uids)}
