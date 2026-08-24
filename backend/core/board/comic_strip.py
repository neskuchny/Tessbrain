# -*- coding: utf-8 -*-
"""Формат «Комикс»: неделя/встреча как полоса кадров.

Показывает КАК прошло — сюжет, динамику, эмоции, кто как себя проявил: то, что
в сухом отчёте не напишешь («Иван выключился», «команда выдохнула»), а на лицах
персонажей — можно. Рисует image-модель по плотному промпту (единый стиль, речевые
пузыри, выражения); Pillow-фолбэк даёт читаемый сториборд из подписей и реплик.
См. docs/VISUAL_REPORTS.md.
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
from backend.core.board.viz_lang import all_text_note, is_ru, lang_line, pick

logger = logging.getLogger(__name__)

# Настроение кадра → цвет шапки (семантика тона сцены).
_MOOD_COLOR = {
    "up": (46, 125, 50), "calm": (0, 131, 143),
    "tense": (245, 124, 0), "down": (198, 40, 40), "neutral": (90, 98, 112),
}
_MOOD_ALIASES = {
    "рост": "up", "радость": "up", "победа": "up", "успех": "up", "good": "up",
    "спокойно": "calm", "норма": "calm", "ok": "calm",
    "напряжение": "tense", "спор": "tense", "тревога": "tense", "warn": "tense",
    "спад": "down", "провал": "down", "проблема": "down", "грусть": "down",
}


def _norm_mood(s: Any) -> str:
    v = str(s or "").strip().lower()
    if v in _MOOD_COLOR:
        return v
    return _MOOD_ALIASES.get(v, "neutral")


_EXTRACT_PROMPT = """Ты раскадровываешь материал ниже в КОМИКС (полоса кадров). Разложи в СТРОГИЙ JSON:
{{
  "title": "заголовок комикса, напр. «Неделя в кадрах»",
  "panels": [
    {{"caption": "подпись сцены (что происходит), 3-7 слов",
      "speaker": "кто говорит/действует ИЛИ пусто",
      "line": "короткая реплика/мысль ИЛИ пусто",
      "mood": "up|calm|tense|down|neutral",
      "sensitive": false}}
  ],
  "threads": [{{"title": "значимый СКВОЗНОЙ сюжет, который тянется и может повториться в след. отчётах", "status": "new|continuing|escalating|improving|resolved"}}]
}}
Правила:
- 3-6 кадров, связная история от начала к итогу недели/встречи.
- mood — тон сцены. Эмоции передавай через реплики/подписи, деликатно и по делу.
- "sensitive": true — на кадры с личным/острым про конкретных людей (конфликт,
  недовольство, риск ухода): такой кадр скроют в командной версии.
- Только реальные события; не выдумывай фактов и цифр.
- По-русски, кратко. threads — 0-3 сквозных сюжета (риск/инициатива/проблема); нет таких — пустой массив, не выдумывай. Верни ТОЛЬКО JSON.

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


async def extract_comic_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    """LLM раскладывает материал в кадры комикса. None при провале."""
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("comic: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("comic extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    if not data or not data.get("panels"):
        return None
    return data


def _comic_style_desc(data: Dict[str, Any], v: Dict[str, Any], lang: str) -> str:
    """Стиль комикса: выбранный пользователем под-стиль (comic_manga/superhero/
    noir/newspaper) из пресетов ИЛИ дефолтная вариативность по сиду."""
    key = str(data.get("_style") or "").strip().lower()
    if key.startswith("comic_"):
        try:
            from backend.core.board.meeting_infographic import _STYLE_PRESETS
            preset = _STYLE_PRESETS.get(key)
            if preset:
                return preset[0 if is_ru(lang) else 1]
        except Exception:
            pass
    return v["style"]


def build_comic_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    """Плотный промпт для image-модели: комикс-полоса из кадров, единый стиль."""
    v = _variety(seed)
    style = _comic_style_desc(data, v, lang)
    title = str(data.get("title") or pick(lang, "Комикс недели", "Week in frames")).strip()
    panels = [p for p in (data.get("panels") or []) if isinstance(p, dict)][:6]
    lines: List[str] = []
    for i, p in enumerate(panels, 1):
        cap = str(p.get("caption") or "").strip()
        sp = str(p.get("speaker") or "").strip()
        ln = str(p.get("line") or "").strip()
        bubble = f' Речевой пузырь{(" " + sp) if sp else ""}: «{ln}».' if ln else ""
        lines.append(f"  Кадр {i}: {cap}.{bubble} Тон — {_norm_mood(p.get('mood'))}.")
    return (
        f"Нарисуй КОМИКС-полосу из {len(panels)} кадров в ЕДИНОМ рисованном стиле "
        f"({style}), кадры с рамками и нумерацией, речевые пузыри. "
        "Персонажи-сотрудники с ВЫРАЗИТЕЛЬНЫМИ лицами — эмоции читаются по мимике "
        "(радость, напряжение, усталость, облегчение). ЛИЦО В КАЖДОМ КАДРЕ = ТОН "
        "этого кадра: мимика и поза персонажей точно передают указанный «Тон» — "
        "это главный канал кадра, важнее слов. Сюжет — история недели по кадрам; "
        "ПОСЛЕДНИЙ кадр задаёт итоговое чувство всей полосы.\n"
        f"ЗАГОЛОВОК сверху: «{title}».\n"
        "Кадры (подписи и реплики ДОСЛОВНО; в пузыре максимум 6 слов — длиннее "
        "сократи без потери смысла):\n"
        + "\n".join(lines) + "\n"
        + f"{v['accent']}. "
        + all_text_note(lang) + " "
        "Каждый кадр уникален; тёплый живой стиль, как хороший веб-комикс; композиция 16:10."
    )


def render_comic_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    """Детерминированный сториборд-фолбэк (Pillow): сетка кадров с подписями и
    репликами. Не рисует персонажей, но даёт читаемую раскадровку (идеальный текст)."""
    from PIL import Image, ImageDraw

    v = _variety(seed)
    bg = v["bg"]
    corner = v["corner"]
    title = str(data.get("title") or pick(lang, "Комикс недели", "Week in frames")).strip()
    panels = [p for p in (data.get("panels") or []) if isinstance(p, dict)][:6]
    n = len(panels) or 1

    W, H = 1680, 1120
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    f_title = _load_font(True, 34)
    f_num = _load_font(True, 22)
    f_cap = _load_font(True, 24)
    f_line = _load_font(False, 22)
    f_sp = _load_font(True, 18)
    f_foot = _load_font(False, 18)

    for ln_i, ln in enumerate(_wrap(draw, title.upper(), f_title, W - 200)[:2]):
        tw = draw.textlength(ln, font=f_title)
        draw.text(((W - tw) / 2, 26 + ln_i * 42), ln, font=f_title, fill=_INK)

    cols = 3 if n >= 5 else 2
    rows = (n + cols - 1) // cols
    pad, top = 40, 110
    gap = 24
    pw = (W - 2 * pad - (cols - 1) * gap) // cols
    ph = (H - top - pad - (rows - 1) * gap) // rows

    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        x = pad + c * (pw + gap)
        y = top + r * (ph + gap)
        mood = _norm_mood(p.get("mood"))
        color = _MOOD_COLOR[mood]
        # рамка кадра
        _rounded(draw, [x, y, x + pw, y + ph], corner, fill=(255, 255, 255), outline=color, width=4)
        # номер кадра
        draw.ellipse([x + 12, y + 12, x + 46, y + 46], fill=color)
        num = str(i + 1)
        draw.text((x + 29 - draw.textlength(num, font=f_num) / 2, y + 16), num, font=f_num, fill=(255, 255, 255))
        # подпись сцены
        cap = str(p.get("caption") or "").strip()
        cy = y + 58
        for ln in _wrap(draw, cap, f_cap, pw - 70)[:2]:
            draw.text((x + 58, cy), ln, font=f_cap, fill=_INK)
            cy += 30
        # речевой пузырь снизу
        sp = str(p.get("speaker") or "").strip()
        line = str(p.get("line") or "").strip()
        if line:
            bx0, by0, bx1, by1 = x + 18, y + ph - 150, x + pw - 18, y + ph - 18
            _rounded(draw, [bx0, by0, bx1, by1], 16, fill=(245, 245, 240), outline=color, width=2)
            draw.polygon([(bx0 + 34, by0), (bx0 + 20, by0 - 16), (bx0 + 58, by0)], fill=(245, 245, 240))
            ty = by0 + 12
            if sp:
                draw.text((bx0 + 16, ty), sp[:24] + ":", font=f_sp, fill=color)
                ty += 24
            for ln in _wrap(draw, "«" + line + "»", f_line, (bx1 - bx0) - 28)[:3]:
                draw.text((bx0 + 16, ty), ln, font=f_line, fill=_INK)
                ty += 26

    foot = pick(lang, "Tessbrain · комикс (раскадровка)", "Tessbrain · comic (storyboard)")
    draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 30), foot, font=f_foot, fill=_MUTED)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_comic_facts(data: Dict[str, Any], *, limit: int = 1024, lang: str = "ru") -> str:
    """Текст-дубль: сюжет по кадрам (на случай ошибок модели в картинке)."""
    title = str(data.get("title") or pick(lang, "Комикс недели", "Week in frames")).strip()
    parts: List[str] = [f"🗯 {title}", ""]
    for i, p in enumerate([p for p in (data.get("panels") or []) if isinstance(p, dict)][:6], 1):
        cap = str(p.get("caption") or "").strip()
        sp = str(p.get("speaker") or "").strip()
        line = str(p.get("line") or "").strip()
        row = f"{i}. {cap}"
        if line:
            row += f" — {sp + ': ' if sp else ''}«{line}»"
        parts.append(row)
    return "\n".join(parts).strip()[:limit]


__all__ = [
    "build_comic_facts",
    "build_comic_prompt",
    "extract_comic_data",
    "render_comic_png",
]
