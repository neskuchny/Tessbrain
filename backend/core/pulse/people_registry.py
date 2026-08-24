# -*- coding: utf-8 -*-
"""Реестр людей «Пульса»: сотрудник ↔ каналы ↔ строки-assignee ↔ руководитель.

Без него персональный пуш невозможен: assignee во всех источниках — свободная
строка имени («Вася», «Василий П.»), а каналы (telegram/email) привязаны к
аккаунтам, которых у исполнителей часто нет.

Запись реестра:
    person_key      — канонический ключ (нормализованное имя)
    names[]         — варианты написания, по которым матчатся assignee-строки
    email           — куда слать письма (участники встреч уже несут email)
    telegram_chat_id— куда слать TG (личный чат сотрудника с ботом компании)
    reports_to      — person_key руководителя (цепочка эскалации)
    quiet_hours     — [start_h, end_h] UTC, когда НЕ пушить
    muted           — сотрудник отписался; уважается и видно руководителю

Автозаполнение создаёт ЧЕСТНЫЕ ЗАГЛУШКИ по assignee-строкам реестра задач:
человек без каналов виден руководителю как «не знаю, кому напоминать про X»,
а не теряется молча. Досклейка каналов — руками через API/UI (образец UX —
crm_workload.set_owner_mapping). Хранение: data/pulse_people/<uid>.json.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _path(user_id: str) -> Path:
    base = Path(os.environ.get("PULSE_PEOPLE_DIR", "").strip()
                or "data/pulse_people")
    return base / f"{user_id}.json"


def _norm_name(s: Any) -> str:
    return " ".join(str(s or "").lower().replace("ё", "е").split())


def load_registry(user_id: str) -> Dict[str, Dict[str, Any]]:
    try:
        p = _path(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("pulse people registry unreadable", exc_info=True)
    return {}


def save_registry(user_id: str, reg: Dict[str, Dict[str, Any]]) -> None:
    p = _path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def resolve(reg: Dict[str, Dict[str, Any]], assignee: Any
            ) -> Optional[Dict[str, Any]]:
    """assignee-строка → запись реестра (по person_key и вариантам имён).

    Матч консервативный: точное нормализованное совпадение с одним из имён
    либо совпадение «фамилия+имя» как подмножество токенов. Неуверенный матч
    хуже отсутствия — чужой человек не должен получать чужие задачи."""
    a = _norm_name(assignee)
    if not a:
        return None
    if a in reg:
        return reg[a]
    a_tokens = set(a.split())
    for rec in reg.values():
        for name in rec.get("names") or []:
            n = _norm_name(name)
            if n == a:
                return rec
            n_tokens = set(n.split())
            # «василий петров» ↔ «петров василий» / «василий»+фамилия
            if len(a_tokens) >= 2 and a_tokens == n_tokens:
                return rec
    return None


def upsert_person(user_id: str, *, name: str,
                  names: Optional[List[str]] = None,
                  email: str = "", telegram_chat_id: str = "",
                  reports_to: str = "", quiet_hours: Optional[List[int]] = None,
                  muted: Optional[bool] = None) -> Dict[str, Any]:
    """Создать/дополнить запись. Пустые значения не затирают заполненные."""
    key = _norm_name(name)
    if not key:
        raise ValueError("name required")
    reg = load_registry(user_id)
    rec = reg.get(key) or {"person_key": key, "names": [name], "email": "",
                           "telegram_chat_id": "", "reports_to": "",
                           "quiet_hours": None, "muted": False}
    for n in [name] + list(names or []):
        if n and n not in rec["names"]:
            rec["names"].append(n)
    if email.strip():
        rec["email"] = email.strip()
    if telegram_chat_id.strip():
        rec["telegram_chat_id"] = telegram_chat_id.strip()
    if reports_to.strip():
        rec["reports_to"] = _norm_name(reports_to)
    if quiet_hours is not None:
        rec["quiet_hours"] = quiet_hours or None
    if muted is not None:
        rec["muted"] = bool(muted)
    reg[key] = rec
    save_registry(user_id, reg)
    return rec


def autofill_from_tasks(user_id: str, tasks: List[Dict[str, Any]]
                        ) -> Dict[str, Any]:
    """Заглушки для всех assignee из реестра задач + emails из графа Person.

    Ничего не угадывает про каналы: только фиксирует людей, чтобы пробелы
    были видны. Возврат — статистика для UI."""
    reg = load_registry(user_id)
    created = 0
    for t in tasks:
        raw = str(t.get("assignee") or t.get("assignee_name") or "").strip()
        if not raw or resolve(reg, raw):
            continue
        key = _norm_name(raw)
        reg[key] = {"person_key": key, "names": [raw], "email": "",
                    "telegram_chat_id": "", "reports_to": "",
                    "quiet_hours": None, "muted": False}
        created += 1
    save_registry(user_id, reg)
    unmapped = [r for r in reg.values()
                if not (r.get("email") or r.get("telegram_chat_id"))]
    return {"total": len(reg), "created": created,
            "without_channel": len(unmapped),
            "without_channel_names": [r["names"][0] for r in unmapped][:20]}
