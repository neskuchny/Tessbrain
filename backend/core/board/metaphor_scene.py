# -*- coding: utf-8 -*-
"""Формат «Метафора-сцена»: абстрактная ситуация одним образом.

Для стратегии/рисков/отношений: мост, шторм, крепость, гора, перекрёсток. Образ
передаёт положение дел целиком. Рисует image-модель (живописная сцена) ИЛИ код
(Pillow — карточка «сцена + что символизирует + смысл»). См. docs/VISUAL_REPORTS.md.
"""
from __future__ import annotations

import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

from backend.core.board.meeting_infographic import (
    _INK,
    _MUTED,
    _load_font,
    _rounded,
    _variety,
    _wrap,
)
from backend.core.board.viz_lang import all_text_note, lang_line, pick  # noqa: F401

logger = logging.getLogger(__name__)

_MOOD_COLOR = {"up": (46, 125, 50), "calm": (0, 131, 143),
               "tense": (245, 124, 0), "down": (198, 40, 40), "neutral": (90, 98, 112)}
_MOOD_ALIASES = {"рост": "up", "спокойно": "calm", "норма": "calm",
                 "напряжение": "tense", "риск": "tense", "спад": "down", "проблема": "down"}


def _norm_mood(s: Any) -> str:
    v = str(s or "").strip().lower()
    return v if v in _MOOD_COLOR else _MOOD_ALIASES.get(v, "neutral")


_EXTRACT_PROMPT = """Подбери МЕТАФОРУ-СЦЕНУ для ситуации из материала ниже. Верни СТРОГИЙ JSON:
{{
  "title": "заголовок",
  "metaphor": "образ-сцена одной фразой (напр. «корабль в шторм у гавани»)",
  "elements": [{{"symbol": "элемент образа", "means": "что означает в реальности", "mood": "up|calm|tense|down"}}],
  "meaning": "главный смысл/вывод одной фразой",
  "threads": [{{"title": "значимый СКВОЗНОЙ сюжет, который тянется и может повториться в след. отчётах", "status": "new|continuing|escalating|improving|resolved"}}]
}}
Правила: 2-4 элемента. Метафора должна честно отражать суть (риск/рост/затор),
не приукрашивать. По-русски. threads — 0-3 сквозных сюжета (риск/инициатива/проблема); нет таких — пустой массив, не выдумывай. Верни ТОЛЬКО JSON.

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


async def extract_metaphor_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("metaphor: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("metaphor extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    if not data or not data.get("metaphor"):
        return None
    return data


def build_metaphor_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    v = _variety(seed)
    title = str(data.get("title") or "").strip()
    metaphor = str(data.get("metaphor") or "").strip()
    elems = "; ".join(
        f"{e.get('symbol')} = {e.get('means')}"
        for e in (data.get("elements") or []) if isinstance(e, dict))
    meaning = str(data.get("meaning") or "").strip()
    return (
        f"Нарисуй ЖИВОПИСНУЮ метафору-сцену ({v['style']}): «{metaphor}». "
        f"Элементы сцены и их смысл: {elems}. Атмосфера передаёт положение дел "
        f"({v['accent']}). ИНТЕНСИВНОСТЬ = СЕРЬЁЗНОСТЬ: чем серьёзнее событие "
        "(mood=down/tense), тем КРУПНЕЕ, ближе и драматичнее его элемент в "
        "сцене; спокойные (calm) — фоном, мягко; растущие (up) — светятся и "
        "поднимаются. Взгляд должен сразу упасть на самый серьёзный элемент. "
        f"Небольшая подпись-заголовок «{title}» и вывод «{meaning}» "
        "крупно и ДОСЛОВНО. " + all_text_note(lang) + " Метафорично, эмоционально, "
        "кинематографично; горизонт 16:9."
    )


def render_metaphor_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    from PIL import Image, ImageDraw

    v = _variety(seed)
    bg = v["bg"]
    corner = v["corner"]
    title = str(data.get("title") or pick(lang, "Метафора", "Metaphor")).strip()
    metaphor = str(data.get("metaphor") or "").strip()
    elements = [e for e in (data.get("elements") or []) if isinstance(e, dict)][:4]
    meaning = str(data.get("meaning") or "").strip()

    W, H = 1680, 945
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    f_title = _load_font(True, 34)
    f_meta = _load_font(True, 30)
    f_el = _load_font(False, 24)
    f_sym = _load_font(True, 22)
    f_mean = _load_font(True, 26)
    f_foot = _load_font(False, 18)

    for ln_i, ln in enumerate(_wrap(draw, title.upper(), f_title, W - 200)[:1]):
        tw = draw.textlength(ln, font=f_title)
        draw.text(((W - tw) / 2, 28), ln, font=f_title, fill=_INK)

    # Образ-метафора крупной плашкой
    _rounded(draw, [50, 90, W - 50, 240], corner, fill=(255, 255, 255), outline=(106, 27, 154), width=4)
    for i, ln in enumerate(_wrap(draw, "« " + metaphor + " »", f_meta, W - 140)[:2]):
        draw.text((W / 2, 118 + i * 40), ln, font=f_meta, fill=_INK, anchor="ma")

    # Элементы образа
    y = 280
    for e in elements:
        color = _MOOD_COLOR[_norm_mood(e.get("mood"))]
        sym = str(e.get("symbol") or "").strip()
        means = str(e.get("means") or "").strip()
        draw.ellipse([60, y + 6, 78, y + 24], fill=color)
        draw.text((96, y), sym[:40], font=f_sym, fill=color)
        mx = 96 + draw.textlength(sym[:40], font=f_sym) + 20
        for k, ln in enumerate(_wrap(draw, "— " + means, f_el, W - mx - 80)[:2]):
            draw.text((mx, y + k * 26), ln, font=f_el, fill=_INK)
        y += 78

    # Вывод-смысл
    if meaning:
        _rounded(draw, [50, H - 150, W - 50, H - 60], corner, fill=(245, 240, 250), outline=(106, 27, 154), width=2)
        meaning_pref = pick(lang, "Смысл: ", "Meaning: ")
        for i, ln in enumerate(_wrap(draw, meaning_pref + meaning, f_mean, W - 140)[:2]):
            draw.text((80, H - 138 + i * 32), ln, font=f_mean, fill=_INK)

    foot = pick(lang, "Tessbrain · метафора", "Tessbrain · metaphor")
    draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 34),
              foot, font=f_foot, fill=_MUTED)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_metaphor_facts(data: Dict[str, Any], *, limit: int = 700, lang: str = "ru") -> str:
    title = str(data.get("title") or pick(lang, "Метафора", "Metaphor")).strip()
    metaphor = str(data.get("metaphor") or "").strip()
    parts: List[str] = [f"🎭 {title}", f"« {metaphor} »"]
    for e in [e for e in (data.get("elements") or []) if isinstance(e, dict)][:4]:
        sym = str(e.get("symbol") or "").strip()
        means = str(e.get("means") or "").strip()
        if sym:
            parts.append(f"  • {sym} — {means}")
    meaning = str(data.get("meaning") or "").strip()
    if meaning:
        parts.append(pick(lang, "Смысл: ", "Meaning: ") + meaning)
    return "\n".join(parts).strip()[:limit]


__all__ = ["extract_metaphor_data", "build_metaphor_prompt", "render_metaphor_png", "build_metaphor_facts"]
