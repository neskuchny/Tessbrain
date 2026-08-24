# -*- coding: utf-8 -*-
"""Формат «Карта-организм компании»: компания как живое «тело».

Отделы — органы; цвет = состояние здоровья (растёт / норма / напряжение / болит);
между органами — потоки (деньги/информация/напряжение). Пульс недели одним
взглядом: где сверкает, где болит. Рисует image-модель по плотному промпту ИЛИ
код (Pillow) — детерминированно, идеальная кириллица. См. docs/VISUAL_REPORTS.md.
"""
from __future__ import annotations

import json
import logging
import math
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
from backend.core.board.viz_lang import all_text_note, is_ru, lang_line, pick  # noqa: F401

logger = logging.getLogger(__name__)

# Состояние органа → цвет (семантика здоровья, НЕ вариативная палитра).
_STATE_COLOR = {
    "growth": (46, 125, 50),    # растёт — зелёный
    "healthy": (0, 131, 143),   # норма — бирюзовый
    "tension": (245, 124, 0),   # напряжение — янтарный
    "pain": (198, 40, 40),      # болит — красный
}
_STATE_RU = {"growth": "растёт", "healthy": "норма",
             "tension": "напряжение", "pain": "болит"}
_STATE_EN = {"growth": "growing", "healthy": "healthy",
             "tension": "strained", "pain": "hurting"}


def _state_label(st: str, lang: str) -> str:
    return _STATE_EN[st] if not is_ru(lang) else _STATE_RU[st]


_STATE_ALIASES = {
    "рост": "growth", "растет": "growth", "растёт": "growth", "up": "growth",
    "ок": "healthy", "ok": "healthy", "норма": "healthy", "стабильно": "healthy",
    "напряжение": "tension", "риск": "tension", "warning": "tension", "warn": "tension",
    "боль": "pain", "болит": "pain", "проблема": "pain", "critical": "pain", "down": "pain",
}


def _norm_state(s: Any) -> str:
    v = str(s or "").strip().lower()
    if v in _STATE_COLOR:
        return v
    return _STATE_ALIASES.get(v, "healthy")


_EXTRACT_PROMPT = """Ты оформляешь «КАРТУ-ОРГАНИЗМ КОМПАНИИ» по материалу ниже. Разложи в СТРОГИЙ JSON:
{{
  "title": "заголовок, напр. «Пульс компании — неделя»",
  "subtitle": "одна строка контекста или пусто",
  "organs": [
    {{"name": "отдел/направление (1-3 слова)",
      "state": "growth|healthy|tension|pain",
      "note": "коротко что происходит",
      "metric": "цифра/срок ИЛИ пусто",
      "sensitive": false}}
  ],
  "flows": [{{"from": "орган", "to": "орган", "kind": "money|info|tension"}}],
  "threads": [{{"title": "значимый СКВОЗНОЙ сюжет, который тянется и может повториться в след. отчётах", "status": "new|continuing|escalating|improving|resolved"}}]
}}
Правила:
- 4-8 органов (ключевые отделы/направления). state — по смыслу: растёт / норма /
  напряжение / болит.
- metric — ТОЛЬКО если реально прозвучала цифра/сумма/срок/процент. Не выдумывай.
- "sensitive": true — если заметка про людей чувствительная (выгорание, конфликт,
  риск ухода): в командной версии её скроют, а «боль» смягчат до «напряжения».
- flows — 0-4 значимые связи между органами (опционально).
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


async def extract_organism_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    """LLM раскладывает материал в карту-организм. None при провале."""
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("organism: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("organism extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    if not data or not data.get("organs"):
        return None
    return data


def build_organism_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    """Плотный промпт для image-модели: компания как живой организм/тело."""
    v = _variety(seed)
    title = str(data.get("title") or pick(lang, "Пульс компании", "Company pulse")).strip()
    organs = [o for o in (data.get("organs") or []) if isinstance(o, dict)][:8]
    lines: List[str] = []
    for o in organs:
        st = _norm_state(o.get("state"))
        nm = str(o.get("name") or "").strip()
        note = str(o.get("note") or "").strip()
        met = str(o.get("metric") or "").strip()
        lines.append(f"  «{nm}» — {_state_label(st, lang)}: {note}" + (f" [{met}]" if met else ""))
    flows = [f for f in (data.get("flows") or []) if isinstance(f, dict)][:4]
    flow_note = ""
    if flows:
        fl = "; ".join(f"{f.get('from')}→{f.get('to')} ({f.get('kind')})" for f in flows)
        flow_note = f"Потоки между органами (нарисуй стрелками-артериями): {fl}.\n"
    return (
        f"Создай МЕТАФОРИЧНУЮ инфографику «компания как живой организм» на фоне «{v['texture']}». "
        "В центре — пульсирующее «сердце-ядро» компании. Вокруг — органы-отделы; "
        "здоровые/растущие СВЕТЯТСЯ зелёным, в норме — спокойный бирюзовый, "
        "напряжённые ПУЛЬСИРУЮТ янтарным, больные — тревожно-красные с трещинами. "
        "Между органами — «артерии» с потоками энергии/денег.\n"
        f"ЗАГОЛОВОК сверху: «{title}».\n"
        f"Органы (название — ДОСЛОВНО, заметку сократи до 2-4 ключевых слов, "
        f"цвет — по состоянию):\n"
        + "\n".join(lines) + "\n"
        + flow_note
        + "РАЗМЕР = СЕРЬЁЗНОСТЬ: больной орган — ЗАМЕТНО крупнее и ближе, "
        "взгляд падает на него первым; органы в норме — обычного размера, "
        "спокойные. Если ВСЕ органы в норме — тело ровно и умиротворённо "
        "светится, никаких трещин и пульсаций, это хорошая новость. "
        "Рисуй ТОЛЬКО органы и потоки из списка, ничего не выдумывай. "
        + f"{v['accent']}. "
        + all_text_note(lang) + " "
        "Метафорично, эмоционально, аналитично; горизонтальная композиция 16:10."
    )


def render_organism_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    """Детерминированный рендер карты-организма (Pillow). Идеальная кириллица.

    Ядро в центре, органы по кольцу, цвет — по состоянию здоровья; линии-артерии
    к ядру; лёгкие вариации фона от seed. Внизу — легенда состояний."""
    from PIL import Image, ImageDraw

    v = _variety(seed)
    bg = v["bg"]
    corner = v["corner"]

    title = str(data.get("title") or pick(lang, "Пульс компании", "Company pulse")).strip()
    subtitle = str(data.get("subtitle") or "").strip()
    organs = [o for o in (data.get("organs") or []) if isinstance(o, dict)][:8]

    W, H = 1680, 1120
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    f_title = _load_font(True, 34)
    f_head = _load_font(True, 24)
    f_note = _load_font(False, 20)
    f_metric = _load_font(True, 19)
    f_leg = _load_font(True, 20)
    f_foot = _load_font(False, 18)

    # Заголовок
    for ln_i, ln in enumerate(_wrap(draw, title.upper(), f_title, W - 200)[:2]):
        tw = draw.textlength(ln, font=f_title)
        draw.text(((W - tw) / 2, 30 + ln_i * 42), ln, font=f_title, fill=_INK)

    cx, cy = W // 2, H // 2 + 10
    core_r = 92

    # Органы по кольцу (артерии рисуем первыми — ядро ляжет поверх них)
    n = len(organs) or 1
    R = 400
    cw, chh = 300, 108
    start = -math.pi / 2  # сверху
    for i, o in enumerate(organs):
        ang = start + 2 * math.pi * i / n
        ox = cx + int(R * math.cos(ang))
        oy = cy + int(R * math.sin(ang))
        st = _norm_state(o.get("state"))
        color = _STATE_COLOR[st]
        box = [ox - cw // 2, oy - chh // 2, ox + cw // 2, oy + chh // 2]
        # артерия к ядру (толще если растёт/болит — сильный сигнал)
        w = 9 if st in ("growth", "pain") else 6
        draw.line([(cx, cy), (ox, oy)], fill=color, width=w, joint="curve")
        _rounded(draw, box, corner, fill=(255, 255, 255), outline=color, width=4)
        # цветная шапка-состояние
        _rounded(draw, [box[0], box[1], box[2], box[1] + 30], corner, fill=color)
        nm = str(o.get("name") or "").strip() or "—"
        draw.text((box[0] + 14, box[1] + 4), nm[:26], font=f_head, fill=(255, 255, 255))
        # заметка
        note = str(o.get("note") or "").strip()
        for k, ln in enumerate(_wrap(draw, note, f_note, cw - 28)[:2]):
            draw.text((box[0] + 14, box[1] + 38 + k * 24), ln, font=f_note, fill=_INK)
        met = str(o.get("metric") or "").strip()
        if met:
            mw = draw.textlength(met, font=f_metric) + 18
            _rounded(draw, [box[2] - mw - 8, box[3] - 30, box[2] - 8, box[3] - 4], 8, fill=color)
            draw.text((box[2] - mw + 1, box[3] - 27), met, font=f_metric, fill=(255, 255, 255))

    # Ядро-«сердце» поверх артерий
    draw.ellipse([cx - core_r - 8, cy - core_r - 4, cx + core_r + 8, cy + core_r + 12],
                 fill=(225, 219, 206))  # тень
    draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r],
                 fill=(255, 255, 255), outline=(198, 40, 40), width=5)
    draw.text((cx - draw.textlength("❤", font=f_title) / 2, cy - 48), "❤", font=f_title, fill=(198, 40, 40))
    core_lbl = pick(lang, "КОМПАНИЯ", "COMPANY")
    draw.text((cx - draw.textlength(core_lbl, font=f_head) / 2, cy + 8), core_lbl, font=f_head, fill=_INK)

    if subtitle:
        sw = draw.textlength(subtitle, font=f_note)
        draw.text(((W - sw) / 2, cy + core_r + 14), subtitle[:110], font=f_note, fill=_MUTED)

    # Легенда состояний (внизу слева)
    lx, ly = 40, H - 42
    for st in ("growth", "healthy", "tension", "pain"):
        draw.ellipse([lx, ly, lx + 18, ly + 18], fill=_STATE_COLOR[st])
        lbl = _state_label(st, lang)
        draw.text((lx + 24, ly - 2), lbl, font=f_leg, fill=_MUTED)
        lx += 30 + int(draw.textlength(lbl, font=f_leg)) + 26

    foot = pick(lang, "Tessbrain · карта-организм", "Tessbrain · organism map")
    draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 30), foot, font=f_foot, fill=_MUTED)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_organism_facts(data: Dict[str, Any], *, limit: int = 1024, lang: str = "ru") -> str:
    """Текст-дубль: состояние органов + потоки (на случай ошибок модели)."""
    title = str(data.get("title") or pick(lang, "Пульс компании", "Company pulse")).strip()
    parts: List[str] = [f"❤ {title}"]
    sub = str(data.get("subtitle") or "").strip()
    if sub:
        parts.append(sub)
    parts.append("")
    icon = {"growth": "🟢", "healthy": "🔵", "tension": "🟡", "pain": "🔴"}
    for o in [o for o in (data.get("organs") or []) if isinstance(o, dict)][:8]:
        st = _norm_state(o.get("state"))
        nm = str(o.get("name") or "").strip()
        if not nm:
            continue
        note = str(o.get("note") or "").strip()
        met = str(o.get("metric") or "").strip()
        line = f"{icon[st]} {nm} — {_state_label(st, lang)}"
        if note:
            line += f": {note}"
        if met:
            line += f" ({met})"
        parts.append(line)
    text = "\n".join(parts).strip()
    return text[:limit]


__all__ = [
    "extract_organism_data",
    "build_organism_prompt",
    "render_organism_png",
    "build_organism_facts",
]
