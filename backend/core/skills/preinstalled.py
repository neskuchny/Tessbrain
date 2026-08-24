# -*- coding: utf-8 -*-
"""Предустановленные скиллы (аудит UI: «должны быть и предустановленные»).

Каждый новый пользователь получает базовый набор скиллов при первом
обращении к списку (`ensure_preinstalled_skills` — идемпотентно, по
`preinstalled_key` в description-маркере). Скиллы — обычные AgentAutomation
с is_skill=true: исполняются ReAct-агентом через AgentAutomationService,
параметры подставляются в instruction_template (см. skills/parameters.py).

Семантика удаления: delete в AgentAutomationService — жёсткий (тумбстоунов
нет), поэтому набор сидится ТОЛЬКО когда у пользователя нет НИ ОДНОГО
preinstalled-скилла (первый вход). Удалил часть — не возвращаем (его выбор);
удалил все — при следующем входе набор вернётся (восстановление по умолчанию).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PREINSTALLED_MARK = "[preinstalled:"


def _mark(key: str) -> str:
    return f"{_PREINSTALLED_MARK}{key}]"


PREINSTALLED_SKILLS: List[Dict[str, Any]] = [
    {
        "key": "meeting_digest",
        "name": "Дайджест встречи",
        "description": "Краткое резюме встречи: решения, задачи, риски, договорённости.",
        "instruction_template": (
            "Найди встречу «{meeting}» в памяти компании и подготовь дайджест: "
            "1) ключевые решения, 2) задачи и ответственные, 3) риски и открытые "
            "вопросы, 4) договорённости и следующие шаги. Если данных мало — "
            "скажи честно, что нашлось."),
        "parameters_schema": [
            {"name": "meeting", "type": "str", "required": True,
             "default": None, "description": "Название или дата встречи"},
        ],
    },
    {
        "key": "person_brief",
        "name": "Досье по человеку",
        "description": "Сводка по сотруднику/контакту: роль, проекты, договорённости, динамика.",
        "instruction_template": (
            "Собери досье по «{person}» из памяти компании: роль и зона "
            "ответственности, активные проекты и задачи, последние "
            "договорённости, динамика за период (что изменилось), открытые "
            "вопросы. Отдели факты от твоих выводов."),
        "parameters_schema": [
            {"name": "person", "type": "str", "required": True,
             "default": None, "description": "Имя сотрудника или контакта"},
        ],
    },
    {
        "key": "weekly_report",
        "name": "Отчёт за неделю",
        "description": "Что произошло за неделю: встречи, решения, прогресс, проблемы.",
        "instruction_template": (
            "Подготовь отчёт за последние {days} дней по данным компании: "
            "прошедшие встречи и их итоги, принятые решения, прогресс по "
            "проектам, возникшие проблемы и риски. Опирайся на свежие данные, "
            "не путай со старыми."),
        "parameters_schema": [
            {"name": "days", "type": "int", "required": False,
             "default": 7, "description": "Период в днях"},
        ],
    },
    {
        "key": "project_risk_map",
        "name": "Риск-карта проекта",
        "description": "Риски, блокеры и слабые места проекта по данным встреч и задач.",
        "instruction_template": (
            "Построй риск-карту проекта «{project}»: текущие риски и блокеры "
            "(из встреч/задач/обсуждений), что уже сорвано или под угрозой, "
            "кто перегружен, какие зависимости критичны. Для каждого риска — "
            "на чём основан вывод и что предпринять."),
        "parameters_schema": [
            {"name": "project", "type": "str", "required": True,
             "default": None, "description": "Название проекта"},
        ],
    },
    {
        "key": "client_proposal",
        "name": "КП для клиента",
        "description": "Черновик коммерческого предложения на основе контекста компании.",
        "instruction_template": (
            "Подготовь черновик коммерческого предложения для «{client}» по "
            "теме «{topic}». Используй контекст компании: наше "
            "позиционирование, кейсы, прошлые обсуждения с этим клиентом. "
            "Структура: проблема клиента → решение → ценность → следующий шаг. "
            "Пробелы в данных перечисли отдельно."),
        "parameters_schema": [
            {"name": "client", "type": "str", "required": True,
             "default": None, "description": "Клиент/компания"},
            {"name": "topic", "type": "str", "required": True,
             "default": None, "description": "Тема/продукт предложения"},
        ],
    },
    {
        "key": "newcomer_onboarding",
        "name": "Brief для нового сотрудника",
        "description": "Вводная по компании/команде/проекту для онбординга.",
        "instruction_template": (
            "Подготовь вводный brief для нового сотрудника в роли «{role}»: "
            "чем живёт компания сейчас (проекты, приоритеты), ключевые люди и "
            "кто за что отвечает, принятые практики и договорённости, что "
            "важно знать в первую неделю."),
        "parameters_schema": [
            {"name": "role", "type": "str", "required": True,
             "default": None, "description": "Роль нового сотрудника"},
        ],
    },
    {
        "key": "followup_deal",
        "name": "Follow-up по сделке",
        "description": "Письмо-продолжение после встречи с клиентом: итоги и следующие шаги.",
        "instruction_template": (
            "Составь follow-up письмо для «{client}» по итогам последнего "
            "контакта: что обсудили, о чём договорились, следующие шаги с "
            "датами. Тон деловой и тёплый. Если последнего контакта в данных "
            "нет — скажи об этом."),
        "parameters_schema": [
            {"name": "client", "type": "str", "required": True,
             "default": None, "description": "Клиент/контакт"},
        ],
    },
    {
        "key": "team_sync_prep",
        "name": "Подготовка к синку команды",
        "description": "Повестка для синка: статусы, рассинхроны, вопросы к обсуждению.",
        "instruction_template": (
            "Подготовь повестку синка команды «{team}»: статусы по активным "
            "задачам, обнаруженные рассинхроны/противоречия в договорённостях, "
            "вопросы, требующие решения, и что застряло с прошлого синка."),
        "parameters_schema": [
            {"name": "team", "type": "str", "required": True,
             "default": None, "description": "Команда или проект"},
        ],
    },
]


def _installed_keys(existing: list) -> set:
    """Какие preinstalled-ключи уже есть (или были) у пользователя."""
    keys = set()
    for a in existing:
        desc = getattr(a, "description", "") or ""
        if _PREINSTALLED_MARK in desc:
            try:
                keys.add(desc.split(_PREINSTALLED_MARK, 1)[1].split("]", 1)[0])
            except Exception:
                continue
    return keys


async def ensure_preinstalled_skills(svc, user_id: str) -> int:
    """Идемпотентно установить предустановленные скиллы (первый вход).

    Если у пользователя уже есть ХОТЬ ОДИН preinstalled-скилл — ничего не
    делаем (повторный сидинг вернул бы удалённые пользователем). Если нет ни
    одного — ставим весь набор. Возвращает число установленных.
    """
    try:
        existing = await svc.list(user_id=user_id, status=None)
    except Exception as e:
        logger.warning(f"preinstalled skills: list failed for {user_id}: {e}")
        return 0
    if _installed_keys(existing):
        return 0  # уже сидили — уважаем удаления пользователя
    # не дублируем пользовательские скиллы с тем же именем
    live_names = {getattr(a, "name", "") for a in existing
                  if getattr(a, "is_skill", False)}
    installed = 0
    for tpl in PREINSTALLED_SKILLS:
        if tpl["name"] in live_names:
            continue
        payload = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": tpl["name"],
            "description": f"{tpl['description']} {_mark(tpl['key'])}",
            "instruction": tpl["instruction_template"],
            "schedule_type": "once",
            "trigger_type": "schedule",
            "status": "active",
            "is_skill": True,
            "instruction_template": tpl["instruction_template"],
            "parameters_schema": tpl["parameters_schema"],
            "shared_in_tenant": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await svc.create(payload)
            installed += 1
        except Exception as e:
            logger.warning(f"preinstalled skill '{tpl['key']}' install failed: {e}")
    if installed:
        logger.info(f"preinstalled skills: installed {installed} for {user_id}")
    return installed
