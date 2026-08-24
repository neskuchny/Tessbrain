"""Трансляция стратегии: руководитель → каждому сотруднику в ЕГО фрейме.

Сценарий: CEO/директор пишет ОДНО послание («куда идём, что меняем»),
система для каждого члена организации генерирует ПЕРСОНАЛЬНУЮ версию —
на его языке, в его стиле, с его метафорами и примерами под его роль.
Источник фрейма — единый сервис адаптации core/cogni/adapt.load_frame
(preferred_style, vocabulary, metaphors, abstraction, language, реальные фразы +
уверенные когнитивные измерения). Поля заполняются communication_style_agent из
реальных встреч; адаптируется ФОРМА послания, не содержание.

Гейт человека: генерация создаёт ЧЕРНОВИК; руководитель видит все версии,
может править руками и только потом жмёт «Отправить». После отправки
сотрудник видит свою версию в приложении (inbox) и best-effort получает
её в привязанный Telegram; кнопка «Принял» замыкает контур «в общем поле».

Хранилище: data/broadcasts/<org_id>.json — атомарная запись, без БД
(объёмы: единицы объявлений на организацию). Never-raise наружу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from backend.core.cogni.adapt import CogniFrame

logger = logging.getLogger(__name__)

def _data_dir() -> str:
    """Каталог хранилища от _DATA_ROOT (cwd-независимо, как tenant_paths):
    относительный "data/broadcasts" рассыпал файлы по разным деревьям в
    зависимости от cwd процесса (API vs worker)."""
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        return os.path.join(_DATA_ROOT, "broadcasts")
    except Exception:
        return os.path.join("data", "broadcasts")

DRAFT = "draft"
SENT = "sent"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(org_id: str) -> str:
    d = _data_dir()
    os.makedirs(d, exist_ok=True)
    safe = "".join(c for c in org_id if c.isalnum() or c in "-_")
    return os.path.join(d, f"{safe}.json")


def _pg() -> bool:
    """Postgres-бэкенд стора доступов включён? (D1b; иначе — файл)."""
    try:
        from backend.core.ingest import access_repo
        return access_repo.enabled()
    except Exception:
        return False


def _load(org_id: str) -> Dict[str, Any]:
    if _pg():
        try:
            from backend.core.ingest import access_repo
            doc = access_repo.load_all("broadcasts").get(org_id)
            return doc if isinstance(doc, dict) else {"broadcasts": []}
        except Exception:
            logger.warning("broadcast PG load failed", exc_info=True)
            return {"broadcasts": []}
    try:
        with open(_path(org_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"broadcasts": []}
    except Exception:
        logger.warning("broadcast store read failed", exc_info=True)
        return {"broadcasts": []}


def _save(org_id: str, data: Dict[str, Any]) -> None:
    # Стор общий на org (пишут несколько участников) — сериализация writer'а:
    # PG — advisory-лок транзакции; файл — межпроцессный file_lock.
    if _pg():
        from backend.core.ingest import access_repo
        access_repo.guarded_replace(
            "broadcasts",
            lambda cur: cur.__setitem__(org_id, data))
        return
    p = _path(org_id)
    from backend.core.store.tenant_io import atomic_write_json, file_lock
    with file_lock(p):
        atomic_write_json(p, data, indent=1)


def _org_and_members(user_id: str) -> tuple[Optional[str], List[Dict[str, Any]]]:
    try:
        from backend.core.ingest import membership
        org_id = membership.get_org_for_user(user_id)
        if not org_id:
            return None, []
        return org_id, membership.list_members(org_id)
    except Exception:
        logger.warning("broadcast: org resolve failed", exc_info=True)
        return None, []


def _personalize_prompt(message: str, member: Dict[str, Any],
                        frame: "CogniFrame") -> str:
    from backend.core.cogni.adapt import frame_block
    role = member.get("role") or "сотрудник"
    dept = member.get("department") or ""
    return (
        "Руководитель компании обращается к команде с посланием ниже. "
        "Твоя задача — ПЕРЕСКАЗАТЬ это послание ОДНОМУ конкретному сотруднику "
        "так, чтобы он его понял, принял и оказался в общем поле с "
        "руководителем.\n\n"
        f"КОМУ: роль — {role}" + (f", отдел — {dept}" if dept else "") + "\n"
        f"ФРЕЙМ МЫШЛЕНИЯ ЭТОГО ЧЕЛОВЕКА:\n{frame_block(frame)}\n\n"
        "ПРАВИЛА:\n"
        "1. НИ ОДНОГО нового факта, обещания или цифры — только то, что есть "
        "в послании. Перевод фрейма, не искажение смысла.\n"
        "2. Обязательный блок «Что это значит для тебя» — 2-4 пункта под его "
        "роль.\n"
        "3. Используй его метафоры/стиль, если они заданы; иначе — просто и "
        "конкретно.\n"
        "4. Кратко: 120-250 слов, markdown, обращение на «ты» если стиль не "
        "требует иного.\n\n"
        f"=== ПОСЛАНИЕ РУКОВОДИТЕЛЯ ===\n{message[:6000]}\n"
    )


def _numbers(text: str) -> set:
    """Числовые токены текста — для проверки «цифра из оригинала»."""
    return set(re.findall(r"\d+(?:[.,]\d+)?", str(text or "")))


def _check_personalized(original: str, adapted: str) -> Dict[str, Any]:
    """Проверить перевод послания ПОСЛЕ модели, а не только попросить в промпте.

    Ограничители «не добавляй фактов и цифр» и «обязательный блок что это
    значит для тебя» держались исключительно на послушании модели. Для
    послания руководителя это слишком: выдуманная цифра в персональной
    версии стратегии — это не стилистическая помарка, а разные вводные у
    разных людей, то есть ровно тот конфликт, который перевод должен
    предотвращать. В соседнем каскаде целей такая проверка есть — делаем
    и здесь.

    Возвращает {"ok": bool, "new_numbers": [...], "has_meaning_block": bool}.
    Ничего не переписывает: решение (перегенерировать / показать
    руководителю предупреждение) принимает вызывающий.
    """
    src = _numbers(original)
    new_numbers = sorted(n for n in _numbers(adapted) if n not in src)
    low = str(adapted or "").lower()
    has_block = ("что это значит для тебя" in low
                 or "что это значит для вас" in low
                 or "what this means for you" in low)
    return {
        "ok": not new_numbers and has_block,
        "new_numbers": new_numbers[:10],
        "has_meaning_block": has_block,
    }


async def _personalize(author_uid: str, message: str,
                       member: Dict[str, Any]) -> str:
    from backend.core.cogni.adapt import load_frame
    frame = await load_frame(member.get("user_id", ""))
    prompt = _personalize_prompt(message, member, frame)
    # читает это сотрудник, а не автор рассылки — язык берём его
    try:
        from backend.core.llm.lang import lang_instruction, resolve_answer_lang
        prompt += lang_instruction(await resolve_answer_lang(
            str(member.get("user_id") or ""), prefer_persona=True))
    except Exception:
        logger.debug("broadcast: язык получателя не определён", exc_info=True)
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        text = await generate_for_workload(author_uid, "tz_generation", prompt)
        if text and text.strip():
            text = text.strip()
            check = _check_personalized(message, text)
            if check["new_numbers"]:
                # Цифра, которой не было в оригинале, — самый опасный вид
                # искажения: люди начнут работать по разным вводным.
                # Одна повторная попытка со строгим напоминанием.
                logger.warning(
                    "broadcast: в персональной версии появились цифры вне "
                    "оригинала (%s) — перегенерация",
                    ", ".join(check["new_numbers"]))
                strict = prompt + (
                    "\n\nВАЖНО: в прошлой версии появились числа, которых "
                    "НЕТ в исходном послании: "
                    + ", ".join(check["new_numbers"])
                    + ". Перепиши БЕЗ единой цифры, которой нет в оригинале.")
                retry = await generate_for_workload(
                    author_uid, "tz_generation", strict)
                if retry and retry.strip():
                    recheck = _check_personalized(message, retry.strip())
                    if not recheck["new_numbers"]:
                        return retry.strip()
                # Обе попытки добавили цифры — отдаём оригинал: лучше
                # неадаптированный текст, чем адаптированный с выдумкой.
                logger.warning("broadcast: перевод отклонён (цифры вне "
                               "оригинала), отдаём исходное послание")
                return message
            return text
    except Exception as e:  # noqa: BLE001
        logger.warning(f"broadcast personalize LLM failed: {e}")
    # честный фолбэк: оригинал + пометка (нет молчаливых потерь)
    return message


async def create_broadcast(author_uid: str, message: str) -> Dict[str, Any]:
    """Сгенерировать ЧЕРНОВИК: персональная версия для каждого члена орг."""
    message = (message or "").strip()
    if len(message) < 20:
        return {"status": "error",
                "message": "Послание слишком короткое — опишите суть (от 20 символов)"}
    org_id, members = _org_and_members(author_uid)
    if not org_id:
        return {"status": "error",
                "message": "У вас нет организации: создайте её во вкладке "
                           "«Организация» и пригласите сотрудников"}
    # Трансляция стратегии — канал руководителя: без гейта любой сотрудник
    # (вкл. contractor) мог разослать «послание руководства» всему оргу.
    try:
        from backend.core.ingest import membership
        author_role = membership.get_user_org_base_role(author_uid)
    except Exception:
        author_role = None
    if author_role not in ("founder", "admin", "manager"):
        return {"status": "error",
                "message": "Транслировать стратегию на организацию могут "
                           "только руководители (founder/admin/manager)"}
    recipients = [m for m in members if m.get("user_id")
                  and m["user_id"] != author_uid]
    if not recipients:
        return {"status": "error",
                "message": "В организации пока нет других участников — "
                           "пригласите сотрудников, чтобы транслировать им стратегию"}

    sem = asyncio.Semaphore(4)

    async def _one(m: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        async with sem:
            text = await _personalize(author_uid, message, m)
        return m["user_id"], {
            "text": text,
            "role": m.get("role") or "",
            "department": m.get("department") or "",
            "status": "new",
            "question": "",
        }

    pairs = await asyncio.gather(*[_one(m) for m in recipients])

    bid = f"bc_{uuid.uuid4().hex[:12]}"
    rec = {
        "id": bid,
        "org_id": org_id,
        "author_uid": author_uid,
        "original": message,
        "created_at": _now(),
        "status": DRAFT,
        "versions": {uid: v for uid, v in pairs},
    }
    data = _load(org_id)
    data["broadcasts"].append(rec)
    # ограничиваем историю (объявления — не лента, а вехи)
    data["broadcasts"] = data["broadcasts"][-50:]
    _save(org_id, data)
    return {"status": "success", "broadcast": rec}


def _find(data: Dict[str, Any], bid: str) -> Optional[Dict[str, Any]]:
    for b in data.get("broadcasts", []):
        if b.get("id") == bid:
            return b
    return None


def list_broadcasts(author_uid: str) -> Dict[str, Any]:
    org_id, _ = _org_and_members(author_uid)
    if not org_id:
        return {"broadcasts": []}
    data = _load(org_id)
    mine = [b for b in data.get("broadcasts", [])
            if b.get("author_uid") == author_uid]
    return {"broadcasts": list(reversed(mine))}


def update_version(author_uid: str, bid: str, member_uid: str,
                   text: str) -> Dict[str, Any]:
    """Ручная правка версии руководителем ДО отправки."""
    org_id, _ = _org_and_members(author_uid)
    if not org_id:
        return {"status": "error", "message": "организация не найдена"}
    data = _load(org_id)
    b = _find(data, bid)
    if not b or b.get("author_uid") != author_uid:
        return {"status": "error", "message": "объявление не найдено"}
    if b.get("status") == SENT:
        return {"status": "error", "message": "объявление уже отправлено"}
    v = (b.get("versions") or {}).get(member_uid)
    if not v:
        return {"status": "error", "message": "адресат не найден"}
    v["text"] = (text or "").strip() or v["text"]
    _save(org_id, data)
    return {"status": "success"}


async def _telegram_best_effort(member_uid: str, text: str) -> bool:
    """Дублируем персональную версию в привязанный Telegram (не критично)."""
    try:
        from backend.config import get_settings
        token = (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
        if not token:
            return False
        from backend.core.messengers.links import get_messenger_links_service
        svc = await get_messenger_links_service()
        link = await svc.find_link_for_user(user_id=member_uid,
                                            platform="telegram")
        if not link or not getattr(link, "external_id", ""):
            return False
        # markdown → Telegram-HTML с фолбэком в чистый текст
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        res = await post_telegram_text(
            token, str(link.external_id),
            f"**📣 Послание руководителя**\n\n{text[:3800]}", timeout=30.0)
        return bool(res["ok"])
    except Exception:
        logger.debug("broadcast telegram skipped", exc_info=True)
        return False


async def send_broadcast(author_uid: str, bid: str) -> Dict[str, Any]:
    org_id, _ = _org_and_members(author_uid)
    if not org_id:
        return {"status": "error", "message": "организация не найдена"}
    data = _load(org_id)
    b = _find(data, bid)
    if not b or b.get("author_uid") != author_uid:
        return {"status": "error", "message": "объявление не найдено"}
    if b.get("status") == SENT:
        return {"status": "error", "message": "объявление уже отправлено"}
    b["status"] = SENT
    b["sent_at"] = _now()
    delivered_tg = 0
    for uid, v in (b.get("versions") or {}).items():
        if await _telegram_best_effort(uid, v.get("text", "")):
            v["telegram"] = True
            delivered_tg += 1
    _save(org_id, data)
    # сигнал в UI получателям (fire-and-forget)
    for uid in (b.get("versions") or {}):
        try:
            from backend.api.websocket.signals_ws import publish_signal
            publish_signal("strategy_broadcast", {
                "broadcast_id": bid, "title": "Послание руководителя",
            }, tenant_id=uid)
        except Exception:
            logger.debug("broadcast signal skipped", exc_info=True)
    return {"status": "success", "broadcast_id": bid,
            "recipients": len(b.get("versions") or {}),
            "telegram_delivered": delivered_tg}


def inbox(member_uid: str) -> Dict[str, Any]:
    """Входящие сотрудника: его персональные версии отправленных объявлений."""
    org_id, _ = _org_and_members(member_uid)
    if not org_id:
        return {"items": []}
    data = _load(org_id)
    items = []
    for b in data.get("broadcasts", []):
        if b.get("status") != SENT:
            continue
        v = (b.get("versions") or {}).get(member_uid)
        if not v:
            continue
        items.append({
            "broadcast_id": b["id"],
            "created_at": b.get("sent_at") or b.get("created_at"),
            "text": v.get("text", ""),
            "status": v.get("status", "new"),
        })
    return {"items": list(reversed(items))}


def ack(member_uid: str, bid: str, question: str = "") -> Dict[str, Any]:
    """«Понял, принял» (+ необязательный вопрос руководителю)."""
    org_id, _ = _org_and_members(member_uid)
    if not org_id:
        return {"status": "error", "message": "организация не найдена"}
    data = _load(org_id)
    b = _find(data, bid)
    v = ((b or {}).get("versions") or {}).get(member_uid)
    if not v:
        return {"status": "error", "message": "объявление не найдено"}
    v["status"] = "acked"
    if (question or "").strip():
        v["question"] = question.strip()[:2000]
    v["acked_at"] = _now()
    _save(org_id, data)
    return {"status": "success"}
