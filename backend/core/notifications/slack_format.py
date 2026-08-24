# -*- coding: utf-8 -*-
"""Отправка ответа в Slack: разметка + доставка.

Закрывает половину интеграции, которая была честно записана в бизнес-карте
как разрыв: «Slack принимает сообщения, но ответ обратно не отправляет».
Входящая часть работала, ответ упирался в две вещи — токен бота и то, что
Slack понимает СВОЮ разметку, а не markdown.

Разметка. Ответ мозга приходит обычным markdown (## заголовки, **жирный**,
списки, таблицы). Slack этого не понимает: `**жирный**` он показывает
звёздочками как есть. Здесь — перевод в mrkdwn: `*жирный*`, `_курсив_`,
заголовок жирной строкой, ссылка в угловых скобках. Таблицы разворачиваем
в строки: в Slack моноширинный блок ломается на телефоне.

Доставка. `chat.postMessage` с токеном бота, длинный ответ режется на
сообщения (предел Slack 40 000 символов на блок, но читаемый предел
меньше — режем по 3000 как в остальных каналах). Never-raise: не смогли
отправить — вернули ok=False, вызывающий это увидит в метрике.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_API = "https://slack.com/api/chat.postMessage"


def _convert_links(text: str) -> str:
    """[подпись](адрес) → <адрес|подпись> — формат ссылок Slack."""
    return re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", text)


def _convert_line(line: str) -> str:
    stripped = line.strip()

    # Разделитель таблицы (|---|---|) выбрасываем целиком.
    if re.fullmatch(r"\|[\s:|-]+\|", stripped):
        return ""

    # Строка таблицы → «ячейка · ячейка»: в мессенджере это читается,
    # а моноширинная таблица на телефоне разъезжается.
    if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") > 2:
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        return " · ".join(c for c in cells if c)

    # Заголовок → жирная строка (уровней у Slack нет).
    m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if m:
        return f"*{m.group(2).strip()}*"

    # Маркер списка: Slack не рендерит markdown-списки, но • читается везде.
    line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
    return line


def markdown_to_mrkdwn(text: str) -> str:
    """Markdown → разметка Slack. Чистая функция, тестируется без сети."""
    if not text:
        return ""
    out: List[str] = []
    in_code = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_code = not in_code
            out.append(raw)
            continue
        if in_code:
            out.append(raw)          # код не трогаем
            continue
        out.append(_convert_line(raw))

    body = "\n".join(out)
    body = _convert_links(body)
    # Жирный: **текст** → *текст*. Делаем ПОСЛЕ ссылок, чтобы не съесть
    # звёздочки внутри подписей.
    body = re.sub(r"\*\*(.+?)\*\*", r"*\1*", body, flags=re.S)
    # Курсив markdown (__текст__) → _текст_.
    body = re.sub(r"__(.+?)__", r"_\1_", body, flags=re.S)
    # Схлопываем пустые строки, оставшиеся от выброшенных разделителей.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def split_chunks(text: str, limit: int = 3000) -> List[str]:
    """Разбить по абзацам, не разрывая строку посередине."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
        while len(current) > limit:          # одна строка длиннее предела
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


async def post_slack_text(bot_token: str, channel: str, markdown_text: str,
                          *, timeout: float = 15.0, chunk_limit: int = 3000,
                          max_chunks: int = 8,
                          thread_ts: str = "") -> Dict[str, Any]:
    """Отправить ответ в канал Slack. Never-raise.

    Возвращает {"ok", "parts", "error"}: ok=True, если доставлен хотя бы
    один кусок. `thread_ts` — ответить веткой на исходное сообщение, чтобы
    не засорять канал (Slack показывает ветку под вопросом).
    """
    res: Dict[str, Any] = {"ok": False, "parts": 0, "error": ""}
    if not (bot_token and channel and (markdown_text or "").strip()):
        res["error"] = "нет токена, канала или текста"
        return res
    try:
        import httpx
    except Exception:
        res["error"] = "httpx недоступен"
        return res

    chunks = split_chunks(markdown_to_mrkdwn(markdown_text), limit=chunk_limit)
    if len(chunks) > max_chunks:
        logger.warning("slack post: текст обрезан до %d сообщений из %d",
                       max_chunks, len(chunks))
        chunks = chunks[:max_chunks]

    headers = {"Authorization": f"Bearer {bot_token}",
               "Content-Type": "application/json; charset=utf-8"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for chunk in chunks:
                payload: Dict[str, Any] = {"channel": channel, "text": chunk}
                if thread_ts:
                    payload["thread_ts"] = thread_ts
                r = await client.post(_API, json=payload, headers=headers)
                data = r.json() if r.content else {}
                if not data.get("ok"):
                    # Slack отвечает 200 с ok=false и полем error — молчаливого
                    # успеха здесь быть не должно.
                    res["error"] = str(data.get("error") or f"HTTP {r.status_code}")
                    logger.warning("slack post failed: %s", res["error"])
                    break
                res["parts"] += 1
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("slack post failed: %s", res["error"])

    res["ok"] = res["parts"] > 0
    return res


__all__ = ["markdown_to_mrkdwn", "post_slack_text", "split_chunks"]
