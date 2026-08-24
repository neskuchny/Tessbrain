# -*- coding: utf-8 -*-
"""
Web-only исполнители — отдельный трек «вайп-таскинга».

Часть инструментов (Lovable, v0, Bolt, Replit, а также веб-версии Claude и
ChatGPT/Codex) НЕ имеют headless-API — их нельзя дёрнуть контрактом
submit/poll, как CLI/OpenHands-бэкенды. Честный мост: Tessbrain готовит ТЗ на
контексте компании, а сюда отдаёт **готовую ссылку-запуск** (deep-link с
предзаполненным промптом, где инструмент это поддерживает) + полный бриф для
вставки. Человек открывает по одному клику, инструмент делает работу, результат
(URL) человек фиксирует обратно (web-result).

Это сознательно НЕ автономное исполнение: у web-only инструментов своя
авторизация/сессия в браузере пользователя. Мы убираем ручной труд подготовки
(ТЗ + куда идти), но запуск остаётся за человеком — это и есть «отдельный ход».

`prompt_param` задан только там, где предзаполнение промпта через query-параметр
известно и стабильно (claude.ai/new?q=, v0.dev/chat?q=, chatgpt.com/?q=). Для
остальных — открываем инструмент и даём бриф для вставки (prefilled=False).
Полный бриф отдаётся ВСЕГДА — deep-link это удобство, а не гарантия.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

# Безопасный предел длины URL (кладём предзаполнение только если влезает).
_MAX_URL = 2000
# Сколько символов ТЗ класть в kickoff-промпт deep-link (URL ограничен).
_KICKOFF_CHARS = 1100


@dataclass(frozen=True)
class WebTarget:
    key: str
    label: str
    base_url: str
    kind: str                      # design | code | general
    note: str = ""
    prompt_param: Optional[str] = None   # query-параметр предзаполнения промпта


# Реестр web-only целей. kind помогает роутингу «тип задачи → инструмент».
WEB_TARGETS: dict[str, WebTarget] = {
    "lovable": WebTarget(
        "lovable", "Lovable", "https://lovable.dev/", "design",
        note="Лендинги/веб-приложения по описанию. Deep-link промпта нет — открыть и вставить бриф."),
    "v0": WebTarget(
        "v0", "v0 (Vercel)", "https://v0.dev/chat", "design",
        prompt_param="q",
        note="UI/лендинг-генерация. Промпт предзаполняется через ?q=."),
    "bolt": WebTarget(
        "bolt", "Bolt.new", "https://bolt.new/", "code",
        note="Фуллстек-прототип в браузере. Открыть и вставить бриф."),
    "replit": WebTarget(
        "replit", "Replit Agent", "https://replit.com/", "code",
        note="Агент-разработчик в браузере. Открыть и вставить бриф."),
    "claude": WebTarget(
        "claude", "Claude (веб)", "https://claude.ai/new", "general",
        prompt_param="q",
        note="Веб-Claude. Промпт предзаполняется через ?q= (усечённый kickoff)."),
    "chatgpt": WebTarget(
        "chatgpt", "ChatGPT / Codex (веб)", "https://chatgpt.com/", "general",
        prompt_param="q",
        note="Веб-ChatGPT/Codex. Промпт предзаполняется через ?q= (усечённый kickoff)."),
}

# Дефолтный выбор инструмента по типу задачи (мягкий, переопределяется явным
# web_target). Лендинг → Lovable, код → Bolt, прочее → Claude.
_TYPE_DEFAULT = {
    "landing": "lovable",
    "design": "lovable",
    "finmodel": "chatgpt",
    "report": "claude",
    "analysis": "claude",
    "code": "bolt",
    "api": "bolt",
}


def list_targets() -> list[dict]:
    """Реестр для UI (без объектов)."""
    return [
        {"key": t.key, "label": t.label, "kind": t.kind,
         "prefill": bool(t.prompt_param), "note": t.note}
        for t in WEB_TARGETS.values()
    ]


def resolve_target_key(web_target: Optional[str], task_type: Optional[str] = None) -> str:
    """Выбрать ключ цели: явный web_target → дефолт по типу → 'lovable'."""
    if web_target and web_target.strip().lower() in WEB_TARGETS:
        return web_target.strip().lower()
    tt = (task_type or "").strip().lower()
    if tt in _TYPE_DEFAULT:
        return _TYPE_DEFAULT[tt]
    return "lovable"


def _kickoff_prompt(task_title: str, tz_markdown: str) -> str:
    """Короткий kickoff для deep-link (полное ТЗ идёт бинарём брифа)."""
    head = (tz_markdown or "").strip()
    if len(head) > _KICKOFF_CHARS:
        head = head[:_KICKOFF_CHARS].rsplit("\n", 1)[0] + "\n… (полное ТЗ — в брифе ниже)"
    title = (task_title or "Задача").strip()
    return f"Задача: {title}\n\n{head}"


def build_launch(web_target: Optional[str], *, task_title: str,
                 tz_markdown: str, task_type: Optional[str] = None) -> dict:
    """Собрать запуск в web-only инструмент.

    Возвращает {tool, label, kind, url, prefilled, brief, note}. `url` —
    ссылка (с предзаполнением, если инструмент поддерживает и влезает по длине),
    `brief` — полный ТЗ для вставки (гарантия), `prefilled` — удалось ли
    вшить промпт в URL.
    """
    key = resolve_target_key(web_target, task_type)
    t = WEB_TARGETS[key]
    url = t.base_url
    prefilled = False
    if t.prompt_param:
        kickoff = _kickoff_prompt(task_title, tz_markdown)
        candidate = f"{t.base_url}?{t.prompt_param}={quote(kickoff)}"
        if len(candidate) <= _MAX_URL:
            url = candidate
            prefilled = True
        else:
            # усечём kickoff до заголовка, чтобы хоть что-то предзаполнить
            short = quote(f"Задача: {(task_title or 'Задача').strip()[:200]}")
            candidate = f"{t.base_url}?{t.prompt_param}={short}"
            if len(candidate) <= _MAX_URL:
                url = candidate
                prefilled = True
    return {
        "tool": t.key, "label": t.label, "kind": t.kind,
        "url": url, "prefilled": prefilled,
        "brief": tz_markdown, "note": t.note,
    }


__all__ = ["WEB_TARGETS", "WebTarget", "build_launch", "list_targets",
           "resolve_target_key"]
