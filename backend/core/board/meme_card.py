# -*- coding: utf-8 -*-
"""Формат «Мем»: одна мысль/настроение мгновенно.

Самый лёгкий носитель — картинка с крупной подписью сверху/снизу. Считывается за
долю секунды, задаёт тон. Рисует image-модель (impact-подпись) ИЛИ код (Pillow —
классическая мем-раскладка с крупным текстом). См. docs/VISUAL_REPORTS.md.
"""
from __future__ import annotations

import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

from backend.core.board.meeting_infographic import (
    _MUTED,
    _load_font,
    _variety,
    _wrap,
)
from backend.core.board.viz_lang import all_text_note, lang_line, pick  # noqa: F401

logger = logging.getLogger(__name__)

_MOOD_COLOR = {
    "up": (46, 125, 50), "tense": (245, 124, 0),
    "down": (198, 40, 40), "neutral": (33, 41, 54),
}
_MOOD_ALIASES = {"рост": "up", "победа": "up", "радость": "up", "good": "up",
                 "напряжение": "tense", "warn": "tense",
                 "спад": "down", "провал": "down", "проблема": "down"}


def _norm_mood(s: Any) -> str:
    v = str(s or "").strip().lower()
    return v if v in _MOOD_COLOR else _MOOD_ALIASES.get(v, "neutral")


_EXTRACT_PROMPT = """Сделай МЕМ по материалу ниже. Верни СТРОГИЙ JSON:
{{
  "top": "верхняя строка мема (кратко, ударно)",
  "bottom": "нижняя строка мема (панчлайн/вывод)",
  "scene": "что на картинке (образ/ситуация) — 3-8 слов",
  "mood": "up|tense|down|neutral"
}}
Правила: коротко и по делу, без грубости; отражай реальную суть (итог недели/
встречи), не выдумывай фактов. Юмор — ТОЛЬКО о ситуациях и процессах, НИКОГДА
над конкретными людьми: не высмеивай, не унижай, не делай человека панчлайном.
Если период ровный и повода для шутки нет — mood=neutral и спокойная тёплая
констатация вместо форсированной шутки. По-русски. Верни ТОЛЬКО JSON.

МАТЕРИАЛ:
{src}
"""


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def extract_meme_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("meme: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("meme extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    if not data or not (data.get("top") or data.get("bottom")):
        return None
    return data


def build_meme_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    v = _variety(seed)
    top = str(data.get("top") or "").strip()
    bottom = str(data.get("bottom") or "").strip()
    scene = str(data.get("scene") or "").strip()
    return (
        f"Создай МЕМ-картинку ({v['style']}, акцент: {v['accent']}): выразительный "
        f"образ «{scene}» и КРУПНАЯ мем-подпись жирным контурным шрифтом "
        f"(impact-стиль) — ВЕРХНЯЯ строка «{top}» и НИЖНЯЯ строка «{bottom}». "
        "Текст ДОСЛОВНО как задан, крупно, по центру. " + all_text_note(lang)
        + " Юмор уместен, только если сам материал даёт повод — иначе тёплая "
        "спокойная подача; шутка НИКОГДА не направлена на конкретного человека. "
        "Квадрат 1:1."
    )


def render_meme_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    from PIL import Image, ImageDraw

    v = _variety(seed)
    bg = v["bg"]
    top = str(data.get("top") or "").strip().upper()
    bottom = str(data.get("bottom") or "").strip().upper()
    scene = str(data.get("scene") or "").strip()
    color = _MOOD_COLOR[_norm_mood(data.get("mood"))]

    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    f_big = _load_font(True, 62)
    f_scene = _load_font(False, 30)
    f_foot = _load_font(False, 20)

    def _outlined(cx: float, y: float, text: str):
        # мем-текст: белая заливка с тёмным контуром
        for dx in (-3, 0, 3):
            for dy in (-3, 0, 3):
                draw.text((cx + dx, y + dy), text, font=f_big, fill=(20, 20, 20),
                          anchor="ma")
        draw.text((cx, y), text, font=f_big, fill=(255, 255, 255), anchor="ma")

    # Центральная «сцена»-плашка (описание образа — без реальной картинки)
    draw.rounded_rectangle([70, 250, W - 70, H - 250], radius=28,
                           fill=(255, 255, 255), outline=color, width=6)
    sc_lines = _wrap(draw, scene, f_scene, W - 220)[:3]
    sy = (H - len(sc_lines) * 38) / 2
    for ln in sc_lines:
        draw.text((W / 2, sy), ln, font=f_scene, fill=_MUTED, anchor="ma")
        sy += 38

    for i, ln in enumerate(_wrap(draw, top, f_big, W - 100)[:2]):
        _outlined(W / 2, 40 + i * 66, ln)
    b_lines = _wrap(draw, bottom, f_big, W - 100)[:2]
    for i, ln in enumerate(b_lines):
        _outlined(W / 2, H - 150 - (len(b_lines) - 1 - i) * 66, ln)

    draw.text((W / 2, H - 34), pick(lang, "Tessbrain · мем", "Tessbrain · meme"),
              font=f_foot, fill=_MUTED, anchor="ma")
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_meme_facts(data: Dict[str, Any], *, limit: int = 400, lang: str = "ru") -> str:
    top = str(data.get("top") or "").strip()
    bottom = str(data.get("bottom") or "").strip()
    parts: List[str] = ["😄 " + top] if top else []
    if bottom:
        parts.append(bottom)
    return "\n".join(parts).strip()[:limit] or "мем"


__all__ = ["extract_meme_data", "build_meme_prompt", "render_meme_png", "build_meme_facts"]
