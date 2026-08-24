# -*- coding: utf-8 -*-
"""Формат «Портрет-персонаж сотрудника» (RPG-отчёт).

Сотрудник как герой: что произошло за период, как «прокачался», где просел.
Метафора-карточка персонажа: аватар, уровень/титул, достижения, прокачка навыков,
настроение. Показывает эмоциональный слой, который в сухом отчёте не напишешь
(«на грани выгорания»), но который важен руководителю. Рисует image-модель по
плотному промпту ИЛИ код (Pillow). См. docs/VISUAL_REPORTS.md.
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

# Настроение/динамика → цвет (семантика, как у карты-организма).
_MOOD_COLOR = {
    "growth": (46, 125, 50), "steady": (0, 131, 143),
    "strain": (245, 124, 0), "down": (198, 40, 40),
}
_MOOD_RU = {"growth": "на подъёме", "steady": "стабильно",
            "strain": "напряжён", "down": "просел"}
_MOOD_EN = {"growth": "on the rise", "steady": "steady",
            "strain": "strained", "down": "slumping"}


def _mood_label(mood: str, lang: str) -> str:
    return _MOOD_EN[mood] if not is_ru(lang) else _MOOD_RU[mood]


_MOOD_ALIASES = {
    "рост": "growth", "подъем": "growth", "подъём": "growth", "up": "growth",
    "норма": "steady", "стабильно": "steady", "ok": "steady", "ок": "steady",
    "напряжение": "strain", "напряжён": "strain", "выгорание": "strain", "warn": "strain",
    "спад": "down", "просел": "down", "down": "down", "проблема": "down",
}


def _norm_mood(s: Any) -> str:
    v = str(s or "").strip().lower()
    if v in _MOOD_COLOR:
        return v
    return _MOOD_ALIASES.get(v, "steady")


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", str(name).strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


_EXTRACT_PROMPT = """Ты оформляешь «ПОРТРЕТЫ-ПЕРСОНАЖИ» участников (RPG-отчёт за период). В материале может быть НЕСКОЛЬКО человек — сделай карточку на КАЖДОГО, о ком есть рабочий сигнал. Разложи в СТРОГИЙ JSON:
{{
  "period": "период, напр. «Октябрь» ИЛИ пусто",
  "team_title": "короткое название команды/встречи ИЛИ пусто",
  "characters": [
    {{
      "name": "имя участника",
      "role": "роль/должность ИЛИ пусто",
      "level": "короткий титул/уровень по итогу, напр. «вырос до Senior» ИЛИ пусто",
      "mood": "growth|steady|strain|down",
      "highlights": [{{"text": "достижение/событие", "metric": "цифра/срок ИЛИ пусто", "sensitive": false}}],
      "skills": [{{"name": "навык/зона", "delta": "напр. +2, ↑, стабильно"}}],
      "attention": "что требует внимания руководителя ИЛИ пусто"
    }}
  ],
  "threads": [{{"title": "значимый СКВОЗНОЙ сюжет, который тянется и может повториться в след. отчётах", "status": "new|continuing|escalating|improving|resolved"}}]
}}
Правила:
- characters: 1-6 человек, о ком реально есть сигнал в материале (не выдумывай людей).
- На каждого: 2-4 highlights, 2-4 skills. mood — по общей динамике этого человека.
- metric — ТОЛЬКО если реально прозвучала цифра/срок/процент. Не выдумывай.
- attention — деликатно и по делу (напр. «признаки перегрузки»), если есть сигнал.
- "sensitive": true — на ЛИЧНЫЕ/чувствительные пункты (перегрузка, конфликт, риск
  ухода, недовольство): их скроют в командной версии.
- По-русски, кратко. threads — 0-3 сквозных сюжета (риск/инициатива/проблема); нет таких — пустой массив, не выдумывай. Верни ТОЛЬКО JSON.

МАТЕРИАЛ:
{src}
"""


def _characters(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Список персонажей: из data["characters"] ИЛИ одиночный (обратная
    совместимость со старой формой {name, ...})."""
    chars = data.get("characters")
    if isinstance(chars, list):
        out = [c for c in chars if isinstance(c, dict) and str(c.get("name") or "").strip()]
        if out:
            return out[:6]
    if str(data.get("name") or "").strip():
        return [data]
    return []


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


async def extract_character_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    """LLM раскладывает материал в карточку-персонажа. None при провале."""
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("character: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("character extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    # Годится, если есть хотя бы один персонаж (новая форма) ИЛИ имя (старая).
    if not data or not _characters(data):
        return None
    return data


_MOOD_DESC = {
    "growth": "энергичный, на подъёме, вокруг искры роста",
    "steady": "спокойный, уверенный, стабильный",
    "strain": "уставший, напряжённый, приглушённые тона",
    "down": "поникший, тревожные красные акценты",
}


def _one_character_line(ch: Dict[str, Any]) -> str:
    """Компактное описание одного персонажа для группового промпта."""
    name = str(ch.get("name") or "").strip()
    role = str(ch.get("role") or "").strip()
    mood = _norm_mood(ch.get("mood"))
    hi = "; ".join(
        f"«{str(h.get('text') or '').strip()}»" + (f" [{h.get('metric')}]" if h.get("metric") else "")
        for h in (ch.get("highlights") or []) if isinstance(h, dict) and str(h.get("text") or "").strip())
    sk = ", ".join(f"{s.get('name')} ({s.get('delta')})"
                   for s in (ch.get("skills") or []) if isinstance(s, dict) and s.get("name"))
    level = str(ch.get("level") or "").strip()
    parts = [f"«{name}»" + (f" ({role})" if role else "") + f" — {_MOOD_DESC[mood]}"]
    if level:
        parts.append(f"титул «{level}»")
    if hi:
        parts.append(f"достижения: {hi}")
    if sk:
        parts.append(f"навыки: {sk}")
    return "; ".join(parts)


def build_character_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    """Плотный промпт для image-модели: портрет-персонаж (RPG). Один человек —
    карточка героя; несколько — «отряд героев» (командная карточка)."""
    v = _variety(seed)
    chars = _characters(data)
    period = str(data.get("period") or (chars[0].get("period") if chars else "") or "").strip()

    if len(chars) > 1:
        team_title = str(data.get("team_title") or "").strip()
        roster = "\n".join(f"{i}. {_one_character_line(c)}" for i, c in enumerate(chars, 1))
        return (
            f"Создай МЕТАФОРИЧНУЮ карточку «ОТРЯД ГЕРОЕВ» — {len(chars)} персонажей команды "
            f"в ЕДИНОМ стиле коллекционных RPG-карт ({v['style']}) на фоне «{v['texture']}». "
            f"Каждый участник — отдельная мини-карточка героя в общей раскладке (сетка/ряд), "
            f"РАВНОГО размера и веса (никого не выделяй крупнее). У каждой карточки: крупное "
            f"ДОСЛОВНОЕ имя, аватар-герой с настроением, титул, иконки-достижения и полоски "
            f"навыков со стрелками роста. Настроение задаёт цветовую ауру карточки.\n"
            + (f"Заголовок сверху (баннером): «{team_title}»" + (f", период «{period}»" if period else "") + ".\n"
               if team_title else (f"Период (баннером): «{period}».\n" if period else ""))
            + "ГЕРОИ ОТРЯДА (перерисуй имена и подписи ДОСЛОВНО):\n" + roster + "\n"
            + f"{v['accent']}. " + all_text_note(lang) + " "
            "Тёплая, уважительная подача (никого не унижать); широкая композиция под сетку карточек."
        )

    # Одиночный герой (как раньше).
    ch = chars[0] if chars else data
    name = str(ch.get("name") or pick(lang, "Сотрудник", "Teammate")).strip()
    role = str(ch.get("role") or "").strip()
    level = str(ch.get("level") or "").strip()
    mood = _norm_mood(ch.get("mood"))
    hi = "; ".join(
        f"«{str(h.get('text') or '').strip()}»" + (f" [{h.get('metric')}]" if h.get("metric") else "")
        for h in (ch.get("highlights") or []) if isinstance(h, dict))
    sk = ", ".join(f"{s.get('name')} ({s.get('delta')})"
                   for s in (ch.get("skills") or []) if isinstance(s, dict))
    return (
        f"Создай МЕТАФОРИЧНЫЙ «портрет-персонаж» сотрудника (карточка героя, {v['style']}) "
        f"на фоне «{v['texture']}». Персонаж стилизован как RPG-герой: {_MOOD_DESC[mood]}. "
        f"ИМЯ (крупно, дословно): «{name}»" + (f", роль «{role}»" if role else "")
        + (f", период «{period}»" if period else "") + ".\n"
        + (f"ТИТУЛ/УРОВЕНЬ по итогу (баннером): «{level}».\n" if level else "")
        + (f"Достижения (иконками-медалями, подписи ДОСЛОВНО): {hi}.\n" if hi else "")
        + (f"Прокачка навыков (полоски/звёзды со стрелками роста): {sk}.\n" if sk else "")
        + f"{v['accent']}. "
        + all_text_note(lang) + " "
        "Каждый портрет уникален: поза/сцена/атрибуты разные. Эмоционально, тепло, "
        "как коллекционная карточка; вертикально-портретная или 4:5 композиция."
    )


def _render_mini_card(draw, x: int, y: int, w: int, h: int, ch: Dict[str, Any],
                      lang: str, corner: int) -> None:
    """Компактная карточка одного героя (для командной раскладки)."""
    mood = _norm_mood(ch.get("mood"))
    accent = _MOOD_COLOR[mood]
    name = str(ch.get("name") or "?").strip()
    role = str(ch.get("role") or "").strip()
    level = str(ch.get("level") or "").strip()
    f_name = _load_font(True, 32)
    f_sub = _load_font(False, 20)
    f_head = _load_font(True, 20)
    f_item = _load_font(False, 19)
    f_badge = _load_font(True, 16)
    # Рамка + цветная шапка
    _rounded(draw, [x, y, x + w, y + h], corner, fill=(255, 255, 255), outline=accent, width=3)
    _rounded(draw, [x, y, x + w, y + 108], corner, fill=accent)
    draw.rectangle([x, y + 60, x + w, y + 108], fill=accent)
    ax, ay, ar = x + 56, y + 54, 40
    draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=(255, 255, 255))
    ini = _initials(name)
    draw.text((ax - draw.textlength(ini, font=f_head) / 2, ay - 12), ini, font=f_head, fill=accent)
    draw.text((x + 108, y + 20), name[:20], font=f_name, fill=(255, 255, 255))
    sub = " · ".join(z for z in (role, _mood_label(mood, lang)) if z)
    if sub:
        draw.text((x + 110, y + 62), sub[:34], font=f_sub, fill=(255, 255, 255))
    yy = y + 124
    if level:
        draw.text((x + 16, yy), "★ " + level[:30], font=f_head, fill=_INK)
        yy += 34
    for h_i in [hh for hh in (ch.get("highlights") or []) if isinstance(hh, dict)][:3]:
        txt = str(h_i.get("text") or "").strip()
        if not txt:
            continue
        met = str(h_i.get("metric") or "").strip()
        draw.ellipse([x + 18, yy + 7, x + 28, yy + 17], fill=accent)
        for k, ln in enumerate(_wrap(draw, txt, f_item, w - 120)[:2]):
            draw.text((x + 40, yy + k * 24), ln, font=f_item, fill=_INK)
        if met:
            bw = draw.textlength(met, font=f_badge) + 16
            _rounded(draw, [x + w - 16 - bw, yy, x + w - 16, yy + 26], 8, fill=accent)
            draw.text((x + w - 8 - bw, yy + 3), met, font=f_badge, fill=(255, 255, 255))
        yy += 26
    sk = [s for s in (ch.get("skills") or []) if isinstance(s, dict) and s.get("name")][:4]
    if sk:
        line = ", ".join(f"{s.get('name')} ({s.get('delta')})" if s.get("delta") else str(s.get("name")) for s in sk)
        for k, ln in enumerate(_wrap(draw, pick(lang, "Прокачка: ", "Level-up: ") + line, f_item, w - 40)[:2]):
            draw.text((x + 18, yy + 6 + k * 24), ln, font=f_item, fill=(90, 90, 90))


def render_character_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    """Детерминированный рендер карточки-персонажа (Pillow). Идеальная кириллица.
    Один человек — большая карточка; несколько — сетка карточек «отряда»."""
    from PIL import Image, ImageDraw

    v = _variety(seed)
    bg = v["bg"]
    corner = v["corner"]
    chars = _characters(data)

    if len(chars) > 1:
        period = str(data.get("period") or "").strip()
        team_title = str(data.get("team_title") or pick(lang, "Команда", "Team")).strip()
        cols = 2
        rows = (len(chars) + cols - 1) // cols
        cw, chh, gap, pad, top = 720, 300, 30, 40, 150
        W = pad * 2 + cols * cw + (cols - 1) * gap
        H = top + rows * (chh + gap) - gap + pad + 40
        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)
        f_title = _load_font(True, 44)
        f_foot = _load_font(False, 18)
        _rounded(draw, [40, 36, W - 40, 116], corner + 6, fill=_INK)
        head = team_title + (f" · {period}" if period else "")
        draw.text((64, 54), head[:48], font=f_title, fill=(255, 255, 255))
        for idx, ch in enumerate(chars):
            r, c = divmod(idx, cols)
            x = pad + c * (cw + gap)
            y = top + r * (chh + gap)
            _render_mini_card(draw, x, y, cw, chh, ch, lang, corner)
        foot = pick(lang, "Tessbrain · отряд героев", "Tessbrain · hero squad")
        draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 30), foot, font=f_foot, fill=_MUTED)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    # Один герой (как раньше).
    ch = chars[0] if chars else data
    name = str(ch.get("name") or pick(lang, "Сотрудник", "Teammate")).strip()
    role = str(ch.get("role") or "").strip()
    period = str(data.get("period") or ch.get("period") or "").strip()
    level = str(ch.get("level") or "").strip()
    mood = _norm_mood(ch.get("mood"))
    accent = _MOOD_COLOR[mood]
    highlights = [h for h in (ch.get("highlights") or []) if isinstance(h, dict)][:5]
    skills = [s for s in (ch.get("skills") or []) if isinstance(s, dict)][:5]
    attention = str(ch.get("attention") or "").strip()

    W, H = 1200, 1500
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    f_name = _load_font(True, 46)
    f_role = _load_font(False, 26)
    f_head = _load_font(True, 28)
    f_item = _load_font(False, 24)
    f_badge = _load_font(True, 20)
    f_foot = _load_font(False, 18)

    # Верхняя цветная «шапка» настроения
    _rounded(draw, [40, 40, W - 40, 300], corner + 8, fill=accent)
    # Аватар — круг с инициалами
    ax, ay, ar = 150, 170, 92
    draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=(255, 255, 255))
    ini = _initials(name)
    draw.text((ax - draw.textlength(ini, font=f_name) / 2, ay - 30), ini, font=f_name, fill=accent)
    # Имя, роль, период (на шапке)
    draw.text((270, 90), name[:26], font=f_name, fill=(255, 255, 255))
    sub = " · ".join(x for x in (role, period) if x)
    if sub:
        draw.text((272, 150), sub[:44], font=f_role, fill=(255, 255, 255))
    # Плашка настроения
    md = _mood_label(mood, lang)
    mw = draw.textlength(md, font=f_badge) + 28
    _rounded(draw, [272, 196, 272 + mw, 236], 12, fill=(255, 255, 255))
    draw.text((286, 202), md, font=f_badge, fill=accent)

    y = 340
    # Титул/уровень
    if level:
        _rounded(draw, [40, y, W - 40, y + 70], corner, fill=(255, 255, 255), outline=accent, width=3)
        draw.text((64, y + 16), "★ " + level[:40], font=f_head, fill=_INK)
        y += 96

    # Достижения
    if highlights:
        draw.text((44, y), pick(lang, "ДОСТИЖЕНИЯ", "ACHIEVEMENTS"), font=f_head, fill=accent)
        y += 44
        for h in highlights:
            txt = str(h.get("text") or "").strip()
            if not txt:
                continue
            met = str(h.get("metric") or "").strip()
            draw.ellipse([48, y + 8, 62, y + 22], fill=accent)
            lines = _wrap(draw, txt, f_item, W - 200)[:2]
            for k, ln in enumerate(lines):
                draw.text((78, y + k * 28), ln, font=f_item, fill=_INK)
            if met:
                bw = draw.textlength(met, font=f_badge) + 20
                _rounded(draw, [W - 40 - bw, y, W - 40, y + 30], 8, fill=accent)
                draw.text((W - 30 - bw, y + 4), met, font=f_badge, fill=(255, 255, 255))
            y += len(lines) * 28 + 14
        y += 10

    # Прокачка навыков
    if skills:
        draw.text((44, y), pick(lang, "ПРОКАЧКА", "LEVEL-UP"), font=f_head, fill=accent)
        y += 44
        for s in skills:
            nm = str(s.get("name") or "").strip()
            if not nm:
                continue
            delta = str(s.get("delta") or "").strip()
            draw.text((78, y), nm[:36], font=f_item, fill=_INK)
            if delta:
                bw = draw.textlength(delta, font=f_badge) + 20
                _rounded(draw, [W - 40 - bw, y, W - 40, y + 30], 8, fill=accent)
                draw.text((W - 30 - bw, y + 4), delta, font=f_badge, fill=(255, 255, 255))
            y += 40
        y += 8

    # Что требует внимания
    if attention:
        _rounded(draw, [40, y, W - 40, y + 96], corner, fill=(255, 248, 240), outline=(245, 124, 0), width=2)
        draw.text((60, y + 12), pick(lang, "▲ Внимание", "▲ Attention"), font=f_head, fill=(216, 100, 0))
        for k, ln in enumerate(_wrap(draw, attention, f_item, W - 120)[:2]):
            draw.text((60, y + 48 + k * 26), ln, font=f_item, fill=_INK)

    foot = pick(lang, "Tessbrain · портрет-персонаж", "Tessbrain · character card")
    draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 34), foot, font=f_foot, fill=_MUTED)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _one_character_facts(ch: Dict[str, Any], period: str, lang: str) -> List[str]:
    name = str(ch.get("name") or pick(lang, "Сотрудник", "Teammate")).strip()
    role = str(ch.get("role") or "").strip()
    mood = _norm_mood(ch.get("mood"))
    head = f"🎖 {name}"
    tail = " · ".join(x for x in (role, period) if x)
    if tail:
        head += " — " + tail
    parts: List[str] = [head, pick(lang, "Состояние: ", "State: ") + _mood_label(mood, lang)]
    level = str(ch.get("level") or "").strip()
    if level:
        parts.append(f"🏆 {level}")
    hi = [h for h in (ch.get("highlights") or []) if isinstance(h, dict)][:5]
    if hi:
        parts.append(pick(lang, "Достижения:", "Achievements:"))
        for h in hi:
            txt = str(h.get("text") or "").strip()
            met = str(h.get("metric") or "").strip()
            if txt:
                parts.append(f"  • {txt}" + (f" — {met}" if met else ""))
    sk = [s for s in (ch.get("skills") or []) if isinstance(s, dict)][:5]
    if sk:
        parts.append(pick(lang, "Прокачка: ", "Level-up: ") + ", ".join(
            f"{s.get('name')} ({s.get('delta')})" for s in sk if s.get("name")))
    att = str(ch.get("attention") or "").strip()
    if att:
        parts.append(pick(lang, f"⚠ Внимание: {att}", f"⚠ Attention: {att}"))
    return parts


def build_character_facts(data: Dict[str, Any], *, limit: int = 1024, lang: str = "ru") -> str:
    """Текст-дубль карточки (на случай ошибок модели в картинке). Один человек —
    одна карточка; несколько — блок на каждого."""
    chars = _characters(data)
    period = str(data.get("period") or (chars[0].get("period") if chars else "") or "").strip()
    if not chars:
        return ""
    if len(chars) == 1:
        return "\n".join(_one_character_facts(chars[0], period, lang)).strip()[:limit]
    title = str(data.get("team_title") or pick(lang, "Отряд героев", "Hero squad")).strip()
    blocks: List[str] = [f"👥 {title}" + (f" · {period}" if period else "")]
    for ch in chars:
        blocks.append("")
        blocks.extend(_one_character_facts(ch, period, lang))
    return "\n".join(blocks).strip()[:limit]


__all__ = [
    "build_character_facts",
    "build_character_prompt",
    "extract_character_data",
    "render_character_png",
]
