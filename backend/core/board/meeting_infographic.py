# -*- coding: utf-8 -*-
"""Богатая инфографика встречи: рисует ИЗОБРАЖЕНИЕ-модель (nano-banana/gpt-image),
а ключевые факты ДУБЛИРУЮТСЯ текстом (подпись), чтобы данные были читаемы даже
если модель ошибётся в паре слов.

Поток: текст встречи → LLM раскладывает в структуру (тема + ветки + пункты +
цифры) → отсюда строим (1) плотный промпт для image-модели с ТОЧНЫМ русским
текстом, который она перерисовывает, и (2) текст-дубль ключевых фактов в подпись.
Ничего не выдумываем: цифры/сроки — только если реально прозвучали на встрече.

render_infographic_png (Pillow) остаётся ЗАПАСНЫМ вариантом — когда у image-модели
нет ключа/она упала, процесс всё равно отдаёт читаемую детерминированную картинку.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

from backend.core.board.viz_lang import all_text_note, is_ru, lang_line, pick

logger = logging.getLogger(__name__)

# ── Палитра веток (насыщенные, различимые — как в аналитических инфографиках) ──
_BRANCH_COLORS = [
    (34, 139, 87),    # зелёный
    (198, 40, 40),    # красный
    (106, 27, 154),   # фиолетовый
    (21, 101, 192),   # синий
    (0, 131, 143),    # бирюзовый
    (230, 126, 34),   # оранжевый
    (173, 20, 87),    # маджента
    (125, 102, 8),    # золото
]
_INK = (33, 41, 54)           # тёмный текст
_MUTED = (90, 98, 112)        # приглушённый

# ── Движок разнообразия ──────────────────────────────────────────────────────
# Один тип отчёта каждый раз выглядит чуть иначе (против баннерной слепоты), но
# узнаваемо. Сид детерминирован из содержимого: одна встреча → стабильный вид,
# разные встречи → разные. См. docs/VISUAL_REPORTS.md, §3 «Закон разнообразия».

# Несколько палитр-настроений (каждая — 8 различимых цветов веток).
_PALETTES = [
    _BRANCH_COLORS,
    [(0, 121, 107), (216, 67, 21), (69, 39, 160), (2, 119, 189),
     (0, 151, 167), (245, 124, 0), (194, 24, 91), (129, 119, 23)],   # тёплая
    [(46, 125, 50), (211, 47, 47), (123, 31, 162), (25, 118, 210),
     (0, 137, 123), (239, 108, 0), (173, 20, 87), (109, 76, 65)],    # глубокая
    [(56, 142, 60), (229, 57, 53), (94, 53, 177), (30, 136, 229),
     (0, 172, 193), (251, 140, 0), (216, 27, 96), (141, 110, 99)],   # яркая
]
_BG_TONES = [(245, 241, 232), (243, 238, 227), (240, 240, 235),
             (247, 243, 234), (242, 236, 224)]
# Пулы стиля для промпта image-модели (метафора/текстура/акцент — каждый раз иные).
_PROMPT_STYLE = [
    "рисованный бизнес-скетчноутинг", "инженерная доска-схема от руки",
    "редакционная инфографика как в деловом журнале", "живой майндмэп цветными маркерами",
]
_PROMPT_TEXTURE = [
    "текстурная кремовая бумага", "слегка состаренный крафт-лист",
    "белая доска с лёгкими бликами", "чертёжная миллиметровка бледных тонов",
]
_PROMPT_ACCENT = [
    "один «порочный круг» из стрелок, где темы усиливают друг друга",
    "каскад стрелок-домино от проблемы к последствиям",
    "пульсирующий красный акцент там, где риск, и растущая зелёная стрелка там, где рост",
    "узел-«очаг» с расходящимися трещинами к связанным темам",
]


def _seed_from(*parts: Any) -> int:
    """Стабильный сид из содержимого (детерминированно, кросс-платформенно)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def _variety(seed: Optional[int]) -> Dict[str, Any]:
    """Набор визуальных вариаций по сиду. seed=None → случайно каждый раз."""
    rng = random.Random(seed)
    palette = list(rng.choice(_PALETTES))
    rng.shuffle(palette)
    return {
        "palette": palette,
        "bg": rng.choice(_BG_TONES),
        "corner": rng.choice([12, 16, 20, 24]),
        "connector_w": rng.choice([6, 7, 8]),
        "left_first": rng.random() < 0.5,
        "style": rng.choice(_PROMPT_STYLE),
        "texture": rng.choice(_PROMPT_TEXTURE),
        "accent": rng.choice(_PROMPT_ACCENT),
    }

_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _load_font(bold: bool, size: int):
    from PIL import ImageFont
    for p in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> List[str]:
    """Перенос по словам под ширину max_w (Pillow сам не переносит)."""
    words = str(text).split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _curve(draw, p0, p1, color, width=6):
    """Плавный «рукав» от центра к ветке — квадратичная кривая сегментами."""
    (x0, y0), (x1, y1) = p0, p1
    cx, cy = (x0 + x1) / 2, y0  # контрольная точка: горизонтальный выход из центра
    pts = []
    steps = 24
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")


def render_infographic_png(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> bytes:
    """Отрисовать инфографику встречи в PNG. Данные — структура из extract_*.

    seed — движок разнообразия: палитра/фон/скругления/толщина связей и сторона
    старта варьируются, так что одна и та же встреча узнаваема, а разные —
    выглядят по-разному (против баннерной слепоты)."""
    from PIL import Image, ImageDraw

    title = str(data.get("title") or pick(lang, "Итоги встречи", "Meeting summary")).strip()
    subtitle = str(data.get("subtitle") or "").strip()
    branches = [b for b in (data.get("branches") or []) if isinstance(b, dict)][:8]

    v = _variety(seed)
    palette = v["palette"]
    # Фирменная палитра (брендовые схемы, §9 фаза 3): валидные hex-цвета
    # бренда заменяют случайную палитру и в КОД-рендере — вариативность
    # остаётся в композиции/фоне, а цвета всегда «наши».
    _brand_pal = [c for c in ((data.get("_brand") or {}).get("palette") or [])
                  if isinstance(c, str)
                  and re.fullmatch(r"#[0-9a-fA-F]{6}", c.strip())]
    if len(_brand_pal) >= 2:
        palette = (_brand_pal * 3)[:max(len(palette), 4)]
    bg = v["bg"]
    corner = v["corner"]
    conn_w = v["connector_w"]

    W, H = 1680, 1120
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    f_title = _load_font(True, 34)
    f_head = _load_font(True, 26)
    f_item = _load_font(False, 22)
    f_metric = _load_font(True, 20)
    f_sub = _load_font(False, 22)
    f_foot = _load_font(False, 18)

    # Заголовок сверху по центру
    for ln_i, ln in enumerate(_wrap(draw, title.upper(), f_title, W - 200)[:2]):
        tw = draw.textlength(ln, font=f_title)
        draw.text(((W - tw) / 2, 34 + ln_i * 42), ln, font=f_title, fill=_INK)

    # Центральный узел (обводка — первый цвет варьируемой палитры)
    cx, cy = W // 2, H // 2 + 20
    core_w, core_h = 380, 150
    core_box = [cx - core_w // 2, cy - core_h // 2, cx + core_w // 2, cy + core_h // 2]
    _rounded(draw, [core_box[0] + 6, core_box[1] + 8, core_box[2] + 6, core_box[3] + 8],
             corner + 12, fill=(225, 219, 206))  # тень
    _rounded(draw, core_box, corner + 12, fill=(255, 255, 255), outline=palette[0], width=4)
    core_lines = _wrap(draw, title, f_head, core_w - 44)[:3]
    ch = len(core_lines) * 32
    for i, ln in enumerate(core_lines):
        tw = draw.textlength(ln, font=f_head)
        draw.text((cx - tw / 2, cy - ch / 2 + i * 32), ln, font=f_head, fill=_INK)

    # Число-якорь под центральным узлом (если реально есть) — крупно, в акценте.
    cm = data.get("central_metric") if isinstance(data.get("central_metric"), dict) else {}
    cm_value = str((cm or {}).get("value") or "").strip()
    if cm_value:
        f_anchor = _load_font(True, 48)
        aw = draw.textlength(cm_value, font=f_anchor)
        draw.text((cx - aw / 2, core_box[3] + 12), cm_value, font=f_anchor,
                  fill=palette[1 % len(palette)])

    # Ветки: пополам слева/справа, стопкой по вертикали. Сторона старта варьируется.
    n = len(branches) or 1
    first = branches[: (n + 1) // 2]
    second = branches[(n + 1) // 2:]
    if v["left_first"]:
        left, right = first, second
    else:
        left, right = second, first

    def _draw_side(items, side: str, start_color_idx: int):
        if not items:
            return
        col_x = 60 if side == "left" else W - 60 - 520
        text_w = 520
        top = 150
        bottom = H - 70
        slot = (bottom - top) / len(items)
        for i, br in enumerate(items):
            color = palette[(start_color_idx + i) % len(palette)]
            y = top + slot * i + 24
            # Заголовок ветки — цветная плашка
            label = str(br.get("label") or "").strip() or "—"
            head_lines = _wrap(draw, label, f_head, text_w - 40)[:2]
            hh = len(head_lines) * 30 + 16
            head_box = [col_x, y, col_x + text_w, y + hh]
            _rounded(draw, head_box, corner, fill=color)
            for j, ln in enumerate(head_lines):
                draw.text((col_x + 20, y + 8 + j * 30), ln, font=f_head, fill=(255, 255, 255))
            # Рукав от центра к плашке
            anchor = (col_x + text_w, y + hh / 2) if side == "left" else (col_x, y + hh / 2)
            core_edge = (core_box[0], cy) if side == "left" else (core_box[2], cy)
            _curve(draw, core_edge, anchor, color, width=conn_w)
            # Число узла (если есть) — крупной цветной цифрой над пунктами.
            iy = y + hh + 10
            br_metric = str(br.get("metric") or "").strip()
            if br_metric:
                f_bm = _load_font(True, 28)
                draw.text((col_x + 8, iy), br_metric, font=f_bm, fill=color)
                iy += 36
            # Пункты ветки
            for it in (br.get("items") or [])[:4]:
                if not isinstance(it, dict):
                    it = {"text": str(it)}
                txt = str(it.get("text") or "").strip()
                if not txt:
                    continue
                metric = str(it.get("metric") or "").strip()
                bullet_x = col_x + 6
                draw.ellipse([bullet_x, iy + 8, bullet_x + 8, iy + 16], fill=color)
                avail = text_w - 40
                lines = _wrap(draw, txt, f_item, avail - 24)[:2]
                for k, ln in enumerate(lines):
                    draw.text((col_x + 24, iy + k * 26), ln, font=f_item, fill=_INK)
                block_h = len(lines) * 26
                if metric:
                    mw = draw.textlength(metric, font=f_metric) + 20
                    mbox = [col_x + 24, iy + block_h + 2, col_x + 24 + mw, iy + block_h + 30]
                    _rounded(draw, mbox, 8, fill=(*color, ) if False else color)
                    draw.text((col_x + 34, iy + block_h + 6), metric, font=f_metric, fill=(255, 255, 255))
                    block_h += 34
                iy += block_h + 12

    _draw_side(left, "left", 0)
    _draw_side(right, "right", len(left))

    if subtitle:
        sw = draw.textlength(subtitle, font=f_sub)
        draw.text(((W - sw) / 2, cy + core_h // 2 + 16), subtitle[:120], font=f_sub, fill=_MUTED)

    foot = pick(lang, "Tessbrain · инфографика встречи", "Tessbrain · meeting infographic")
    draw.text((W - draw.textlength(foot, font=f_foot) - 24, H - 30), foot, font=f_foot, fill=_MUTED)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


_EXTRACT_PROMPT = """Ты готовишь МЕТАФОРИЧНУЮ карту-инсайт по встрече — визуализацию, которую мозг считывает за 1 СЕКУНДУ, не читая текст: через образ, ОДИН фокус, цвет-сигнал и эмоцию. Разложи материал ниже в СТРОГИЙ JSON:
{{
  "title": "тема, 2-4 слова",
  "headline": "ГЛАВНАЯ мысль встречи ОДНОЙ фразой — то, ради чего на неё смотрят (ключевой сдвиг/решение/риск). Это ФОКУС всей картинки.",
  "hook": "подпись-крючок: короткая фраза с НЕЗАКРЫТОЙ петлёй, чтобы захотелось открыть картинку (напр. «Демо согласовали — а сроки повисли в воздухе»). Не пересказ, а зацепка.",
  "central_image": "ОДИН зримый образ-метафора, несущий headline с НАПРЯЖЕНИЕМ или направлением (не статичный): напр. «весы, кренящиеся в минус», «корабль, выходящий из гавани в шторм», «трещина, бегущая по мосту». Конкретный, рисуемый одним рисунком.",
  "accent": "up|watch|down — сигнал главного: рост / риск-внимание / потеря.",
  "central_metric": {{"value": "ОДНО главное число-якорь всей встречи (итоговая сумма/процент/счёт), напр. «$13.48M»", "note": "короткая рамка смысла, напр. «135% выручки», ИЛИ пусто"}},
  "branches": [
    {{"label": "1-3 слова",
      "icon": "иконка-образ узла, передающая суть БЕЗ слов (конкретный рисуемый объект: «сейф», «попугай», «черепаха», «песочные часы», «монеты»)",
      "tone": "up|calm|watch|down",
      "weight": "hero|normal",
      "metric": "ОДНО самое сильное число узла КРУПНО ($4.4M, 6 ч/нед) ИЛИ пусто",
      "items": [{{"text": "факт КЛЮЧЕВЫМИ СЛОВАМИ (до 4-5 слов, НЕ предложение)", "icon": "маленькая сценка-спутник: конкретный объект-метафора факта («уставший сотрудник», «стопка записок») ИЛИ пусто", "metric": "цифра/срок/имя ИЛИ пусто"}}]}}
  ],
  "links": [{{"from": "ДОСЛОВНЫЙ label ветви-причины", "to": "ДОСЛОВНЫЙ label ветви-следствия", "kind": "усиливает|порочный круг|каскад|ведёт к"}}],
  "loops": [{{"nodes": ["ДОСЛОВНЫЕ label 2-4 ветвей по кругу"], "label": "порочный круг|усиливающий цикл"}}],
  "quotes": [{{"text": "яркая цитата ДОСЛОВНО (до 12 слов), которая цепляет", "speaker": "кто сказал ИЛИ пусто"}}],
  "main_conclusion": "ГЛАВНЫЙ ВЫВОД встречи (до 25 слов) — идёт в отдельную плашку «Главный вывод»",
  "map_type": "потери|решение|проблемы|сделка|варианты|внедрение|конфликт|договорённости — тип карты, который ЛУЧШЕ всего объясняет эту встречу",
  "threads": [{{"title": "значимый СЮЖЕТ, который тянется и может повториться в след. отчётах (напр. «Риск задержки запуска клиента»)", "status": "new|continuing|escalating|improving|resolved"}}]
}}
Это ПЛОТНАЯ аналитическая КАРТА ПАМЯТИ (узлы + спутники + причинные стрелки + циклы + цитаты + вывод), а не список тем.
Правила:
- 6-8 веток; РОВНО ОДНА с weight=hero — самая важная (несёт headline), остальные normal.
- КЛЮЧЕВЫЕ СЛОВА, а не фразы: label 1-3 слова, items[].text до 4-5 слов. Длинные предложения на карте искажаются моделью — сжимай до сути.
- headline и hook — короткие и острые. central_image обязан нести эмоцию/направление, а не статику; конкретный рисуемый образ.
- ЧИСЛА (central_metric, branches[].metric, items[].metric) — ТОЛЬКО если реально прозвучали; не считай сам; central_metric — РОВНО одно доминирующее итоговое число на карту (нет такого — оставь пусто/убери).
- icon (у ветви и у item) — ТОЛЬКО конкретный рисуемый объект-метафора; абстрактное/непонятное → пусто.
- links — ТОЛЬКО между ДОСЛОВНЫМИ label существующих ветвей и ТОЛЬКО при реальной причинности; «порочный круг» замыкать, если поток реально возвращается к началу; причинности нет → пустой массив.
- loops — замкнутые циклы (порочные/усиливающие) по ДОСЛОВНЫМ label; ТОЛЬКО если поток реально возвращается к началу; нет — пустой массив.
- quotes — 1-4 ДОСЛОВНЫЕ яркие фразы со встречи (цитата = дословно, не пересказ); нет ярких цитат — пустой массив.
- main_conclusion — трезвый итог встречи (что решено/не решено), не лозунг.
- threads — 0-4 значимых СКВОЗНЫХ сюжета (риск/инициатива/проблема), которые логично встретить и в следующих отчётах; status по динамике. Нет таких — пустой массив.
- Все поля central_metric/metric/icon/links/loops/quotes/main_conclusion/map_type/threads ОПЦИОНАЛЬНЫ — пусто, если данных нет. Ничего не выдумывай.
- Верни ТОЛЬКО JSON, без пояснений.

МАТЕРИАЛ ВСТРЕЧИ:
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


async def extract_infographic_data(user_id: Optional[str], source_text: str, *, lang: str = "ru") -> Optional[Dict[str, Any]]:
    """LLM раскладывает текст встречи в структуру инфографики. None при провале."""
    src = (source_text or "").strip()
    if not src:
        return None
    prompt = _EXTRACT_PROMPT.format(src=src[:8000]) + lang_line(lang)
    raw = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id or "", "search_deep_synthesis", prompt)
    except Exception:
        logger.debug("infographic: workload route skipped", exc_info=True)
    if not raw:
        try:
            from backend.core.llm.router import get_llm_router
            raw = await get_llm_router().generate(prompt, personalize=False)
        except Exception as e:
            logger.warning("infographic extract failed: %s", e)
            return None
    data = _parse_json(raw or "")
    if not data or not data.get("branches"):
        return None
    return data


async def build_meeting_infographic(user_id: Optional[str], source_text: str) -> Dict[str, Any]:
    """Запасной цикл (Pillow): текст встречи → структура → PNG. Используется, когда
    image-модель недоступна. {"png": bytes} или {"error": ...}."""
    data = await extract_infographic_data(user_id, source_text)
    if not data:
        return {"png": None, "error": "не удалось разложить встречу в структуру (пустой вход или сбой LLM)"}
    try:
        png = render_infographic_png(data)
        return {"png": png, "title": data.get("title") or "", "branches": len(data.get("branches") or [])}
    except Exception as e:
        logger.exception("infographic render failed")
        return {"png": None, "error": f"рендер не удался: {e}"}


_TONE_COLOR_RU = {
    "up": "насыщенный тёплый зелёный", "calm": "спокойный бирюзовый",
    "watch": "янтарно-оранжевый", "down": "тревожный красно-терракотовый",
}
_TONE_COLOR_EN = {
    "up": "warm saturated green", "calm": "calm teal",
    "watch": "amber-orange", "down": "alarming terracotta red",
}
_TONE_ALIASES = {"рост": "up", "успех": "up", "норма": "calm", "спокойно": "calm",
                 "риск": "watch", "внимание": "watch", "проблема": "down", "потеря": "down"}

# Палитра Бьюзена: цвет НА ветвь (канон — «use a ton of colours»), каждая
# главная ветвь своего насыщенного цвета, поддерево наследует. Не 4 семантических
# тона. См. docs/VISUAL_REPORTS + ресёрч законов Бьюзена.
_BUZAN_PALETTE_RU = ["алый", "оранжевый", "золотой", "изумрудный", "бирюзовый",
                     "кобальтово-синий", "фиолетовый", "малиновая фуксия", "тёплая сепия"]
_BUZAN_PALETTE_EN = ["crimson red", "orange", "gold", "emerald green", "teal",
                     "cobalt blue", "violet", "magenta fuchsia", "warm sepia"]

# Правила плотной аналитической «карты памяти» (эталон — «Схема скрытых потерь»).
# Синтез ресёрча Бьюзена: цвет-на-узел, центр+число-якорь, узлы с иллюстрацией и
# числом, спутники-сценки, причинные стрелки. Рисуем ТОЛЬКО что есть в данных.
_BUZAN_RULES_RU = (
    "ПРАВИЛА КАРТЫ ПАМЯТИ (рисуй ТОЛЬКО то, что есть в данных, ничего не выдумывай): "
    "(1) ЦВЕТ НА УЗЕЛ — много разных насыщенных цветов, РОВНО ОДИН доминирующий на "
    "каждый узел; заголовок, иллюстрация, спутники и линии узла держат его цвет — глаз "
    "группирует тему по цвету мгновенно; монохром, единый акцент-цвет на всю карту и "
    "блёклая гамма ЗАПРЕЩЕНЫ. (2) ЦЕНТР — крупный светящийся цельный образ-якорь, БЕЗ "
    "мелких надписей внутри; число-якорь (если задано) — ОГРОМНЫМИ цифрами рядом. "
    "(3) УЗЛЫ (радиально) — у каждого жирный ЦВЕТНОЙ рукописный заголовок ПЕЧАТНЫМИ и "
    "КРУПНАЯ иллюстрация-сцена (крупнее подписи, понятна без слов); число узла (если "
    "есть) — большой цветной цифрой; ГЛАВНЫЙ (hero) узел крупнее и ярче всех. "
    "(4) СПУТНИКИ — вокруг узла маленькие рисованные сценки (человек/предмет-персонаж) с "
    "коротким фактом; число у факта — маленькой цифрой рядом; рисуй РОВНО столько "
    "спутников, сколько реальных фактов. (5) ПЛОТНОСТЬ — заполни лист богато и плотно "
    "(не разрежённая майндкарта), но каждый заголовок и число разборчивы. "
    "(6) ТЕКСТ — КЛЮЧЕВЫМИ СЛОВАМИ (канон Бьюзена): подпись узла 1-3 слова, факт-спутник "
    "до 4-5 слов; НИКАКИХ длинных фраз и абзацев внутри картинки — длинный текст модель "
    "искажает. Каждое слово — КРУПНО, чётко и БЕЗ ОРФОГРАФИЧЕСКИХ ошибок на языке подписей. "
    "Мало разборчивого текста лучше, чем много нечитаемого.")
_BUZAN_RULES_EN = (
    "MEMORY-MAP RULES (draw ONLY what the data contains, invent nothing): (1) COLOR PER "
    "NODE — many distinct saturated colors, EXACTLY ONE dominant per node; its heading, "
    "illustration, satellites and lines all hold that hue — the eye groups a topic by "
    "color instantly; monochrome, a single all-over accent color and washed-out palettes "
    "are forbidden. (2) CENTER — one large glowing anchor image, NO small lettering "
    "inside; the anchor number (if given) as HUGE figures beside it. (3) NODES (radial) — "
    "each with a bold COLORED hand-lettered heading in CAPS and a LARGE illustration-scene "
    "(bigger than the label, readable without words); the node number (if any) as a big "
    "colored figure; the hero node largest and brightest. (4) SATELLITES — small hand-"
    "drawn vignettes around a node (a person/object-character) with a short fact; a fact's "
    "number as a small figure beside it; draw EXACTLY as many satellites as real facts. "
    "(5) DENSITY — fill the sheet richly and densely (not a sparse mind map), yet every "
    "heading and number stays legible. (6) TEXT — KEYWORDS ONLY (Buzan canon): a node label "
    "is 1-3 words, a satellite fact up to 4-5 words; NO long phrases or paragraphs inside the "
    "image — long text gets mangled by the model. Every word LARGE, crisp and SPELLED "
    "CORRECTLY in the caption language. A little legible text beats a lot of unreadable text.")


def _norm_tone(s: Any) -> str:
    v = str(s or "").strip().lower()
    if v in _TONE_COLOR_RU:
        return v
    return _TONE_ALIASES.get(v, "calm")


# Сильные стилевые ПРЕСЕТЫ — цельный, «дорогой» вид (а не детский набросок).
# Пользователь выбирает пресет на узле; auto → ротация по сиду. Каждый — плотный
# арт-дирекшн, задающий узнаваемый мир. (ru, en).
_STYLE_PRESETS: Dict[str, tuple] = {
    "buzan": (
        "ЧИСТАЯ, ОРГАНИЗОВАННАЯ и легко читаемая РИСОВАННАЯ ОТ РУКИ интеллект-карта в "
        "каноне Тони Бьюзена — как эталонный скетчнот мастера-иллюстратора (НЕ шумный "
        "перегруженный коллаж, НЕ детский набросок, НЕ клипарт, НЕ плоский флэт-вектор). "
        "Тёплая кремовая бумага с лёгким зерном; уверенная живая линия тушью и кистью, "
        "сочные заливки цветными маркерами и акварелью, мягкий объём, местами добрый юмор. "
        "СТРОГО РАДИАЛЬНАЯ структура: в ЦЕНТРЕ — крупный округлый эмблема-медальон с главным "
        "образом-метафорой (образ, НЕ длинный текст); от него ПЛАВНО ИЗОГНУТЫМИ ветвями "
        "расходятся 6-8 узлов, КАЖДЫЙ своего насыщенного цвета и с номером (1, 2, 3…), "
        "толщина ветви = важность. У каждого узла — короткий ЦВЕТНОЙ рукописный заголовок "
        "ключевыми словами, выразительная иконка-сценка и аккуратный список из коротких "
        "пунктов-ключевых слов. По краям карты — облачка-цитаты; внизу — плашка-лента "
        "«Главный вывод». Богатая разноцветная палитра, где каждая ветвь группируется своим "
        "цветом с одного взгляда (никакого монохрома и блёклой гаммы). Всё ПРОСТОРНО и "
        "РАЗБОРЧИВО: воздух между ветвями, ничего не наезжает, каждое слово читается. За "
        "секунду считывается ИСТОРИЯ и ДИНАМИКА встречи (что на что влияет, где нарастает)",
        "a CLEAN, ORGANIZED and easily readable HAND-DRAWN mind map in the Tony Buzan canon "
        "— a reference-grade master illustrator's sketchnote (NOT a noisy overcrowded "
        "collage, NOT a childish doodle, NOT clipart, NOT flat vector). Warm cream paper with "
        "light grain; confident living ink-and-brush linework, juicy colored-marker and "
        "watercolor fills, soft depth, gentle humor here and there. STRICTLY RADIAL "
        "structure: at the CENTER a large rounded emblem-medallion holding the core metaphor "
        "IMAGE (an image, NOT long text); 6-8 nodes radiate from it on SMOOTHLY CURVED "
        "branches, EACH its own saturated color and numbered (1, 2, 3…), branch thickness = "
        "importance. Each node has a short COLORED hand-lettered keyword heading, an "
        "expressive icon-scene and a tidy list of short keyword bullet points. Quote bubbles "
        "float at the margins; a «Key takeaway» ribbon panel sits at the bottom. A rich "
        "multicolor palette where each branch groups by its own hue at a glance (no "
        "monochrome, no washed-out gamut). Everything SPACIOUS and LEGIBLE: air between "
        "branches, nothing overlaps, every word readable. It reads as the STORY and DYNAMICS "
        "of the meeting in one second (what drives what, where it builds)"),
    "buzan_vector": (
        "ЧИСТАЯ СОВРЕМЕННАЯ ВЕКТОРНАЯ интеллект-карта корпоративного уровня (как флэт-"
        "инфографика делового отчёта, НЕ рисованная от руки, НЕ акварель): светлый/белый "
        "фон, в ЦЕНТРЕ округлая панель-медальон с образом-эмблемой и заголовком; от неё "
        "тонкими ПЛАВНО ИЗОГНУТЫМИ цветными линиями-коннекторами расходятся 6-8 ветвей, "
        "каждая своего чистого насыщенного цвета и с номером в кружке (1,2,3…). У ветви — "
        "чёткий цветной заголовок гротеском, аккуратные флэт-лайн-иконки и опрятные буллет-"
        "списки короткими словами. Цитаты — в скруглённых прямоугольных выносках, «Главный "
        "вывод» — в аккуратной рамке-панели. Идеально ровная типографика, ничего не "
        "наезжает, много воздуха, ощущение дорогого корпоративного инфографика",
        "a CLEAN MODERN VECTOR mind map at corporate level (a flat business-report "
        "infographic, NOT hand-drawn, NOT watercolor): light/white background, at the CENTER "
        "a rounded emblem panel with the core image and title; 6-8 branches fan out on thin "
        "SMOOTHLY CURVED colored connector lines, each a clean saturated color with a "
        "numbered circle (1,2,3…). Each branch has a crisp colored sans-serif heading, tidy "
        "flat line icons and neat keyword bullet lists. Quotes sit in rounded rectangle "
        "callouts, the «Key takeaway» in a neat framed panel. Perfectly even typography, "
        "nothing overlaps, lots of air, an expensive corporate-infographic feel"),
    "buzan_marker": (
        "ЭНЕРГИЧНЫЙ РИСОВАННЫЙ ОТ РУКИ МАРКЕРНЫЙ скетчноут как на воркшопе/флипчарте: "
        "чистый белый фон, жирные сочные линии цветными маркерами и фломастерами, живое "
        "рукописное леттеринг ПЕЧАТНЫМИ, быстрые выразительные дудл-иконки. В ЦЕНТРЕ — "
        "облако/рамка с образом и заголовком, от него толстыми цветными маркерными ветвями "
        "расходятся 6-8 узлов, каждый своего цвета с номером в кружке. Короткие буллеты "
        "ключевыми словами, речевые пузыри с цитатами, «Главный вывод» в рамке со звездой. "
        "Бодро, разборчиво, с воздухом — доска после крутого воркшопа, НЕ акварель и НЕ "
        "плоский вектор",
        "an ENERGETIC HAND-DRAWN MARKER sketchnote like a workshop/flipchart: clean white "
        "background, bold juicy strokes in colored markers and felt-tips, lively hand-"
        "lettering in CAPS, quick expressive doodle icons. At the CENTER a cloud/frame with "
        "the core image and title; 6-8 nodes fan out on thick colored marker branches, each "
        "its own color with a numbered circle. Short keyword bullets, speech bubbles with "
        "quotes, the «Key takeaway» in a star-marked box. Upbeat, legible, airy — a "
        "whiteboard after a great workshop, NOT watercolor and NOT flat vector"),
    "buzan_chalk": (
        "интеллект-карта ЦВЕТНЫМ МЕЛОМ на тёмной грифельной доске (уютный кафе-борд/"
        "класс): глубокий графитово-чёрный фон с лёгкой меловой пылью, в ЦЕНТРЕ меловая "
        "эмблема-медальон с образом и заголовком; от неё изогнутыми меловыми ветвями "
        "разных ярких мелков расходятся 6-8 узлов, каждый со своим цветом и номером в "
        "кружке. Живое меловое леттеринг ПЕЧАТНЫМИ, меловые дудл-иконки, короткие буллеты "
        "ключевыми словами, облачка-цитаты, рамка со звездой «Главный вывод». Мягкая "
        "меловая фактура, тёплое свечение; разборчиво и с воздухом, НЕ вектор и НЕ акварель",
        "a mind map in COLORED CHALK on a dark slate blackboard (a cozy cafe board / "
        "classroom): deep graphite-black ground with light chalk dust, at the CENTER a chalk "
        "emblem-medallion with the core image and title; 6-8 nodes radiate on curved chalk "
        "branches in different bright chalks, each its own color with a numbered circle. "
        "Lively chalk hand-lettering in CAPS, chalk doodle icons, short keyword bullets, "
        "quote clouds, a star-marked «Key takeaway» frame. Soft chalk texture, warm glow; "
        "legible and airy, NOT vector and NOT watercolor"),
    "buzan_notebook": (
        "интеллект-карта, НАРИСОВАННАЯ РУЧКОЙ в КЛЕТЧАТОЙ тетради (личные конспекты): фон "
        "— страница в клетку тёплого бумажного тона, всё нарисовано цветными гелевыми/"
        "шариковыми ручками. В ЦЕНТРЕ — обведённая от руки рамка с образом и заголовком; от "
        "неё изогнутыми разноцветными ручными ветвями расходятся 6-8 узлов, каждый своего "
        "цвета с номером в кружке. Аккуратный рукописный почерк ключевыми словами, "
        "маленькие ручечные дудл-иконки, буллеты, выноски-цитаты на полях, обведённая "
        "рамка «Главный вывод». Ощущение живого умного конспекта; чисто, разборчиво, "
        "с воздухом, НЕ плоский вектор и НЕ маркерная доска",
        "a mind map DRAWN IN PEN on SQUARED/GRID notebook paper (personal study notes): the "
        "background is warm-toned graph paper, everything drawn with colored gel/ballpoint "
        "pens. At the CENTER a hand-outlined frame with the core image and title; 6-8 nodes "
        "radiate on curved multicolor pen branches, each its own color with a numbered "
        "circle. Neat handwriting in keywords, tiny pen doodle icons, bullets, margin quote "
        "callouts, a boxed «Key takeaway». The feel of a smart living study note; clean, "
        "legible, airy, NOT flat vector and NOT a marker board"),
    "poster": (
        "смелый редакционный инфографик-ПЛАКАТ журнального уровня: винтажная фактурная "
        "бумага/крафт с лёгким гранжем, ОГРОМНЫЙ фокусный элемент в центре (ключевое "
        "число или слово), вокруг — драматичные гравюрные мини-сцены с людьми и "
        "предметами, крупный заголовок сверху и плашка-призыв снизу, высокий контраст",
        "a bold magazine-grade editorial infographic POSTER: vintage textured/kraft "
        "paper with light grunge, a HUGE focal element at the center (the key number or "
        "word), dramatic engraved mini-scenes with people and objects around it, a large "
        "headline on top and a call-to-action bar at the bottom, high contrast"),
    "neon": (
        "футуристичный тех-инфографик кинематографичного уровня: глубокий тёмный фон, "
        "светящиеся узлы и плавные потоки-связи, неоновые бирюзово-янтарно-фиолетовые "
        "акценты, полупрозрачные стеклянные карточки, ощущение «дашборда из будущего»",
        "a cinematic futuristic tech infographic: deep dark background, glowing nodes "
        "and smooth flowing connections, neon teal-amber-violet accents, translucent "
        "glass cards, a 'dashboard from the future' feel"),
    "editorial": (
        "чистый современный редакционный флэт-инфографик уровня делового журнала: "
        "светлый фон, аккуратная сетка, минималистичные плоские иконки, приятная "
        "сдержанная палитра, плотно, но с воздухом и идеальной типографикой",
        "a clean modern flat editorial infographic at business-magazine level: light "
        "background, tidy grid, minimalist flat icons, a restrained pleasant palette, "
        "dense yet airy with impeccable typography"),
    "isometric": (
        "современный ИЗОМЕТРИЧЕСКИЙ 3D-инфографик уровня продуктовой презентации: аккуратные "
        "изометрические сцены, объекты и фигурки людей под углом 3/4, мягкие длинные тени и "
        "объём, единая сдержанно-корпоративная палитра, парящие карточки с номерами и "
        "короткими подписями, тонкие соединительные линии между блоками. Чисто, дорого, "
        "техно-стильно; много воздуха, идеальная читаемость",
        "a modern ISOMETRIC 3D infographic at product-deck level: neat isometric scenes, "
        "objects and little human figures at a 3/4 angle, soft long shadows and depth, a "
        "cohesive restrained corporate palette, floating cards with numbers and short "
        "labels, thin connector lines between blocks. Clean, premium, techy; lots of air, "
        "perfect legibility"),
    "blueprint": (
        "технический ЧЕРТЁЖ-BLUEPRINT: тонкие белые/голубые линии, штриховки и аннотации на "
        "глубоком синем чертёжном фоне, точные геометрические иконки, пунктирные "
        "выносные/размерные линии, аккуратные подписи-выноски. Инженерно-схематичная "
        "эстетика, но РАЗБОРЧИВАЯ и не перегруженная — как красивый концепт-чертёж",
        "a technical BLUEPRINT schematic: thin white/cyan lines, hatching and annotations on "
        "a deep blue drafting ground, precise geometric icons, dashed leader/dimension lines, "
        "tidy callout labels. An engineering-schematic aesthetic, yet LEGIBLE and not "
        "overloaded — like a beautiful concept blueprint"),
    "engraving": (
        "винтажная РЕТРО-ГРАВЮРА в духе старой энциклопедии/газеты XIX века: тонкая "
        "штриховка и кросс-хэтчинг тушью, гравюрные мини-сцены с людьми и предметами, "
        "тёплая сепия/кремовая бумага с лёгким состариванием, благородные акцентные цвета "
        "поверх гравюры, классическая типографика с засечками. Дорого, атмосферно, но "
        "РАЗБОРЧИВО — как иллюстрированный деловой альманах прошлого века",
        "a vintage RETRO-ENGRAVING in the spirit of a 19th-century encyclopedia/newspaper: "
        "fine ink hatching and cross-hatching, engraved mini-scenes of people and objects, "
        "warm sepia/cream aged paper, noble accent colors over the etching, classic serif "
        "typography. Rich and atmospheric yet LEGIBLE — like an illustrated business almanac "
        "of the past century"),
    "magazine": (
        "премиальный ЖУРНАЛЬНЫЙ РАЗВОРОТ уровня делового издания: чёткая модульная сетка "
        "колонок, крупный акцентный заголовок, огромные выносные цитаты (pull-quotes), "
        "элегантная смесь фото-качественных иллюстраций и инфографики, много воздуха, "
        "изысканная типографская иерархия и сдержанная дорогая палитра. Ощущение обложки "
        "и разворота глянцевого бизнес-журнала",
        "a premium MAGAZINE SPREAD at business-publication level: a crisp modular column "
        "grid, a large accent headline, huge pull-quotes, an elegant mix of photo-grade "
        "illustration and infographic, lots of air, refined typographic hierarchy and a "
        "restrained expensive palette. The feel of a glossy business-magazine cover and "
        "spread"),
    "comic": (
        "энергичный КОМИКС / ПОП-АРТ-инфографик: жирные чёрные контуры, сочные плоские "
        "заливки, полутон-точки (ben-day dots), динамичные панели и «взрывные» акценты-"
        "вспышки, крупные речевые баблы и звуковые надписи. Бодро и весело, но "
        "РАЗБОРЧИВО и по делу — деловой смысл в поп-арт-подаче, без хаоса",
        "an energetic COMIC / POP-ART infographic: bold black outlines, juicy flat fills, "
        "halftone ben-day dots, dynamic panels and burst accents, large speech balloons and "
        "sound-word captions. Upbeat and fun yet LEGIBLE and on-point — business meaning in "
        "a pop-art delivery, without chaos"),
    "comic_manga": (
        "чёрно-белый МАНГА / АНИМЕ-комикс: экспрессивная перьевая линия, скринтоны и "
        "штриховка вместо цвета, крупные выразительные глаза и эмоции, динамичные "
        "спидлайны, аккуратные прямоугольные панели с номерами, речевые баблы. Один-два "
        "акцентных цвета максимум; разборчиво, по делу",
        "a black-and-white MANGA / ANIME comic: expressive pen linework, screentones and "
        "hatching instead of color, big expressive eyes and emotions, dynamic speed-lines, "
        "tidy numbered rectangular panels, speech bubbles. One or two accent colors at most; "
        "legible and on-point"),
    "comic_superhero": (
        "классический СУПЕРГЕРОЙСКИЙ комикс золотого века: жирный тушевый инкинг, сочные "
        "насыщенные цвета, полутон-точки, драматичные ракурсы и позы, звуковые надписи "
        "(BOOM/POW), чёткие панели с рамками и номерами, крупные баблы. Героика применительно "
        "к бизнес-сюжету; разборчиво",
        "a classic GOLDEN-AGE SUPERHERO comic: bold ink inking, rich saturated colors, "
        "halftone dots, dramatic angles and poses, sound-word captions (BOOM/POW), crisp "
        "framed numbered panels, large bubbles. Heroic treatment of the business story; "
        "legible"),
    "comic_noir": (
        "НУАР-комикс / графическая новелла: высококонтрастная чёрно-белая тушь с редкими "
        "красными акцентами, резкие тени и силуэты, атмосфера детектива, кинематографичные "
        "панели с рамками и номерами, лаконичные баблы. Серьёзно и стильно, но разборчиво",
        "a NOIR comic / graphic novel: high-contrast black-and-white ink with rare red "
        "accents, harsh shadows and silhouettes, a detective mood, cinematic framed numbered "
        "panels, terse bubbles. Serious and stylish yet legible"),
    "comic_newspaper": (
        "ГАЗЕТНЫЙ комикс-стрип (daily strip): простая чистая линия, мягкая приглушённая "
        "печатная палитра, лёгкая газетная фактура/растр, ровный ряд квадратных панелей с "
        "номерами, добрый юмор, короткие баблы. Уютно, разборчиво, по-деловому",
        "a NEWSPAPER comic strip (daily strip): simple clean line, soft muted print palette, "
        "light newsprint texture/dot-screen, an even row of square numbered panels, gentle "
        "humor, short bubbles. Cozy, legible, businesslike"),
}
_STYLE_ORDER = ("buzan", "poster", "neon", "editorial", "isometric", "blueprint",
                "engraving", "magazine", "comic")
# Все варианты «карты памяти» (интеллект-карты) — одна КАНОНИЧЕСКАЯ структура
# (радиальная, цвет-на-узел, ключевые слова, цитаты, вывод), разный АРТ-СТИЛЬ.
_MINDMAP_STYLES = {"buzan", "buzan_vector", "buzan_marker",
                   "buzan_chalk", "buzan_notebook"}


def build_infographic_prompt(data: Dict[str, Any], *, seed: Optional[int] = None, lang: str = "ru") -> str:
    """Промпт для image-модели: карта-инсайт, которую мозг считывает за 1 СЕКУНДУ,
    в СИЛЬНОМ цельном стиле (пресет). Принципы: ОДИН фокус (герой), ЕДИНЫЙ акцент-
    сигнал, продакшн-качество (не детский набросок), НИЧЕГО не обрезано, широкий
    кадр. Стиль — data["_style"] (buzan/poster/neon/editorial) или auto по сиду."""
    tone_color = _TONE_COLOR_RU if is_ru(lang) else _TONE_COLOR_EN
    title = str(data.get("title") or pick(lang, "Итоги встречи", "Meeting summary")).strip()
    central = str(data.get("central_image") or "").strip()
    accent = tone_color[_norm_tone(data.get("accent") or "watch")]

    style_key = str(data.get("_style") or "auto").strip().lower()
    if style_key not in _STYLE_PRESETS:
        style_key = _STYLE_ORDER[(seed or 0) % len(_STYLE_ORDER)]
    style_desc = _STYLE_PRESETS[style_key][0 if is_ru(lang) else 1]
    is_buzan = style_key in _MINDMAP_STYLES
    palette = _BUZAN_PALETTE_RU if is_ru(lang) else _BUZAN_PALETTE_EN

    # Фирменный стиль тенанта (Visual Reports §14) — мягкий оверлей поверх пресета.
    brand_block = ""
    try:
        from backend.core.board.brand_profile import brand_prompt_block
        brand_block = brand_prompt_block(data.get("_brand"), lang_ru=is_ru(lang))
    except Exception:
        brand_block = ""
    # Style-reference: приложены ОБРАЗЦЫ фирменного стиля — модель подражает
    # СТИЛЮ (палитра/линии/типографика/настроение), содержание берёт только
    # из данных, референс не копирует.
    if data.get("_style_ref"):
        brand_block += pick(lang,
            " К промпту приложены референс-изображения ФИРМЕННОГО СТИЛЯ "
            "компании: точно повтори их стиль — палитру, характер линий, "
            "типографику, настроение. СОДЕРЖАНИЕ референсов НЕ копируй: "
            "все объекты и подписи — только из данных этого отчёта.",
            " Brand STYLE reference images are attached: match their style "
            "precisely — palette, line character, typography, mood. Do NOT "
            "copy their CONTENT: every object and label comes only from "
            "this report's data.")

    # Бьюзен допускает 7-9 главных ветвей; прочие стили держим компактнее.
    branches = [b for b in (data.get("branches") or []) if isinstance(b, dict)][:9 if is_buzan else 7]

    lines: List[str] = []
    for i, br in enumerate(branches, 1):
        label = str(br.get("label") or "").strip()
        icon = str(br.get("icon") or "").strip()
        br_metric = str(br.get("metric") or "").strip()
        # Канон Бьюзена: цвет НА узел из богатой палитры (каждый свой), а не
        # 4 семантических тона. Прочие стили — цвет по смыслу (tone).
        color = palette[(i - 1) % len(palette)] if is_buzan else tone_color[_norm_tone(br.get("tone"))]
        is_hero = str(br.get("weight") or "").strip().lower() == "hero"
        # Спутники: каждый item — маленькая сценка (icon) с фактом и числом.
        sats = []
        for it in (br.get("items") or [])[:3]:
            if not isinstance(it, dict):
                it = {"text": str(it)}
            txt = str(it.get("text") or "").strip()
            met = str(it.get("metric") or "").strip()
            it_icon = str(it.get("icon") or "").strip()
            if not txt:
                continue
            fact = f"{txt} [{met}]" if met else txt
            if it_icon and is_buzan:
                sats.append(pick(lang, f"сценка «{it_icon}» — «{fact}»",
                                 f"vignette «{it_icon}» — «{fact}»"))
            else:
                sats.append(f"«{fact}»")
        icon_part = pick(lang, f"КРУПНАЯ иллюстрация-сцена «{icon}»",
                         f"a LARGE illustration-scene «{icon}»") if icon else ""
        metric_part = pick(lang, f", крупное число узла «{br_metric}»",
                           f", big node number «{br_metric}»") if br_metric else ""
        hero_mark = pick(lang, "[ГЛАВНЫЙ — крупнее и заметнее всех] ",
                         "[HERO — largest and most prominent] ") if is_hero else ""
        node_word = pick(lang, "Узел" if is_buzan else "Ветвь", "Node" if is_buzan else "Branch")
        sat_word = pick(lang, "спутники" if is_buzan else "подписи",
                        "satellites" if is_buzan else "labels")
        lines.append(
            pick(lang,
                 f"  {hero_mark}{node_word} {i} «{label}» — цвет {color}, {icon_part}{metric_part}; {sat_word}: "
                 + "; ".join(sats),
                 f"  {hero_mark}{node_word} {i} «{label}» — color {color}, {icon_part}{metric_part}; {sat_word}: "
                 + "; ".join(sats)))

    # Центр — ЦЕЛЬНЫЙ образ БЕЗ мелких надписей внутри (image-модели «глючат»
    # буквы): чистая картинка-метафора; заголовок — отдельным крупным словом рядом,
    # НЕ длинной фразой в ядре. Так центр не «плывёт» текстом.
    if central:
        central_line = pick(
            lang,
            f"В ЦЕНТРЕ — единый крупный цветной ОБРАЗ-МЕТАФОРА (мин. 3-5 цветов, объёмный, "
            f"с эмоцией/направлением): {central}. Это цельная чистая иллюстрация БЕЗ мелких "
            f"надписей внутри — только картинка. Название встречи, если нужно, — рядом отдельным "
            f"крупным словом «{title}», НЕ строкой текста в ядре.\n",
            f"At the CENTER — one large multicolor METAPHOR IMAGE (min. 3-5 colors, dimensional, "
            f"with emotion/direction): {central}. A single clean pictorial metaphor with NO small "
            f"lettering inside — image only. The meeting title, if needed, sits beside it as one "
            f"large word «{title}», NOT a line of text in the core.\n")
    else:
        central_line = pick(lang, f"В ЦЕНТРЕ — крупный цельный образ по теме «{title}» (без мелких надписей внутри).\n",
                            f"At the CENTER — one large clean image for «{title}» (no small lettering inside).\n")

    # Число-якорь: ОДНО доминирующее число встречи — ОГРОМНЫМИ цифрами у центра
    # (большие цифры image-модель рисует надёжно, в отличие от фраз). Нет — нет.
    cm = data.get("central_metric") if isinstance(data.get("central_metric"), dict) else {}
    cm_value = str((cm or {}).get("value") or "").strip()
    cm_note = str((cm or {}).get("note") or "").strip()
    if cm_value:
        central_line += pick(
            lang,
            f"РЯДОМ с центром — ОГРОМНОЕ ключевое число «{cm_value}»"
            + (f" ({cm_note})" if cm_note else "")
            + f" яркими цифрами в цвете {accent}, крупнее всего, как эмоциональный якорь (ТОЛЬКО цифры).\n",
            f"BESIDE the center — a HUGE key number «{cm_value}»"
            + (f" ({cm_note})" if cm_note else "")
            + f" in vivid {accent} figures, largest of all, as the emotional anchor (figures only).\n")

    head = pick(
        lang,
        f"Создай ВПЕЧАТЛЯЮЩИЙ инфографик-отчёт по встрече, стиль: {style_desc}. Уровень — "
        f"как у сильного дизайнера, «дорого» и профессионально (НЕ детский набросок, НЕ "
        f"кривые каракули). ЗАДАЧА: человек считывает суть за 1 СЕКУНДУ и его цепляет.\n",
        f"Create a STRIKING meeting infographic report, style: {style_desc}. Level — like a "
        f"strong designer, premium and professional (NOT a childish doodle, NOT crooked "
        f"scribbles). GOAL: a person grasps the point in 1 SECOND and is hooked.\n")

    frame_ru = ("КАДРИРОВАНИЕ — КРИТИЧНО: формат ШИРОКИЙ 16:9 (альбомный); ВСЯ композиция и "
                "ВЕСЬ текст — ЦЕЛИКОМ внутри кадра с запасом полей ~7% со всех сторон; НИЧЕГО не "
                "обрезано, НИ ОДНА буква не касается края. Подписи — 1-2 КЛЮЧЕВЫХ слова на линию, "
                "печатными ЗАГЛАВНЫМИ, крупно и разборчиво даже в миниатюре.")
    frame_en = ("FRAMING — CRITICAL: WIDE 16:9 (landscape); the ENTIRE composition and ALL text "
                "fully INSIDE the frame with ~7% safe margins on every side; NOTHING cropped, NO "
                "letter touches the edge. Labels — 1-2 KEY words per line, printed CAPITALS, large "
                "and legible even in a thumbnail.")
    if is_buzan:
        # Причинные стрелки — ТОЛЬКО между реально существующими узлами (по ДОСЛОВНОМУ
        # label) и только если LLM их выделил. Нет связей → стрелок нет.
        valid = {str(b.get("label") or "").strip() for b in branches}
        link_lines = []
        for lk in [x for x in (data.get("links") or []) if isinstance(x, dict)][:6]:
            frm = str(lk.get("from") or "").strip()
            to = str(lk.get("to") or "").strip()
            kind = str(lk.get("kind") or "").strip()
            if frm and to and kind and frm in valid and to in valid:
                link_lines.append(pick(lang,
                    f"стрелка от «{frm}» к «{to}» с подписью «{kind}»",
                    f"arrow from «{frm}» to «{to}» labeled «{kind}»"))
        if link_lines:
            links_block = pick(
                lang,
                " ПРИЧИННЫЕ СТРЕЛКИ (нарисуй как ИСТОРИЮ, изогнутыми рукописными стрелками "
                "с подписями): " + "; ".join(link_lines) + ".",
                " CAUSAL ARROWS (draw as a STORY, curved hand-inked arrows with labels): "
                + "; ".join(link_lines) + ".")
        else:
            links_block = ""
        rules = _BUZAN_RULES_RU if is_ru(lang) else _BUZAN_RULES_EN
        tail = rules + links_block + " " + (frame_ru if is_ru(lang) else frame_en)
    else:
        tail = pick(
            lang,
            f"ПРАВИЛА: (1) ОДИН фокус — центральный образ/ГЛАВНАЯ ветвь крупнее и ярче всех, "
            f"остальное — тише; (2) ЕДИНЫЙ акцент-сигнал: яркий «{accent}» ТОЛЬКО у главного, "
            f"остальная картинка спокойнее — взгляд летит в акцент; (3) у каждой ветви — "
            f"КАЧЕСТВЕННАЯ иллюстрация, передающая смысл без слов; (4) богато и плотно, но "
            f"организованно и с воздухом — высокая продакшн-ценность. " + frame_ru,
            f"RULES: (1) ONE focus — the central image/HERO branch largest and brightest, the "
            f"rest quieter; (2) ONE accent signal: vivid «{accent}» ONLY on the main point, the "
            f"rest calmer — the eye flies to the accent; (3) each branch has a QUALITY "
            f"illustration conveying meaning without words; (4) rich and dense yet organized and "
            f"airy — high production value. " + frame_en)

    section = (pick(lang, "Узлы (перерисуй подписи ДОСЛОВНО):\n", "Nodes (redraw labels VERBATIM):\n")
               if is_buzan else
               pick(lang, "Ветви (перерисуй подписи ДОСЛОВНО):\n", "Branches (redraw labels VERBATIM):\n"))

    # Доп-элементы «как в референсах»: цитаты → речевые пузыри, вывод → плашка,
    # циклы → петли. Рисуем ТОЛЬКО если данные есть (иначе не упоминаем).
    extras = []
    q_lines = []
    for q in [q for q in (data.get("quotes") or []) if isinstance(q, dict)][:4]:
        qt = str(q.get("text") or "").strip()
        sp = str(q.get("speaker") or "").strip()
        if qt:
            q_lines.append(f"«{qt}»" + (f" — {sp}" if sp else ""))
    if q_lines:
        extras.append(pick(lang,
            "РЕЧЕВЫЕ ПУЗЫРИ с ДОСЛОВНЫМИ цитатами по краям карты (облачка-реплики, крупно): "
            + "; ".join(q_lines) + ".",
            "SPEECH BUBBLES with VERBATIM quotes around the map (large): " + "; ".join(q_lines) + "."))
    concl = str(data.get("main_conclusion") or "").strip()
    if concl:
        extras.append(pick(lang,
            f"Внизу — крупная ПЛАШКА со звездой «ГЛАВНЫЙ ВЫВОД»: «{concl}».",
            f"At the bottom — a large star-marked «KEY TAKEAWAY» panel: «{concl}»."))
    if is_buzan:
        loop_lines = []
        for lp in [lp for lp in (data.get("loops") or []) if isinstance(lp, dict)][:3]:
            nodes = [str(n).strip() for n in (lp.get("nodes") or []) if str(n).strip()]
            lbl = str(lp.get("label") or pick(lang, "порочный круг", "vicious circle")).strip()
            if len(nodes) >= 2:
                loop_lines.append(f"{lbl}: " + " → ".join([*nodes, nodes[0]]))
        if loop_lines:
            extras.append(pick(lang,
                "ЦИКЛЫ круговыми стрелками-петлями (читаются без пояснений): " + "; ".join(loop_lines) + ".",
                "LOOPS as circular arrow loops (readable at a glance): " + "; ".join(loop_lines) + "."))
    # Сериальность: продолжающиеся сюжеты со стабильным символом (§17).
    try:
        from backend.core.board.report_threads import threads_prompt_block
        tb = threads_prompt_block(data.get("_threads"), lang_ru=is_ru(lang))
        if tb:
            extras.append(tb.strip())
    except Exception:
        pass
    extras_block = (" " + " ".join(extras)) if extras else ""

    return (head + brand_block + "\n" + central_line + section
            + "\n".join(lines) + "\n" + all_text_note(lang) + " " + tail + extras_block)


def build_key_facts_text(data: Dict[str, Any], *, limit: int = 2200, lang: str = "ru") -> str:
    """Текстовое САММАРИ рядом с картинкой — полное и КОПИРУЕМОЕ (с ним работают).
    Картинка бьёт мгновенно, текст даёт структуру для работы. Сверху — крючок
    (hook, зацепка), затем разбор по ветвям с пунктами и цифрами."""
    title = str(data.get("title") or pick(lang, "Итоги встречи", "Meeting summary")).strip()
    hook = str(data.get("hook") or data.get("essence") or "").strip()
    parts: List[str] = [f"🗂 {title}"]
    if hook:
        parts.append(hook)
    # Число-якорь (если есть) — сразу под крючком.
    cm = data.get("central_metric") if isinstance(data.get("central_metric"), dict) else {}
    cm_value = str((cm or {}).get("value") or "").strip()
    cm_note = str((cm or {}).get("note") or "").strip()
    if cm_value:
        parts.append(f"💥 {cm_value}" + (f" — {cm_note}" if cm_note else ""))
    parts.append("")
    for br in [b for b in (data.get("branches") or []) if isinstance(b, dict)][:8]:
        label = str(br.get("label") or "").strip()
        if not label:
            continue
        br_met = str(br.get("metric") or "").strip()
        parts.append(f"▸ {label}" + (f" — {br_met}" if br_met else ""))
        for it in (br.get("items") or [])[:4]:
            if not isinstance(it, dict):
                it = {"text": str(it)}
            txt = str(it.get("text") or "").strip()
            met = str(it.get("metric") or "").strip()
            if txt:
                parts.append(f"   • {txt}" + (f" — {met}" if met else ""))
    # Причинные связи (если выделены) — ЧЕЛОВЕЧЕСКИМИ фразами, а не «A → B (kind)».
    # Раньше строка «Боль → Интерактив (ведёт к)» была непонятна: что за стрелка,
    # что за «ведёт к». Разворачиваем в предложение с глаголом — поймёт любой.
    _link_ru = {
        "усиливает": "«{a}» усиливает «{b}»",
        "ведёт к": "«{a}» ведёт к «{b}»",
        "порочный круг": "«{a}» и «{b}» закручивают друг друга (порочный круг)",
        "каскад": "«{a}» запускает цепочку к «{b}»",
    }
    _link_en = {
        "усиливает": "«{a}» amplifies «{b}»",
        "ведёт к": "«{a}» leads to «{b}»",
        "порочный круг": "«{a}» and «{b}» feed each other (vicious circle)",
        "каскад": "«{a}» triggers a chain to «{b}»",
    }
    links = [x for x in (data.get("links") or []) if isinstance(x, dict)]
    link_rows = []
    for lk in links[:6]:
        frm = str(lk.get("from") or "").strip()
        to = str(lk.get("to") or "").strip()
        kind = str(lk.get("kind") or "").strip().lower()
        if frm and to:
            tmpl = (_link_ru if is_ru(lang) else _link_en).get(
                kind, pick(lang, "«{a}» влияет на «{b}»", "«{a}» affects «{b}»"))
            link_rows.append("   • " + tmpl.format(a=frm, b=to))
    if link_rows:
        parts.append("")
        parts.append(pick(lang, "🔗 Что на что влияло на встрече:",
                          "🔗 What affected what in the meeting:"))
        parts.extend(link_rows)
    # Цитаты (если есть) — как в референсах.
    q_rows = []
    for q in [q for q in (data.get("quotes") or []) if isinstance(q, dict)][:4]:
        qt = str(q.get("text") or "").strip()
        sp = str(q.get("speaker") or "").strip()
        if qt:
            q_rows.append(f"   «{qt}»" + (f" — {sp}" if sp else ""))
    if q_rows:
        parts.append("")
        parts.append(pick(lang, "💬 Цитаты:", "💬 Quotes:"))
        parts.extend(q_rows)
    # Главный вывод — отдельной строкой внизу.
    concl = str(data.get("main_conclusion") or "").strip()
    if concl:
        parts.append("")
        parts.append(pick(lang, f"⭐ Главный вывод: {concl}", f"⭐ Key takeaway: {concl}"))
    # Сквозные сюжеты (сериальность) — со статусом, если сверены.
    thr = [t for t in (data.get("_threads") or data.get("threads") or []) if isinstance(t, dict)]
    thr_rows = []
    _st_ru = {"new": "новый", "continuing": "продолжается", "escalating": "нарастает",
              "improving": "улучшается", "resolved": "решён", "stale": "затих"}
    for t in thr[:4]:
        ttl = str(t.get("title") or "").strip()
        stt = str(t.get("status") or "").strip().lower()
        if ttl:
            label = _st_ru.get(stt, stt) if is_ru(lang) else (stt or "")
            thr_rows.append(f"   {ttl}" + (f" — {label}" if label else ""))
    if thr_rows:
        parts.append("")
        parts.append(pick(lang, "🧵 Темы, которые тянутся из встречи в встречу:",
                          "🧵 Themes carrying over from meeting to meeting:"))
        parts.extend(thr_rows)
    text = "\n".join(parts).strip()
    return text[:limit]


def _threads_block(data: Dict[str, Any], *, lang: str = "ru") -> str:
    """Секция «Темы, которые тянутся из встречи в встречу» — отдельно, т.к. это
    кросс-встречные данные (сверка серии), которых нет в тексте одной встречи."""
    thr = [t for t in (data.get("_threads") or data.get("threads") or []) if isinstance(t, dict)]
    _st = {"new": "новый", "continuing": "продолжается", "escalating": "нарастает",
           "improving": "улучшается", "resolved": "решён", "stale": "затих"}
    rows = []
    for t in thr[:4]:
        ttl = str(t.get("title") or "").strip()
        stt = str(t.get("status") or "").strip().lower()
        if ttl:
            label = _st.get(stt, stt) if is_ru(lang) else (stt or "")
            rows.append(f"   {ttl}" + (f" — {label}" if label else ""))
    if not rows:
        return ""
    head = pick(lang, "🧵 Темы, которые тянутся из встречи в встречу:",
                "🧵 Themes carrying over from meeting to meeting:")
    return "\n" + head + "\n" + "\n".join(rows)


async def build_readable_caption(user_id: Optional[str], source_text: str,
                                 data: Dict[str, Any], *, lang: str = "ru",
                                 limit: int = 2400) -> str:
    """ЧИТАЕМАЯ подпись под картинку — ПОЛНЫМИ короткими предложениями, понятная
    любому, кто на встрече не был. Отдельный LLM-вызов (картинку не трогает): карта
    остаётся на ключевых словах, а текст — человеческий. Кросс-встречные «Темы»
    дописываем из data (их нет в тексте одной встречи). Пусто → вызывающий оставит
    keyword-подпись (build_key_facts_text). Never-raise, never-mock."""
    src = (source_text or "").strip()
    if len(src) < 40:
        return ""
    title = str((data or {}).get("title") or "").strip()
    instr = (
        "Сделай ПОНЯТНОЕ саммари встречи для мессенджера — чтобы понял любой человек, "
        "которого на встрече НЕ было. Строго по этому виду:\n"
        "🗂 <короткий заголовок темы>\n"
        "<одна фраза сути: о чём договорились или спорили>\n"
        "\n"
        "Далее 3–6 пунктов, каждый с «• » и ПОЛНЫМ коротким предложением (НЕ ключевые "
        "слова, НЕ обрывки) — что обсудили и к чему пришли.\n"
        "Если на встрече одно влияло на другое — добавь строку «🔗 Что на что влияло:» "
        "и 1–3 пункта простыми фразами с глаголом («X усиливает Y», «X ведёт к Y»).\n"
        "Если есть яркая ДОСЛОВНАЯ цитата — строка «💬 <цитата>».\n"
        "В конце — «⭐ Главный вывод: <одно предложение>».\n"
        "Только по реальному тексту встречи, ничего не выдумывай; без воды, канцелярита "
        "и жаргона. Простым живым языком."
        if is_ru(lang) else
        "Write a CLEAR meeting summary for a messenger — so anyone who was NOT at the "
        "meeting understands. Exactly this shape:\n"
        "🗂 <short topic title>\n"
        "<one sentence: what was agreed or argued>\n"
        "\n"
        "Then 3–6 bullets, each «• » a FULL short sentence (NOT keywords, NOT fragments) "
        "— what was discussed and concluded.\n"
        "If one thing drove another, add a line «🔗 What affected what:» with 1–3 plain "
        "sentences using a verb («X amplifies Y», «X leads to Y»).\n"
        "If there's a striking VERBATIM quote — a line «💬 <quote>».\n"
        "End with «⭐ Key takeaway: <one sentence>».\n"
        "Only from the real meeting text, invent nothing; no fluff or jargon. Plain, "
        "living language.")
    prompt = f"{instr}\n\nВСТРЕЧА «{title}»:\n{src[:8000]}"
    out = ""
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        out = str(await generate_for_workload(user_id or "", "chat", prompt) or "").strip()
    except Exception:
        logger.info("readable caption: LLM skipped", exc_info=True)
        return ""
    if len(out) < 40:
        return ""
    return (out + _threads_block(data, lang=lang)).strip()[:limit]


__all__ = [
    "build_infographic_prompt",
    "build_key_facts_text",
    "build_meeting_infographic",
    "build_readable_caption",
    "extract_infographic_data",
    "render_infographic_png",
]
