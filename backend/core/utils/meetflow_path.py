# -*- coding: utf-8 -*-
"""Единая точка правды для пути к пакету MeetFlow-агентов.

История бага: дефолт «E:/synlabs/tessent/alfa_asynk_meetflow» указывал на
СТАРУЮ папку (репозиторий переехал в tessent-test_new), а четыре inline-
«спасения» в agents.py считали путь на уровень короче и попадали в
несуществующий tessent_brain/alfa_asynk_meetflow. Итог: дефолтный режим
«Задачи» чата Auto, MeetFlow-тулзы Tess/hybrid/agent_oc и экшены фоновых
автоматизаций падали «No module named 'autogen_async'», если env не задан
и старой папки нет (аудит: docs/AUTO_CHATS_AUDIT.md, P0).

Порядок: env MEETFLOW_PATH → alfa_asynk_meetflow РЯДОМ с tessent_brain
(корень репозитория) → пусто, то есть «не настроено».

Почему в конце пусто, а не старый абсолютный путь. Путь с одной машины
работал ровно на ней, а везде ещё давал не «не настроено», а невнятную
ошибку импорта по несуществующему каталогу — тот самый баг, описанный
выше, просто в другой одежде. Пустая строка честна и безопасна:
ensure_meetflow_on_path её не кладёт в sys.path (пустая строка там
значит текущий каталог и тихо подменяет модули).
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)


def default_meetflow_path() -> str:
    env = os.getenv("MEETFLOW_PATH", "")
    if env:
        return env
    try:
        from pathlib import Path as _P
        # этот файл: tessent_brain/backend/core/utils/meetflow_path.py
        # parents[3] = tessent_brain → .parent = корень репозитория
        sibling = _P(__file__).resolve().parents[3].parent / "alfa_asynk_meetflow"
        if (sibling / "automation_tools_async.py").exists():
            return str(sibling)
    except Exception:
        logger.debug("meetflow sibling detection failed", exc_info=True)
    return ""


def ensure_meetflow_on_path() -> str:
    """Вернуть путь и гарантировать его в sys.path (для import
    automation_tools_async / autogen_async)."""
    p = default_meetflow_path()
    if p and p not in sys.path:
        sys.path.insert(0, p)
    return p
