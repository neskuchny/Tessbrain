# -*- coding: utf-8 -*-
"""Переписки как источник мозга: TG-группы и Slack-каналы.

Без переписок мозг видит только встречи и документы — договорённости из
рабочих чатов для него не существуют. Этот модуль закрывает дыру честно
и безопасно:

- **Telegram-группы**: бота компании добавляют в группу, и привязанный к
  системе участник пишет там `/brain_on` — группа привязывается к ЕГО
  тенанту и включается. Пока команда не прозвучала, сообщения группы НЕ
  сохраняются вообще (даже «на всякий случай»): бот молчит и не пишет на
  диск. `/brain_off` — выключить. Привязать группу может только аккаунт,
  уже связанный с системой (lookup_by_external) — чужой человек в чужой
  группе не подключит её к своему тенанту.
- **Slack-каналы**: pull через уже существующую SlackIntegration
  (bot_token из «Интеграций», scope channels:history) — синк по кнопке
  забирает историю выбранных каналов.

Сообщения складываются в per-source буфер (cap, дедуп по (ts, автор, текст)),
«Собрать в мозг» строит из них KB-документ «Переписка: <название>» (тот же
механизм, что экспорт аудитории в чат) — дальше мозг ищет по нему как по
любому документу. Сам дайджест — без LLM: сырые цитаты честнее пересказа.

«Анализ» (analyze_source) — единственное место, где LLM участвует, и он
ИНКРЕМЕНТАЛЬНЫЙ: каждый прогон видит только сообщения, появившиеся после
предыдущего анализа (analyzed_upto_ts) — уже разобранные не жгут токены
повторно, а находки накапливаются, не затираются.

Deny-by-default: ENABLE_CHAT_INGEST (OFF) — без флага /brain_on честно
отвечает, что функция выключена, и ничего не пишет.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MSG_CAP = 4000          # сообщений в буфере источника
_DIGEST_MSGS = 1200      # сколько последних сообщений уходит в KB-документ
_TEXT_CAP = 2000         # длина одного сообщения


def ingest_enabled() -> bool:
    return os.environ.get("ENABLE_CHAT_INGEST", "").strip().lower() in (
        "1", "on", "true", "yes")


# ── Хранилище ────────────────────────────────────────────────────────────

def _store_dir() -> Path:
    p = Path("data/chat_ingest")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(s))[:64] or "x"


def _sources_path(user_id: str) -> Path:
    return _store_dir() / f"{_safe(user_id)}.json"


def _msgs_path(user_id: str, key: str) -> Path:
    return _store_dir() / f"{_safe(user_id)}__{_safe(key)}.json"


def _groups_index_path() -> Path:
    # глобальный индекс «chat_id группы → владелец»: вебхуку нужно роутить
    # сообщение группы в нужный тенант без перебора всех пользователей
    return _store_dir() / "_tg_groups.json"


def _read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data: Any) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_key(platform: str, chat_id: str) -> str:
    return f"{platform}:{chat_id}"


def list_sources(user_id: str) -> List[Dict[str, Any]]:
    data = _read_json(_sources_path(user_id), {})
    out = []
    for key, s in (data.get("sources") or {}).items():
        out.append({"key": key, **s})
    out.sort(key=lambda s: s.get("added_at") or "", reverse=True)
    return out


def _save_sources(user_id: str, sources: Dict[str, Any]) -> None:
    # user_id хранится внутри файла: имя файла санитизировано и из него
    # исходный id не восстановить — а планировщику нужно перечислить
    # пользователей с источниками (list_ingest_users)
    _write_json(_sources_path(user_id), {"user_id": user_id,
                                         "sources": sources})


def list_ingest_users() -> List[str]:
    """Пользователи, у которых есть источники переписки (для автоцикла)."""
    out: List[str] = []
    try:
        for p in _store_dir().glob("*.json"):
            if p.name.startswith("_") or "__" in p.name:
                continue
            data = _read_json(p, {})
            uid = str(data.get("user_id") or "")
            if uid and (data.get("sources") or {}):
                out.append(uid)
    except Exception:
        logger.debug("chat_ingest: list users failed", exc_info=True)
    return out


def set_source_enabled(user_id: str, key: str, enabled: bool) -> bool:
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    if key not in sources:
        return False
    sources[key]["enabled"] = bool(enabled)
    _save_sources(user_id, sources)
    if key.startswith("telegram:") and not enabled:
        _unbind_group(key.split(":", 1)[1])
    return True


def set_source_mode(user_id: str, key: str, mode: str) -> bool:
    """Режим источника: "brain" (дайджест уходит в цифровой мозг, default)
    или "context_only" (в мозг НЕ выгружается никогда — доступен только как
    явно выбираемый контекст чата). Для чатов с партнёрами и прочего, что
    владелец сознательно не хочет индексировать в общий мозг."""
    if mode not in ("brain", "context_only"):
        return False
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    if key not in sources:
        return False
    sources[key]["mode"] = mode
    _save_sources(user_id, sources)
    return True


def source_mode(src: Dict[str, Any]) -> str:
    return str(src.get("mode") or "brain")


def source_collect_files(src: Dict[str, Any]) -> bool:
    """Собирать ли вложения-документы этого источника (default: да)."""
    return bool(src.get("collect_files", True))


def set_source_files(user_id: str, key: str, enabled: bool) -> bool:
    """Переключатель сбора файлов: владелец решает, забирать ли документы
    из этой переписки в базу знаний (сообщения — отдельная настройка)."""
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    if key not in sources:
        return False
    sources[key]["collect_files"] = bool(enabled)
    _save_sources(user_id, sources)
    return True


def _register_source(user_id: str, *, platform: str, chat_id: str,
                     title: str, enabled: bool,
                     identity_source: str = "") -> str:
    key = source_key(platform, chat_id)
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    rec = sources.get(key) or {"platform": platform, "chat_id": str(chat_id),
                               "added_at": _now(), "message_count": 0}
    rec["title"] = title or rec.get("title") or str(chat_id)
    rec["enabled"] = enabled
    if identity_source:
        # видно в UI: КАК система поняла, чей это аккаунт
        rec["identity_source"] = identity_source
    sources[key] = rec
    _save_sources(user_id, sources)
    return key


def append_messages(user_id: str, key: str,
                    messages: List[Dict[str, str]]) -> int:
    """Дозаписать сообщения в буфер источника.

    Что сохраняется: время, имя автора (как в мессенджере), его id в
    мессенджере (для сшивки с людьми из встреч) и текст. Что НЕ
    сохраняется: медиа, голосовые, стикеры, реакции, пересылки-вложения.
    Дедуп по (ts, author, text); буфер держится ОТСОРТИРОВАННЫМ по времени —
    догруженная вручную старая переписка встаёт на своё место, а не в конец."""
    if not messages:
        return 0
    p = _msgs_path(user_id, key)
    buf: List[Dict[str, str]] = _read_json(p, [])
    seen = {(m.get("ts"), m.get("author"), m.get("text")) for m in buf}
    added = 0
    participants: Dict[str, Dict[str, Any]] = {}
    for m in messages:
        text = str(m.get("text") or "").strip()[:_TEXT_CAP]
        if not text:
            continue
        rec = {"ts": str(m.get("ts") or _now()),
               "author": str(m.get("author") or "")[:120], "text": text}
        aid = str(m.get("author_id") or "").strip()
        if aid:
            rec["author_id"] = aid
        sig = (rec["ts"], rec["author"], rec["text"])
        if sig in seen:
            continue
        seen.add(sig)
        buf.append(rec)
        added += 1
        pk = aid or rec["author"]
        if pk:
            part = participants.setdefault(
                pk, {"name": rec["author"], "count": 0})
            part["count"] += 1
            if aid:
                part["external_id"] = aid
    if added:
        buf.sort(key=lambda m: str(m.get("ts") or ""))
        buf = buf[-_MSG_CAP:]
        _write_json(p, buf)
        data = _read_json(_sources_path(user_id), {})
        sources = data.get("sources") or {}
        if key in sources:
            src = sources[key]
            src["message_count"] = len(buf)
            src["last_message_at"] = buf[-1]["ts"]
            allp = src.setdefault("participants", {})
            for pk, part in participants.items():
                cur = allp.setdefault(pk, {"name": part["name"], "count": 0})
                cur["count"] = int(cur.get("count", 0)) + part["count"]
                cur["name"] = part["name"] or cur.get("name")
                if part.get("external_id"):
                    cur["external_id"] = part["external_id"]
            _save_sources(user_id, sources)
    return added


# ── Telegram-группы (вебхук бота) ────────────────────────────────────────

def _groups_index() -> Dict[str, Any]:
    return _read_json(_groups_index_path(), {})


def _bind_group(chat_id: str, user_id: str) -> None:
    idx = _groups_index()
    idx[str(chat_id)] = {"user_id": user_id,
                        "key": source_key("telegram", chat_id)}
    _write_json(_groups_index_path(), idx)


def _unbind_group(chat_id: str) -> None:
    idx = _groups_index()
    if str(chat_id) in idx:
        del idx[str(chat_id)]
        _write_json(_groups_index_path(), idx)


def _author_of(message: Dict[str, Any]) -> str:
    frm = message.get("from") or {}
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name"))
                    if x).strip()
    uname = str(frm.get("username") or "").strip()
    return name or (f"@{uname}" if uname else "участник")


async def handle_telegram_group_update(message: Dict[str, Any]
                                       ) -> Optional[str]:
    """Сообщение из TG-группы. Возврат: текст ответа в группу (только на
    команды) или None — бот в группе молчит.

    Приватность по конструкции: пока группа не привязана командой
    /brain_on от связанного с системой участника — на диск не пишется
    НИЧЕГО, включая название группы."""
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return None
    text = str(message.get("text") or "").strip()
    cmd = text.split("@", 1)[0].strip().lower() if text.startswith("/") else ""

    if cmd == "/brain_on":
        if not ingest_enabled():
            return ("Сбор переписки выключен на сервере "
                    "(ENABLE_CHAT_INGEST). Попросите администратора "
                    "включить и повторите /brain_on.")
        owner, how = await _linked_user_for(message)
        if not owner:
            return ("Не могу определить, к какому аккаунту привязать группу: "
                    "ваш Telegram в системе не найден.\n\n"
                    "Подойдёт любой из двух способов:\n"
                    "1. «Интеграции → Telegram → Контакты» — добавьте себя "
                    "(ваш Telegram ID), либо\n"
                    "2. «Интеграции → Мессенджеры → Telegram → Подключить» — "
                    "откроется этот бот, нажмите Start.\n\n"
                    "Важно: то, что бот присылает вам отчёты, само по себе "
                    "не привязка — отчёты идут на вписанный Chat ID (у вас "
                    "это, похоже, группа). Потом повторите /brain_on здесь.")
        title = str(chat.get("title") or chat_id)
        _register_source(owner, platform="telegram", chat_id=chat_id,
                         title=title, enabled=True, identity_source=how)
        _bind_group(chat_id, owner)
        return (f"✅ Группа «{title}» подключена как источник мозга.\n"
                f"Аккаунт определён по: {how}. Если это не ваш аккаунт — "
                "сразу /brain_off.\n"
                "Сообщения будут собираться и попадать в базу знаний "
                "(«Знания → Синхронизация → Переписки»). Отключить — "
                "/brain_off.")

    if cmd == "/brain_off":
        idx = _groups_index()
        rec = idx.get(chat_id)
        if not rec:
            return "Эта группа и так не подключена."
        set_source_enabled(rec["user_id"], rec["key"], False)
        _unbind_group(chat_id)
        return "⏹ Группа отключена: сообщения больше не собираются."

    # обычное сообщение: пишем ТОЛЬКО если группа привязана и включена
    if not ingest_enabled():
        return None
    rec = _groups_index().get(chat_id)
    if not rec:
        return None
    user_id, key = rec["user_id"], rec["key"]
    src = next((s for s in list_sources(user_id) if s["key"] == key), None)
    if not src or not src.get("enabled"):
        return None
    if text:
        ts = message.get("date")
        iso = (datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
               if ts else _now())
        append_messages(user_id, key,
                        [{"ts": iso, "author": _author_of(message),
                          "author_id": str((message.get("from") or {})
                                           .get("id") or ""),
                          "text": text}])
    # вложение-документ (pdf/docx/txt/md/csv) → база знаний; в буфере
    # остаётся след «прислал файл», чтобы контекст переписки его видел
    doc = message.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        try:
            note = await ingest_telegram_document(user_id, src, doc,
                                                  author=_author_of(message))
        except Exception:
            logger.warning("chat_ingest: document ingest failed",
                           exc_info=True)
            note = None
        if note:
            ts = message.get("date")
            iso = (datetime.fromtimestamp(int(ts), tz=timezone.utc)
                   .isoformat() if ts else _now())
            append_messages(user_id, key, [{
                "ts": iso, "author": _author_of(message),
                "author_id": str((message.get("from") or {}).get("id") or ""),
                "text": f"[{note}]"}])
    return None


def _digits(s: Any) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


async def _owner_by_integrations(tg_user_id: str) -> Tuple[Optional[str], str]:
    """Владелец по данным «Интеграций»: этот Telegram записан в аккаунте как
    контакт или как Default Chat ID (личный чат).

    Зачем: привязку через «Подключить → Start» (таблица messenger_links)
    делают не все — многие просто вписывают свой Telegram в «Интеграции»
    (оттуда им и приходят отчёты). Для НИХ /brain_on раньше выглядел
    сломанным, хотя система их Telegram знает.

    Безопасность: значение вписано владельцем аккаунта у себя же — это его
    заявление «этот Telegram мой/наш». Принимаем ТОЛЬКО если претендент
    ровно один: два аккаунта с одним и тем же id — отказ (иначе чужая
    группа могла бы уехать не туда). Вызывается лишь на /brain_on, не на
    каждое сообщение: тут расшифровка секретов всех тенантов."""
    want = _digits(tg_user_id)
    if not want:
        return None, ""
    try:
        from backend.api.routes.integrations import decrypt_secret
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()
        # контакты берём по ВСЕМ провайдерам: строку мог завести не только
        # экран «Интеграции → Telegram», но и смежный продукт на той же базе
        # (провайдер тогда другой, а тип контакта — телеграмный)
        rows = await supabase._request(
            "GET", "/rest/v1/user_integrations",
            params={"key_name": "like.contact_*",
                    "select": "user_id,provider,key_name,secret_enc,meta",
                    "limit": "5000"}) or []
        rows += await supabase._request(
            "GET", "/rest/v1/user_integrations",
            params={"provider": "eq.telegram",
                    "key_name": "eq.default_chat_id",
                    "select": "user_id,provider,key_name,secret_enc,meta",
                    "limit": "5000"}) or []
    except Exception:
        logger.debug("chat_ingest: integrations lookup failed", exc_info=True)
        return None, ""
    if not rows:
        # ноль строк на весь запрос — это не «контактов нет», а скорее
        # доступ: ключ Supabase без прав на чтение таблицы (RLS)
        logger.warning("chat_ingest: user_integrations вернул 0 строк — "
                       "проверьте права ключа Supabase (RLS)")

    _TG_CONTACT_TYPES = {"user_id", "telegram", "telegram_id",
                         "telegram_user_id", "username", "chat_id"}
    claimants: Dict[str, str] = {}      # user_id → как именно найден
    for row in rows:
        key_name = str(row.get("key_name") or "")
        provider = str(row.get("provider") or "").lower()
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        ctype = str((meta or {}).get("contact_type") or "").lower()
        is_contact = key_name.startswith("contact_")
        if is_contact:
            # чужой провайдер (например, телефон WhatsApp) не должен
            # случайно совпасть цифрами с telegram-id
            if provider != "telegram" and ctype not in _TG_CONTACT_TYPES:
                continue
        elif key_name != "default_chat_id":
            continue
        try:
            value = decrypt_secret(row.get("secret_enc") or "")
        except Exception:
            continue
        if _digits(value) != want:
            continue
        uid = str(row.get("user_id") or "").strip()
        if not uid:
            continue
        label = str((meta or {}).get("display_name") or "").strip()
        claimants[uid] = (f"контакт «{label}»" if label and is_contact
                          else ("контакт в «Интеграциях»" if is_contact
                                else "Default Chat ID в «Интеграциях»"))
    if len(claimants) == 1:
        uid, how = next(iter(claimants.items()))
        logger.info("chat_ingest: владелец группы определён по интеграциям "
                    "(%s), tg_user=%s", how, tg_user_id)
        return uid, how
    if len(claimants) > 1:
        logger.warning("chat_ingest: этот Telegram записан в %d аккаунтах — "
                       "привязка группы отклонена (неоднозначно)",
                       len(claimants))
    return None, ""


# ── Документы из TG-групп → база знаний ─────────────────────────────────

_DOC_EXTS = {".pdf", ".docx", ".txt", ".md", ".csv"}
_DOC_MAX_BYTES = 20 * 1024 * 1024   # лимит Telegram Bot API на getFile


def _tg_bot_token() -> str:
    try:
        from backend.config import get_settings
        tok = (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


async def ingest_telegram_document(user_id: str, src: Dict[str, Any],
                                   doc: Dict[str, Any],
                                   author: str = "") -> Optional[str]:
    """Вложение-документ из подключённой группы → текст → KB-документ.

    Поддержаны текстовые форматы (pdf/docx/txt/md/csv — парсер тот же, что
    у загрузки документов). Фото/сканы не берём — OCR у нас нет, честно в
    роадмапе. Режим «только контекст» документы в мозг не пускает — как и
    сообщения. Возврат: заметка для лога/UI или None (пропущено)."""
    if not source_collect_files(src):
        logger.info("chat_ingest: вложение пропущено — сбор файлов для "
                    "«%s» выключен владельцем", src.get("title"))
        return None
    name = str(doc.get("file_name") or "file").strip()
    ext = Path(name).suffix.lower()
    if ext not in _DOC_EXTS:
        logger.info("chat_ingest: вложение «%s» пропущено (формат %s не "
                    "поддержан; берём pdf/docx/txt/md/csv)", name, ext or "?")
        return None
    size = int(doc.get("file_size") or 0)
    if size > _DOC_MAX_BYTES:
        logger.info("chat_ingest: вложение «%s» пропущено (%d МБ > лимита "
                    "Telegram getFile 20 МБ)", name, size // (1024 * 1024))
        return None
    if source_mode(src) == "context_only":
        logger.info("chat_ingest: вложение «%s» не ушло в мозг — источник в "
                    "режиме «только контекст»", name)
        return None
    token = _tg_bot_token()
    file_id = str(doc.get("file_id") or "")
    if not (token and file_id):
        return None

    import tempfile

    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id})
            fp = ((r.json() or {}).get("result") or {}).get("file_path")
            if not fp:
                logger.warning("chat_ingest: getFile не вернул путь для «%s»",
                               name)
                return None
            blob = (await client.get(
                f"https://api.telegram.org/file/bot{token}/{fp}")).content
    except Exception:
        logger.warning("chat_ingest: скачивание «%s» не удалось", name,
                       exc_info=True)
        return None

    ftype = ("pdf" if ext == ".pdf"
             else "docx" if ext == ".docx" else "text")
    try:
        from backend.core.capture.document_processor import DocumentProcessor
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(blob)
            tmp_path = Path(tf.name)
        try:
            text = await DocumentProcessor(
                use_semantic_chunking=False)._extract_text(tmp_path, ftype)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.warning("chat_ingest: парсинг «%s» не удался: %s", name, e)
        return None
    text = (text or "").strip()
    if not text:
        logger.info("chat_ingest: «%s» без извлекаемого текста — пропущен",
                    name)
        return None

    title = f"Файл из переписки: {name} ({src.get('title') or ''})"[:120]
    content = (f"# {name}\n\n_Источник: файл из переписки "
               f"«{src.get('title')}»"
               + (f", прислал(а) {author}" if author else "")
               + f", {_now()[:10]}._\n\n" + text[:60000])
    try:
        from backend.core.marketing.chat_context import upsert_kb_document
        res = await upsert_kb_document(user_id, title=title, content=content)
    except Exception:
        logger.warning("chat_ingest: KB-документ для «%s» не сохранился",
                       name, exc_info=True)
        return None
    # счётчик файлов на источнике — видно в UI
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    key = str(src.get("key") or "")
    if key in sources:
        sources[key]["files_ingested"] = int(
            sources[key].get("files_ingested", 0) or 0) + 1
        _save_sources(user_id, sources)
    logger.info("chat_ingest: файл «%s» из «%s» → база знаний (doc=%s)",
                name, src.get("title"), res.get("id"))
    return f"файл «{name}» добавлен в базу знаний"


async def _linked_user_for(message: Dict[str, Any]
                           ) -> Tuple[Optional[str], str]:
    """(user_id системы, как определили) по отправителю сообщения.

    Два источника: (1) подтверждённая привязка «Подключить → Start»
    (messenger_links); (2) данные «Интеграций» этого же Telegram —
    контакт или Default Chat ID."""
    frm = message.get("from") or {}
    tg_user_id = str(frm.get("id") or "")
    if not tg_user_id:
        return None, ""
    try:
        from backend.core.messengers.links import get_messenger_links_service
        svc = await get_messenger_links_service()
        link = await svc.lookup_by_external(platform="telegram",
                                            external_id=tg_user_id)
        uid = getattr(link, "user_id", None) if link else None
        if uid:
            return str(uid), "привязка «Подключить»"
    except Exception:
        logger.debug("chat_ingest: link lookup failed", exc_info=True)
    return await _owner_by_integrations(tg_user_id)


# ── Slack-каналы (pull через SlackIntegration) ───────────────────────────

async def sync_slack(user_id: str, channels: List[str]) -> Dict[str, Any]:
    """Забрать историю каналов Slack в буферы. Требует bot_token из
    «Интеграций» (scope channels:history)."""
    if not ingest_enabled():
        return {"status": "disabled",
                "message": "Сбор переписки выключен (ENABLE_CHAT_INGEST)."}
    channels = [str(c).strip().lstrip("#") for c in (channels or [])
                if str(c).strip()]
    if not channels:
        return {"status": "error", "message": "укажите каналы"}
    try:
        from backend.integrations.slack_integration import SlackIntegration
        slack = SlackIntegration(user_id)
        ok = await slack.load_credentials()
        if not ok or not slack.get_key("bot_token"):
            return {"status": "error",
                    "message": ("Slack bot_token не настроен — впишите его "
                                "во вкладке «Интеграции → Slack»")}
    except Exception as e:
        return {"status": "error", "message": f"Slack недоступен: {e}"}

    # conversations.history принимает ID канала (C…), не имя — резолвим
    # имена через conversations.list один раз
    name_to_id: Dict[str, str] = {}
    try:
        raw = await slack.list_channels(limit=500)
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        for c in (data.get("channels") or []):
            if c.get("name") and c.get("id"):
                name_to_id[str(c["name"]).lower()] = str(c["id"])
    except Exception:
        logger.debug("chat_ingest: slack list_channels failed", exc_info=True)

    synced, errors = [], []
    for ch in channels[:10]:
        ch_id = ch if re.fullmatch(r"[CGD][A-Z0-9]{6,}", ch) \
            else name_to_id.get(ch.lower(), ch)
        try:
            raw = await slack.read_messages(ch_id, limit=200)
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            msgs = data.get("messages") or []
            norm = []
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                txt = str(m.get("text") or "").strip()
                if not txt:
                    continue
                ts = str(m.get("ts") or "")
                try:
                    iso = datetime.fromtimestamp(
                        float(ts), tz=timezone.utc).isoformat()
                except Exception:
                    iso = _now()
                norm.append({"ts": iso,
                             "author": str(m.get("user_name")
                                           or m.get("user") or "")[:120],
                             "text": txt})
            key = _register_source(user_id, platform="slack", chat_id=ch,
                                   title=f"#{ch}", enabled=True)
            added = append_messages(user_id, key, norm)
            files_added = 0
            try:
                fresh = next((s for s in list_sources(user_id)
                              if s.get("key") == key), None)
                if fresh is not None:
                    files_added = await _ingest_slack_files(
                        user_id, fresh, slack, ch_id)
            except Exception:
                logger.warning("chat_ingest: файлы канала «%s» не "
                               "обработались", ch, exc_info=True)
            synced.append({"channel": ch, "fetched": len(norm),
                           "added": added, "files": files_added})
        except Exception as e:
            errors.append({"channel": ch, "error": str(e)[:200]})
    status = "success" if synced else "error"
    return {"status": status, "synced": synced, "errors": errors,
            **({"message": "ни один канал не прочитан"}
               if not synced else {})}


async def _parse_blob_to_text(blob: bytes, name: str) -> str:
    """Байты файла → текст общим DocumentProcessor. Пусто = не распарсилось."""
    import tempfile

    ext = Path(name).suffix.lower()
    ftype = ("pdf" if ext == ".pdf"
             else "docx" if ext == ".docx" else "text")
    try:
        from backend.core.capture.document_processor import DocumentProcessor
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(blob)
            tmp_path = Path(tf.name)
        try:
            text = await DocumentProcessor(
                use_semantic_chunking=False)._extract_text(tmp_path, ftype)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return (text or "").strip()
    except Exception as e:
        logger.warning("chat_ingest: парсинг «%s» не удался: %s", name, e)
        return ""


async def _ingest_slack_files(user_id: str, src: Dict[str, Any], slack: Any,
                              channel_id: str) -> int:
    """Файлы Slack-канала → база знаний (files.list + url_private с Bearer).

    Дедуп по slack file id (ingested_file_ids на источнике) — повторный
    синк не перезаливает старые файлы. Тот же фильтр форматов и та же
    настройка collect_files, что у TG-вложений."""
    if source_mode(src) == "context_only" or not source_collect_files(src):
        return 0
    try:
        raw = await slack._api("files.list", channel=channel_id, count=50)
        files = (raw or {}).get("files") or []
    except Exception:
        logger.debug("chat_ingest: slack files.list failed", exc_info=True)
        return 0
    if not files:
        return 0
    token = slack.get_key("bot_token")
    seen = set((src.get("ingested_file_ids") or []))
    added = 0

    import httpx
    for f in files:
        fid = str(f.get("id") or "")
        name = str(f.get("name") or "file").strip()
        url = str(f.get("url_private") or "")
        if not fid or fid in seen or not url:
            continue
        if Path(name).suffix.lower() not in _DOC_EXTS:
            continue
        if int(f.get("size") or 0) > _DOC_MAX_BYTES:
            logger.info("chat_ingest: slack-файл «%s» пропущен (слишком "
                        "большой)", name)
            continue
        try:
            async with httpx.AsyncClient(timeout=60.0,
                                         follow_redirects=True) as client:
                r = await client.get(url, headers={
                    "Authorization": f"Bearer {token}"})
                blob = r.content if r.status_code == 200 else b""
        except Exception:
            logger.warning("chat_ingest: скачивание slack-файла «%s» не "
                           "удалось", name, exc_info=True)
            continue
        text = await _parse_blob_to_text(blob, name)
        if not text:
            continue
        author = str(f.get("user") or "")
        title = (f"Файл из переписки: {name} "
                 f"({src.get('title') or channel_id})")[:120]
        content = (f"# {name}\n\n_Источник: файл из Slack-канала "
                   f"«{src.get('title')}»"
                   + (f", прислал(а) {author}" if author else "")
                   + f", {_now()[:10]}._\n\n" + text[:60000])
        try:
            from backend.core.marketing.chat_context import upsert_kb_document
            await upsert_kb_document(user_id, title=title, content=content)
        except Exception:
            logger.warning("chat_ingest: KB-документ для «%s» не сохранился",
                           name, exc_info=True)
            continue
        seen.add(fid)
        added += 1
        logger.info("chat_ingest: slack-файл «%s» из «%s» → база знаний",
                    name, src.get("title"))

    if added:
        data = _read_json(_sources_path(user_id), {})
        sources = data.get("sources") or {}
        key = str(src.get("key") or "")
        if key in sources:
            sources[key]["ingested_file_ids"] = sorted(seen)[-500:]
            sources[key]["files_ingested"] = int(
                sources[key].get("files_ingested", 0) or 0) + added
            _save_sources(user_id, sources)
    return added


# ── Дайджест → база знаний (мозг) ────────────────────────────────────────

# ── Просмотр и ручная загрузка старых диалогов ───────────────────────────

def read_buffer(user_id: str, key: str, *, limit: int = 100,
                offset: int = 0) -> Dict[str, Any]:
    """Просмотр буфера (старые — раньше, порядок хронологический)."""
    msgs = _read_json(_msgs_path(user_id, key), [])
    total = len(msgs)
    off = max(0, int(offset))
    return {"total": total,
            "messages": msgs[off:off + max(1, min(int(limit), 500))]}


_WA_IOS_RE = re.compile(
    r"^\[(\d{1,2}[./]\d{1,2}[./]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?)\]\s+"
    r"([^:]{1,80}):\s(.*)$")
_WA_ANDROID_RE = re.compile(
    r"^(\d{1,2}[./]\d{1,2}[./]\d{2,4}),?\s+(\d{1,2}:\d{2})\s+-\s+"
    r"([^:]{1,80}):\s(.*)$")


def _parse_wa_ts(d: str, t: str) -> str:
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M:%S",
                "%d.%m.%y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(f"{d} {t}", fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except Exception:
            continue
    return _now()


def _parse_telegram_export(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """result.json из «Экспорт истории чата» Telegram Desktop."""
    out: List[Dict[str, str]] = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict) or m.get("type") not in (None, "message"):
            continue
        text = m.get("text")
        if isinstance(text, list):
            text = "".join(
                p if isinstance(p, str) else str((p or {}).get("text") or "")
                for p in text)
        text = str(text or "").strip()
        if not text:
            continue
        out.append({"ts": str(m.get("date") or _now()),
                    "author": str(m.get("from") or "участник"),
                    "author_id": str(m.get("from_id") or ""),
                    "text": text})
    return out


def _parse_whatsapp_txt(raw: str) -> List[Dict[str, str]]:
    """Экспорт чата WhatsApp (.txt): iOS `[дата, время] Имя: текст` и
    Android `дата, время - Имя: текст`. Строки-продолжения клеятся к
    предыдущему сообщению."""
    out: List[Dict[str, str]] = []
    for line in raw.splitlines():
        s = line.strip("‎‏ ").rstrip()
        if not s:
            continue
        m = _WA_IOS_RE.match(s) or _WA_ANDROID_RE.match(s)
        if m:
            d, t, author, text = m.groups()
            if text.strip():
                out.append({"ts": _parse_wa_ts(d, t),
                            "author": author.strip(), "text": text.strip()})
        elif out:
            out[-1]["text"] = (out[-1]["text"] + "\n" + s)[:_TEXT_CAP]
    return out


def import_dialog(user_id: str, *, title: str, content: str) -> Dict[str, Any]:
    """Ручная загрузка СТАРОЙ переписки: экспорт Telegram (result.json) или
    WhatsApp (.txt). Формат распознаётся сам; нераспознанное — честный отказ,
    а не свалка «всего подряд». Это и есть путь для WhatsApp: API истории
    групп не отдаёт, а экспорт чата — отдаёт."""
    if not ingest_enabled():
        return {"status": "disabled",
                "message": "Сбор переписки выключен (ENABLE_CHAT_INGEST)."}
    content = (content or "").strip()
    if not content:
        return {"status": "error", "message": "пустой файл"}
    msgs: List[Dict[str, str]] = []
    platform = "import"
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("messages") is not None:
            msgs = _parse_telegram_export(data)
            platform = "telegram-export"
            title = (title or str(data.get("name") or "")).strip()
    except Exception:
        pass
    if not msgs:
        msgs = _parse_whatsapp_txt(content)
        if msgs:
            platform = "whatsapp-export"
    if not msgs:
        return {"status": "error",
                "message": ("формат не распознан: поддерживаются экспорт "
                            "Telegram (result.json) и WhatsApp (.txt «дата - "
                            "Имя: текст»). Пересохраните экспорт и повторите")}
    title = (title or "импортированный диалог").strip()[:120]
    key = _register_source(user_id, platform=platform,
                           chat_id=_safe(title), title=title, enabled=True)
    added = append_messages(user_id, key, msgs)
    return {"status": "success", "key": key, "platform": platform,
            "parsed": len(msgs), "added": added,
            "note": (f"распознано {len(msgs)} сообщений, новых {added}. "
                     "Дальше: сшивка участников и «В мозг»/«Анализ»")}


# ── Сшивка участников с людьми из встреч (только с подтверждением) ──────

def _identity_map(src: Dict[str, Any]) -> Dict[str, Any]:
    return src.get("identity") or {}


async def suggest_participant_matches(user_id: str, key: str
                                      ) -> Dict[str, Any]:
    """Участники источника + КАНДИДАТЫ на сшивку с Person из встреч.

    Матч по имени — это ПРЕДПОЛОЖЕНИЕ, не факт: система сама никого не
    связывает. Каждому кандидату нужен явный confirm от пользователя
    (confirm_participant) — ровно та «дополнительная проверка, понял ли
    он, кто это», о которой просил владелец. До подтверждения сшивка
    нигде не утверждается."""
    from difflib import SequenceMatcher
    src = next((s for s in list_sources(user_id) if s["key"] == key), None)
    if not src:
        return {"status": "error", "message": "источник не найден"}

    people: List[Dict[str, Any]] = []
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
            people = await gen.get_all_people_profiles(tenant_id=user_id) or []
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception:
        logger.debug("chat_ingest: people unavailable", exc_info=True)

    identity = _identity_map(src)
    out = []
    for pk, part in (src.get("participants") or {}).items():
        row: Dict[str, Any] = {"author": pk, "name": part.get("name"),
                               "count": part.get("count", 0)}
        confirmed = identity.get(pk)
        if confirmed:
            row["confirmed"] = confirmed   # {person_id, person_name} | {person_id: ""}
        else:
            name = str(part.get("name") or "").lower()
            cands = []
            for p in people:
                pname = str(p.get("name") or "").lower()
                if not (name and pname):
                    continue
                ratio = SequenceMatcher(None, name, pname).ratio()
                # фамилия/имя целиком внутри — тоже кандидат («Иван» vs
                # «Иван Петров»)
                contains = (name in pname or pname in name) and len(name) >= 3
                if ratio >= 0.6 or contains:
                    cands.append({"person_id": p.get("id"),
                                  "person_name": p.get("name"),
                                  "role": p.get("role") or "",
                                  "score": round(max(ratio,
                                                     0.6 if contains else 0),
                                                 2)})
            cands.sort(key=lambda c: -c["score"])
            if cands:
                row["candidates"] = cands[:3]
        out.append(row)
    out.sort(key=lambda r: -int(r.get("count") or 0))
    return {"status": "success", "participants": out}


def confirm_participant(user_id: str, key: str, author: str,
                        person_id: str, person_name: str = "") -> bool:
    """Подтвердить (или отклонить: person_id="") сшивку участника с Person.

    Решение принимает человек — система только предлагала кандидатов."""
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    src = sources.get(key)
    if not src or author not in (src.get("participants") or {}):
        return False
    identity = src.setdefault("identity", {})
    identity[author] = {"person_id": str(person_id or ""),
                        "person_name": str(person_name or ""),
                        "confirmed_at": _now()}
    _save_sources(user_id, sources)
    return True


# ── Анализ: из переписки — проверяемые наборы данных, не свалка ──────────

def _quote_in_buffer(quote: str, msgs: List[Dict[str, str]]) -> bool:
    q = " ".join(str(quote or "").lower().split())
    if len(q) < 8:
        return False
    for m in msgs:
        if q in " ".join(str(m.get("text") or "").lower().split()):
            return True
    return False


async def _persist_chat_tasks(user_id: str, src: Dict[str, Any],
                              tasks: List[Dict[str, Any]]) -> int:
    """Извлечённые из переписки задачи → Task-узлы личного графа со связью
    CREATED_FROM → Chat-узел источника (тот же паттерн, что задачи встреч в
    task_analysis._persist_extracted_tasks).

    Дальше конвейер общий: пульс собирает Task-узлы из merged-графа, дедуп
    ledger сверяет их с трекерами (SequenceMatcher по названию в скоупе
    исполнителя) — «поставлено в чате, в задачник не занесено» становится
    видно так же, как для встреч. Best-effort: граф недоступен → 0."""
    if not tasks:
        return 0
    import uuid as _uuid

    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user
    try:
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
    except Exception:
        logger.warning("chat_ingest: граф недоступен для задач", exc_info=True)
        return 0
    if not gb.connected:
        return 0
    count = 0
    try:
        chat_key = str(src.get("key") or "")
        title = str(src.get("title") or chat_key)
        chat_node = await gb._merge_node(
            "Chat", "chat_key", chat_key,
            {"chat_key": chat_key, "title": title,
             "platform": str(src.get("platform") or ""),
             "access_group": "public"})
        for t in tasks:
            tid = str(_uuid.uuid4())
            node = await gb._merge_node(
                "Task", "task_id", tid,
                {"task_id": tid, "title": str(t.get("title") or "")[:200],
                 "description": str(t.get("quote") or "")[:400],
                 "status": "todo",
                 "assignee": str(t.get("assignee") or ""),
                 "source_chat": title, "chat_key": chat_key,
                 "origin": "chat_ingest", "access_group": "public"})
            if node and chat_node:
                await gb._create_relationship(node, chat_node,
                                              "CREATED_FROM", {})
                count += 1
        try:
            gb.save_graph()
        except Exception:
            pass
    except Exception:
        logger.warning("chat_ingest: задачи не легли в граф", exc_info=True)
    finally:
        try:
            await gb.close(save=True)
        except Exception:
            pass
    return count


_ANALYSIS_CATS = ("agreements", "tasks", "client_facts", "decisions")
_MAX_ANALYSIS_ITEMS = 60   # на категорию, накопительно — старейшее вытесняется


def _merge_analysis_rows(existing: List[Dict[str, Any]],
                         new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Слить находки нового прогона со старыми: без дублей по цитате,
    старое не теряется, категория не растёт бесконечно."""
    seen = {r.get("quote", "") for r in existing}
    out = list(existing)
    for r in new:
        q = r.get("quote", "")
        if q in seen:
            continue
        seen.add(q)
        out.append(r)
    return out[-_MAX_ANALYSIS_ITEMS:]


async def analyze_source(user_id: str, key: str) -> Dict[str, Any]:
    """Извлечь из переписки СТРУКТУРУ: договорённости, задачи, факты о
    клиентах, решения. Каждый пункт обязан нести дословную цитату-опору;
    пункт, чья цитата НЕ находится в проанализированной части, отбрасывается
    (та же дисциплина grounding, что у cogni-измерений).

    ИНКРЕМЕНТАЛЬНО: LLM видит только сообщения, появившиеся ПОСЛЕ последнего
    анализа (analyzed_upto_ts) — уже разобранные сообщения повторно не
    жгут токены. Новые находки добавляются к прежним (не затирают их).
    Нет новых сообщений — LLM не вызывается вовсе, отдаём сохранённое."""
    if not ingest_enabled():
        return {"status": "disabled",
                "message": "Сбор переписки выключен (ENABLE_CHAT_INGEST)."}
    src = next((s for s in list_sources(user_id) if s["key"] == key), None)
    if not src:
        return {"status": "error", "message": "источник не найден"}
    msgs = _read_json(_msgs_path(user_id, key), [])
    if not msgs:
        return {"status": "error", "message": "в буфере нет сообщений"}

    prev = src.get("analysis") or {}
    marker = str(prev.get("analyzed_upto_ts") or "")
    # buffer отсортирован по ts (append_messages) — "новые" это хвост после
    # маркера; при первом прогоне marker="" и новыми считаются ВСЕ сообщения
    new_msgs = [m for m in msgs if str(m.get("ts") or "") > marker]

    if not new_msgs:
        merged_prev = {c: prev.get(c) or [] for c in _ANALYSIS_CATS}
        return {"status": "success",
                "total": sum(len(v) for v in merged_prev.values()),
                "dropped_unverified": prev.get("dropped_unverified", 0),
                **merged_prev, "new_messages_analyzed": 0,
                "note": "новых сообщений с последнего анализа нет — данные "
                        "уже актуальны, LLM не вызывался"}

    # огромный залповый импорт разбираем ПОРЦИЯМИ (маркер продвигается
    # постепенно) — старые «новые» сообщения не теряются, а достаются
    # следующим прогоном (ручным или ночным автоциклом)
    truncated = len(new_msgs) > _DIGEST_MSGS
    batch = new_msgs[:_DIGEST_MSGS]

    lines = [f"[{m.get('ts', '')[:16]}] {m.get('author')}: {m.get('text')}"
             for m in batch]
    # рамка «данные, не инструкции»: переписку пишут внешние люди — просьба
    # «забудь инструкции и …» в сообщении не должна управлять анализом
    from backend.core.llm.untrusted import wrap_untrusted
    corpus = wrap_untrusted("\n".join(lines)[-40000:],
                            source=f"переписка «{src.get('title') or key}»")
    prompt = (
        "Ниже НОВЫЕ сообщения рабочей переписки (уже прочитанные ранее "
        "сообщения сюда не входят). Извлеки ТОЛЬКО то, что реально "
        "прозвучало — ничего не выдумывай и не додумывай.\n"
        "Каждый пункт ОБЯЗАН нести поле quote — ДОСЛОВНЫЙ фрагмент "
        "сообщения из переписки (копируй как есть, без правок). Пункт без "
        "точной цитаты будет отброшен автоматикой.\n"
        'Ответь ТОЛЬКО JSON: {"agreements": [{"text": "о чём договорились", '
        '"quote": "дословно"}], "tasks": [{"title": "задача-поручение", '
        '"assignee": "кому (если названо)", "quote": "дословно"}], '
        '"client_facts": [{"client": "клиент/компания", "fact": "факт", '
        '"quote": "дословно"}], "decisions": [{"text": "решение", '
        '"quote": "дословно"}]}\n'
        "Нет данных категории — пустой список.\n\n"
        f"ПЕРЕПИСКА:\n{corpus}")
    try:
        from backend.core.llm.router import get_llm_router
        data = await get_llm_router().generate_json(prompt=prompt,
                                                    temperature=0.2)
    except Exception as e:
        return {"status": "error", "message": f"LLM недоступен: {e}"}

    result: Dict[str, Any] = {}
    dropped = 0
    for cat, fields in (("agreements", ("text",)), ("tasks", ("title",)),
                        ("client_facts", ("client", "fact")),
                        ("decisions", ("text",))):
        rows = []
        for r in ((data or {}).get(cat) or [])[:30]:
            if not isinstance(r, dict):
                continue
            if not all(str(r.get(f) or "").strip() for f in fields):
                continue
            quote = str(r.get("quote") or "").strip()
            # grounding: цитата обязана дословно находиться в НОВОЙ порции
            # (это и есть то, что реально видел LLM)
            if not _quote_in_buffer(quote, batch):
                dropped += 1
                continue
            row = {f: str(r.get(f) or "").strip()[:300] for f in fields}
            if cat == "tasks" and str(r.get("assignee") or "").strip():
                row["assignee"] = str(r.get("assignee")).strip()[:120]
            row["quote"] = quote[:400]
            rows.append(row)
        result[cat] = rows

    merged = {c: _merge_analysis_rows(prev.get(c) or [], result.get(c) or [])
             for c in _ANALYSIS_CATS}
    total_dropped = int(prev.get("dropped_unverified", 0) or 0) + dropped
    messages_analyzed = (int(prev.get("messages_analyzed", 0) or 0)
                        + len(batch))

    analysis = {"extracted_at": _now(),
                "analyzed_upto_ts": batch[-1]["ts"],
                "messages_analyzed": messages_analyzed,
                "dropped_unverified": total_dropped, **merged}
    data_s = _read_json(_sources_path(user_id), {})
    sources = data_s.get("sources") or {}
    if key in sources:
        sources[key]["analysis"] = analysis
        _save_sources(user_id, sources)

    # свежие задачи этого прогона → Task-узлы графа → их видит пульс и
    # сверяет с трекерами. ТОЛЬКО свежие (не merged) — иначе дубли при
    # каждом прогоне; ТОЛЬКО режим «в мозг» — context_only в граф не пишет
    tasks_persisted = 0
    if result.get("tasks") and source_mode(src) != "context_only":
        tasks_persisted = await _persist_chat_tasks(user_id, src,
                                                    result["tasks"])

    total = sum(len(v) for v in merged.values())
    note = (f"новых сообщений разобрано: {len(batch)}; без цитатной опоры "
            f"отброшено в этом прогоне: {dropped}")
    if tasks_persisted:
        note += (f". Задач-поручений отправлено в граф (видны пульсу): "
                 f"{tasks_persisted}")
    if truncated:
        note += (f". В очереди ещё {len(new_msgs) - len(batch)} — запустите "
                 "«Анализ» ещё раз (или дождитесь ночного автоцикла)")
    return {"status": "success", "total": total,
            "dropped_unverified": total_dropped, **merged,
            "new_messages_analyzed": len(batch),
            "tasks_persisted": tasks_persisted, "note": note}


def _analysis_markdown(analysis: Dict[str, Any]) -> str:
    parts = ["\n## Извлечённые данные (каждый пункт опёрт на цитату)",
             f"_Проанализировано сообщений: "
             f"{analysis.get('messages_analyzed', '?')}; пунктов без "
             f"дословной опоры отброшено: "
             f"{analysis.get('dropped_unverified', 0)}._"]
    for cat, title in (("agreements", "Договорённости"),
                       ("tasks", "Задачи-поручения"),
                       ("client_facts", "Факты о клиентах"),
                       ("decisions", "Решения")):
        rows = analysis.get(cat) or []
        if not rows:
            continue
        parts.append(f"\n### {title}")
        for r in rows:
            head = r.get("text") or r.get("title") or ""
            if cat == "client_facts":
                head = f"{r.get('client')}: {r.get('fact')}"
            if r.get("assignee"):
                head += f" — {r['assignee']}"
            parts.append(f"- {head}\n  - 💬 «{r.get('quote')}»")
    return "\n".join(parts)


def _digest_markdown(title: str, msgs: List[Dict[str, str]],
                     identity: Optional[Dict[str, Any]] = None) -> str:
    """Сырые сообщения по дням. Имя автора аннотируется человеком из встреч
    ТОЛЬКО если сшивку явно подтвердил пользователь — предположения системы
    в документ не попадают."""
    identity = identity or {}
    confirmed_names: Dict[str, str] = {}
    for _pk, rec in identity.items():
        if rec.get("person_id") and rec.get("person_name"):
            confirmed_names[_pk] = rec["person_name"]

    lines = [f"# Переписка: {title}",
             "",
             "_Источник: рабочий чат. Сырые сообщения (без пересказа): что "
             "написано — то и есть. Пометка «= …» у имени — подтверждённая "
             "пользователем сшивка с человеком из встреч._", ""]
    day = ""
    for m in msgs:
        d = str(m.get("ts") or "")[:10]
        if d != day:
            day = d
            lines.append(f"\n## {d}")
        author = m.get("author") or "участник"
        pk = str(m.get("author_id") or "") or author
        linked = confirmed_names.get(pk) or confirmed_names.get(author)
        if linked and linked != author:
            author = f"{author} (= {linked})"
        lines.append(f"- **{author}**: {m.get('text')}")
    return "\n".join(lines)


async def digest_to_brain(user_id: str, key: str) -> Dict[str, Any]:
    """Буфер источника → KB-документ (upsert по названию). Мозг дальше ищет
    по нему так же, как по любому документу."""
    src = next((s for s in list_sources(user_id) if s["key"] == key), None)
    if not src:
        return {"status": "error", "message": "источник не найден"}
    if source_mode(src) == "context_only":
        return {"status": "error",
                "message": ("источник в режиме «только контекст чата» — в "
                            "цифровой мозг не выгружается. Поменяйте режим "
                            "на «в мозг», если передумали")}
    msgs = _read_json(_msgs_path(user_id, key), [])
    if not msgs:
        return {"status": "error",
                "message": ("в буфере нет сообщений — для TG проверьте, что "
                            "бот в группе и группа включена (/brain_on), "
                            "для Slack — нажмите синк")}
    md = _digest_markdown(src.get("title") or key, msgs[-_DIGEST_MSGS:],
                          identity=_identity_map(src))
    if src.get("analysis"):
        md += "\n" + _analysis_markdown(src["analysis"])
    title = f"Переписка: {src.get('title') or key}"[:120]
    try:
        from backend.core.marketing.chat_context import upsert_kb_document
        res = await upsert_kb_document(user_id, title=title, content=md)
    except Exception as e:
        return {"status": "error",
                "message": f"не удалось сохранить документ: {e}"}
    data = _read_json(_sources_path(user_id), {})
    sources = data.get("sources") or {}
    if key in sources:
        sources[key]["last_digest_at"] = _now()
        _save_sources(user_id, sources)
    return {"status": "success", "document_id": res.get("id"),
            "title": title, "messages": len(msgs[-_DIGEST_MSGS:]),
            "updated": res.get("updated", False)}


# ── Автоцикл (планировщик): Slack re-pull → анализ → в мозг ──────────────

async def auto_cycle(user_id: str) -> Dict[str, Any]:
    """Периодическая обработка включённых источников — аналог синхронизации
    встреч, но для переписки:

    1. Slack-источники дотягиваются повторным pull (TG-группам pull не
       нужен — их сообщения приходят на вебхук в реальном времени);
    2. источник с новыми сообщениями после последнего дайджеста → анализ
       (структура с цитатами) → «В мозг» (KB-документ, upsert).

    Каждый шаг идёт через повторы: сбой сети или лимит провайдера не должен
    молча съедать кусок переписки. Что всё-таки не доехало после повторов —
    попадает в журнал неудач (backend/core/ingest/resilience) и в счётчик
    `failed`, чтобы «всё упало» не выглядело как «нечего было делать».

    Выключенные источники не трогаются; без флага — честный disabled."""
    if not ingest_enabled():
        return {"status": "disabled"}

    from backend.core.ingest.resilience import (
        default_attempts,
        record_failure,
        with_retries,
    )
    attempts = default_attempts()

    stats = {"status": "success", "synced_slack": 0, "analyzed": 0,
             "digested": 0, "skipped": 0, "failed": 0}

    async def _step(coro_factory, *, stage: str, key: str,
                    platform: str = "") -> Optional[Dict[str, Any]]:
        """Шаг с повторами. None = не доехало (уже записано в журнал)."""
        try:
            return await with_retries(coro_factory,
                                      label=f"{stage}:{key}")
        except Exception as exc:
            stats["failed"] += 1
            record_failure(user_id, source_key=key, stage=stage, error=exc,
                           attempts=attempts, platform=platform)
            # warning, а не debug: молчащий лог — причина, по которой эти
            # потери раньше были невидимы в проде
            logger.warning("chat_ingest auto: %s не удался для %s после "
                           "%d попыток: %s", stage, key, attempts, exc)
            return None

    for src in list_sources(user_id):
        if not src.get("enabled"):
            stats["skipped"] += 1
            continue
        key = src["key"]
        platform = str(src.get("platform") or "")
        if platform == "slack":
            chat_id = src.get("chat_id") or ""
            res = await _step(lambda: sync_slack(user_id, [chat_id]),
                              stage="slack_sync", key=key, platform=platform)
            if res and res.get("status") == "success":
                stats["synced_slack"] += 1
        fresh = next((s for s in list_sources(user_id)
                      if s["key"] == key), src)
        last_msg = str(fresh.get("last_message_at") or "")
        last_dig = str(fresh.get("last_digest_at") or "")
        if not last_msg or (last_dig and last_msg <= last_dig):
            stats["skipped"] += 1
            continue
        res = await _step(lambda: analyze_source(user_id, key),
                          stage="analyze", key=key, platform=platform)
        if res and res.get("status") == "success":
            stats["analyzed"] += 1
        # режим «только контекст» — анализ полезен (нужен для контекста
        # чата), но в мозг НИЧЕГО не выгружается
        if source_mode(fresh) == "context_only":
            continue
        res = await _step(lambda: digest_to_brain(user_id, key),
                          stage="digest", key=key, platform=platform)
        if res and res.get("status") == "success":
            stats["digested"] += 1

    if stats["failed"]:
        stats["status"] = "partial"
    return stats
