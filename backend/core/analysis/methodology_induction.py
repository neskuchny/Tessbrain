# -*- coding: utf-8 -*-
"""МОРМ-lite: индукция методологии компании из её реальных артефактов.

Вход: N документов одного жанра (медиапланы, сметы, посты, стратегии…) —
пары «вход→выход» чужого мышления, процесс скрыт. Выход: документ-
методология — правила с рамками применимости, происхождением и trust-счётом.

Три закона из МОРМ (docs/ методология Антона), которые здесь исполнены:
1. Валюта доверия — предсказание: правило засчитывается, только если
   ДЕРЖИТСЯ на отложенных (held-out) документах, которые индукция не видела.
2. Числовые оси считает КОД (константы/якоря/связки-отношения/округления),
   LLM только извлекает структуру текстов и формулирует правила.
3. Честность: < _MIN_PAIRS пар → статус «пилот гипотез», не «методология»;
   отклонённые гейтом правила перечисляются в «выброшено», не замалчиваются.

Держим held-out по ХВОСТУ списка (поздние документы проверяют, что поймали
процедуру, а не запомнили период) — реконструктор сплит не выбирает.
"""
from __future__ import annotations

import logging
import re
import statistics
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MIN_PAIRS = 10          # меньше — честный «пилот гипотез»
_HELDOUT_SHARE = 0.25    # хвост списка
_MIN_GATE_DOCS = 4       # с меньшим корпусом гейт не построить
_RATIO_CV_MAX = 0.20     # связка «A ≈ k×B» живёт при CV отношения ≤ 20%
_CONST_TOL = 0.02        # константа: разброс ≤ 2%
_ERR_OK = 0.25           # правило проходит гейт при медианной ошибке ≤ 25%
_DOC_CAP = 30
_DOC_CHARS = 2500        # текст одного документа в LLM-выжимку


# ── Числовые оси: парсинг таблиц (markdown и 1С-выгрузки) ───────────────

def _num(v: Any) -> Optional[float]:
    from backend.core.ontology.dataset_registry import to_number
    return to_number(v)


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", (s or "").lower()).strip()


# Синонимы колонок 1С/смет → канонические имена (после _norm)
_COL_SYNONYMS = {
    "наименование": "номенклатура", "товар": "номенклатура",
    "позиция": "номенклатура", "материал": "номенклатура",
    "кол во": "количество", "кол": "количество", "колич": "количество",
    "цена за ед": "цена", "цена руб": "цена", "стоимость ед": "цена",
    "ед изм": "ед", "единица": "ед", "единица измерения": "ед",
    "ед измерения": "ед",
    "сумма руб": "сумма", "итого руб": "сумма", "стоимость": "сумма",
}

# Единицы: (базовая единица, множитель к базе)
_UNITS = {
    "г": ("кг", 0.001), "гр": ("кг", 0.001), "грамм": ("кг", 0.001),
    "кг": ("кг", 1.0), "т": ("кг", 1000.0), "тонн": ("кг", 1000.0),
    "тонна": ("кг", 1000.0), "тн": ("кг", 1000.0),
    "мм": ("м", 0.001), "см": ("м", 0.01), "м": ("м", 1.0),
    "км": ("м", 1000.0), "м2": ("м2", 1.0), "м3": ("м3", 1.0),
    "шт": ("шт", 1.0), "штук": ("шт", 1.0), "компл": ("компл", 1.0),
    "упак": ("упак", 1.0), "уп": ("упак", 1.0), "л": ("л", 1.0),
    "час": ("час", 1.0), "ч": ("час", 1.0), "чел час": ("час", 1.0),
}

_QTY_COLS = ("количество", "вес", "объем", "объём", "масса", "расход")
_PRICE_COLS = ("цена", "тариф", "ставка", "расценка")


def _unit_parse(cell: str) -> Optional[Tuple[str, float]]:
    u = _norm(cell).replace(".", "")
    return _UNITS.get(u)


def _col_scale(col_norm: str) -> float:
    """Масштаб из заголовка: «сумма тыс руб» → ×1000. Точная семантика
    заголовка, не догадка."""
    if "млн" in col_norm:
        return 1e6
    if "тыс" in col_norm:
        return 1e3
    return 1.0


def _pipe_blocks(text: str) -> List[List[List[str]]]:
    """Блоки подряд идущих |-строк → списки строк-ячеек (позиции колонок
    сохранены). Разделители |---| пропускаются. Понимает и markdown-таблицы,
    и старые xlsx-выгрузки без рамки (a | b | c)."""
    blocks: List[List[List[str]]] = []
    cur: List[List[str]] = []
    for line in (text or "").splitlines():
        s = line.strip()
        is_pipe = s.startswith("|") or s.count(" | ") >= 2
        if is_pipe and re.match(r"^\|[\s:|-]+\|$", s):
            continue  # разделитель markdown
        if is_pipe:
            cur.append([c.strip() for c in s.strip("|").split("|")])
        else:
            if len(cur) >= 2:
                blocks.append(cur)
            cur = []
    if len(cur) >= 2:
        blocks.append(cur)
    return blocks


def _find_header(block: List[List[str]]) -> Optional[int]:
    """Настоящая шапка: 1С-выгрузки начинаются с названия отчёта/периода —
    ищем первую строку с ≥2 непустыми нечисловыми ячейками, за которой
    идёт строка с числами. Бонус за знакомые имена колонок."""
    for i, row in enumerate(block[:10]):
        cells = [c for c in row if c]
        nonnum = [c for c in cells if _num(c) is None]
        if len(cells) < 2 or len(nonnum) < 2:
            continue
        known = sum(1 for c in cells
                    if _COL_SYNONYMS.get(_norm(c), _norm(c)) in
                    ("номенклатура", "количество", "цена", "сумма", "ед",
                     "контрагент", "себестоимость", "канал", "бюджет"))
        has_data_below = any(
            any(_num(c) is not None for c in r)
            for r in block[i + 1:i + 4])
        if has_data_below and (known >= 2 or len(nonnum) >= 3
                               or len(nonnum) == len(cells)):
            return i
    return 0 if block else None


def normalize_table(block: List[List[str]]) -> List[Dict[str, str]]:
    """Блок ячеек → строки {колонка: значение}: шапка найдена (мусорные
    строки 1С выше отброшены), колонки переименованы по синонимам,
    строки «итого/всего» выброшены."""
    hi = _find_header(block)
    if hi is None:
        return []
    raw_headers = block[hi]
    headers = []
    for j, h in enumerate(raw_headers):
        n = _norm(h)
        headers.append(_COL_SYNONYMS.get(n, n) or f"col{j}")
    rows: List[Dict[str, str]] = []
    for r in block[hi + 1:]:
        d = {headers[k]: (r[k] if k < len(r) else "")
             for k in range(len(headers))}
        joined = _norm(" ".join(v for v in d.values() if v))
        if joined.startswith(("итого", "всего")) or not joined:
            continue
        rows.append(d)
    return rows


def parse_md_tables(text: str) -> List[List[Dict[str, str]]]:
    """Все таблицы документа (markdown и 1С-выгрузки) нормализованными
    строками {колонка: ячейка}."""
    out = []
    for block in _pipe_blocks(text):
        rows = normalize_table(block)
        if rows:
            out.append(rows)
    return out


def _label_col(rows: List[Dict[str, str]]) -> Optional[str]:
    """Колонка-подпись строки: первая, где большинство значений не числа
    (колонка единиц измерения подписью не считается)."""
    if not rows:
        return None
    for col in rows[0].keys():
        if col == "ед":
            continue
        vals = [r.get(col, "") for r in rows]
        nonnum = sum(1 for v in vals if v and _num(v) is None)
        if nonnum >= max(1, len(vals) // 2):
            return col
    return None


def collect_numeric_points(docs: List[dict]) -> Tuple[
        Dict[Tuple[str, str], List[Tuple[int, float]]],
        Dict[Tuple[str, str], str], List[str]]:
    """(метка строки, колонка) → [(индекс документа, значение)] + единица
    базы по ключу + честные заметки.

    Единицы: количества приводятся к базе (т → кг ×1000), цены — к цене
    за базу (₽/т → ₽/кг ÷1000); суммы от единицы не зависят. Смешение
    НЕсовместимых единиц (шт и кг) в одном ключе → ключ выбрасывается с
    заметкой, не гадаем."""
    points: Dict[Tuple[str, str], List[Tuple[int, float]]] = {}
    units: Dict[Tuple[str, str], set] = {}
    notes: List[str] = []
    for di, doc in enumerate(docs):
        for table in parse_md_tables(doc.get("content") or ""):
            lc = _label_col(table)
            has_unit = "ед" in table[0]
            for row in table:
                label = _norm(row.get(lc, "")) if lc else ""
                if not label or label.startswith(("итог", "всего")):
                    continue
                uf = _unit_parse(row.get("ед", "")) if has_unit else None
                for col, cell in row.items():
                    if col == lc or col == "ед":
                        continue
                    n = _num(cell)
                    if n is None or n == 0:
                        continue
                    n *= _col_scale(col)
                    base = ""
                    if uf:
                        base, factor = uf
                        if any(q in col for q in _QTY_COLS):
                            n *= factor
                        elif any(p in col for p in _PRICE_COLS):
                            n /= factor
                        else:
                            base = ""  # суммы и пр. — от единицы не зависят
                    key = (label, col)
                    points.setdefault(key, []).append((di, n))
                    if base:
                        units.setdefault(key, set()).add(base)

    unit_of: Dict[Tuple[str, str], str] = {}
    for key, bases in units.items():
        if len(bases) == 1:
            unit_of[key] = next(iter(bases))
        else:
            label, col = key
            notes.append(f"«{label}» / {col}: смешаны несовместимые единицы "
                         f"({', '.join(sorted(bases))}) — правило не строим")
            points.pop(key, None)
    return points, unit_of, notes


def _median_err(preds: List[Tuple[float, float]]) -> Optional[float]:
    """Медианная относительная ошибка [(предсказание, факт)]."""
    errs = [abs(p - a) / abs(a) for p, a in preds if a]
    return statistics.median(errs) if errs else None


def induce_numeric(docs: List[dict], train_idx: List[int],
                   heldout_idx: List[int]) -> dict:
    """Числовые правила кодом: якоря-константы и связки-отношения,
    каждое — через гейт предсказанием на held-out против базлайна."""
    points, unit_of, unit_notes = collect_numeric_points(docs)
    if not points:
        return {"rules": [], "discarded": [], "notes": unit_notes,
                "has_numbers": False}
    train_set, held_set = set(train_idx), set(heldout_idx)
    rules: List[dict] = []
    discarded: List[str] = []

    # Базлайн по колонке: медиана колонки по train (наивнее правила)
    col_train_vals: Dict[str, List[float]] = {}
    for (label, col), pts in points.items():
        col_train_vals.setdefault(col, []).extend(
            v for di, v in pts if di in train_set)

    # 1) Якоря-константы: (метка, колонка) стабильна между документами
    for (label, col), pts in sorted(points.items()):
        tr = [v for di, v in pts if di in train_set]
        hd = [(di, v) for di, v in pts if di in held_set]
        if len(tr) < 3:
            continue
        med = statistics.median(tr)
        if med == 0:
            continue
        spread = (max(tr) - min(tr)) / abs(med)
        if spread > _CONST_TOL * 4:  # не константа даже близко
            continue
        unit = unit_of.get((label, col), "")
        u_sfx = f" (за {unit})" if unit else ""
        rule = {
            "kind": "anchor",
            "rule": f"«{label}» / {col}: устойчивое значение ≈ {med:g}{u_sfx}",
            "frame": f"диапазон в примерах {min(tr):g}–{max(tr):g}"
                     + (f"; база единиц: {unit}" if unit else ""),
            "origin": f"{len(tr)} документов обучения",
            "check": None,
            "target": {"type": "anchor", "label": label, "col": col,
                       "value": round(med, 6), "unit": unit},
        }
        if hd:
            err = _median_err([(med, v) for _, v in hd])
            base = statistics.median(col_train_vals.get(col) or tr)
            base_err = _median_err([(base, v) for _, v in hd])
            if err is None or err > _ERR_OK or (base_err is not None
                                                and err >= base_err):
                discarded.append(
                    f"{rule['rule']} — не прошло гейт (ошибка на held-out "
                    f"{err:.0%} против базлайна {base_err:.0%})"
                    if err is not None and base_err is not None
                    else f"{rule['rule']} — не прошло гейт")
                continue
            rule["trust"] = round(max(0.0, 1 - err), 2)
            rule["gate"] = (f"held-out {len(hd)} знач.: ошибка {err:.0%} "
                            f"(базлайн {base_err:.0%})" if base_err is not None
                            else f"held-out {len(hd)} знач.: ошибка {err:.0%}")
        else:
            rule["trust"] = 0.5
            rule["gate"] = "без гейта (мало документов) — пилот"
        rules.append(rule)

    # 2) Связки-отношения: колонка A ≈ k × колонка B (внутри строки)
    #    Пары значений в одной строке одного документа.
    row_vals: Dict[Tuple[int, str], Dict[str, float]] = {}
    for (label, col), pts in points.items():
        for di, v in pts:
            row_vals.setdefault((di, label), {})[col] = v
    cols = sorted({c for (_, c) in points.keys()})
    for a in cols:
        for b in cols:
            if a >= b:
                continue
            pairs = [(di, vals[a], vals[b]) for (di, _), vals in row_vals.items()
                     if a in vals and b in vals and vals[b]]
            tr = [(x, y) for di, x, y in pairs if di in train_set]
            hd = [(x, y) for di, x, y in pairs if di in held_set]
            if len(tr) < 5:
                continue
            ratios = [x / y for x, y in tr if y]
            k = statistics.median(ratios)
            if k == 0:
                continue
            cv = (statistics.pstdev(ratios) / abs(statistics.mean(ratios))
                  if len(ratios) > 1 and statistics.mean(ratios) else 1.0)
            if cv > _RATIO_CV_MAX:
                continue
            rule = {
                "kind": "ratio",
                "rule": f"{a} ≈ {k:.3g} × {b}",
                "frame": f"стабильно в {len(tr)} строках обучения (CV {cv:.0%})",
                "origin": "связка колонок в примерах",
                "check": None,
                "target": {"type": "ratio", "col_a": a, "col_b": b,
                           "k": round(k, 6)},
            }
            if hd:
                err = _median_err([(k * y, x) for x, y in hd])
                base_a = statistics.median([x for x, _ in tr])
                base_err = _median_err([(base_a, x) for x, _ in hd])
                if err is None or err > _ERR_OK or (base_err is not None
                                                    and err >= base_err):
                    discarded.append(
                        f"{rule['rule']} — не прошло гейт"
                        + (f" (ошибка {err:.0%}, базлайн {base_err:.0%})"
                           if err is not None and base_err is not None else ""))
                    continue
                rule["trust"] = round(max(0.0, 1 - err), 2)
                rule["gate"] = (f"held-out {len(hd)} строк: ошибка {err:.0%}"
                                + (f" (базлайн {base_err:.0%})"
                                   if base_err is not None else ""))
            else:
                rule["trust"] = 0.5
                rule["gate"] = "без гейта (мало документов) — пилот"
            rules.append(rule)

    # 3) Сигнатура округлений — как носитель считает (кэш арифметики)
    all_vals = [v for pts in points.values() for _, v in pts]
    big = [v for v in all_vals if abs(v) >= 100]
    if len(big) >= 8:
        for step in (1000, 500, 100):
            share = sum(1 for v in big if v % step == 0) / len(big)
            if share >= 0.6:
                rules.append({
                    "kind": "signature",
                    "rule": f"числа кратны {step} в {share:.0%} случаев — "
                            "прикидка блоками, не попозиционный счёт",
                    "frame": "сигнатура стиля, не формула",
                    "origin": f"{len(big)} чисел корпуса", "check": None,
                    "trust": round(share, 2), "gate": "описательная статистика",
                })
                break
    return {"rules": rules, "discarded": discarded, "notes": unit_notes,
            "has_numbers": True}


# ── Структурно-текстовые оси: LLM извлекает, код гейтит ─────────────────

async def _llm_json(llm, prompt: str) -> Any:
    return await llm.generate_json(prompt=prompt, temperature=0.2)


async def extract_structures(docs: List[dict], llm, genre: str) -> List[dict]:
    """LLM-экстракция структуры каждого документа (секции по порядку,
    формат, стиль). Один вызов на пачку — не по вызову на документ."""
    parts = []
    for i, d in enumerate(docs[:_DOC_CAP]):
        parts.append(f"### DOC {i}: {d.get('title') or ''}\n"
                     + (d.get("content") or "")[:_DOC_CHARS])
    prompt = (
        f"Ниже {len(parts)} документов жанра «{genre}». Для КАЖДОГО извлеки "
        "структуру. Ответь ТОЛЬКО JSON-списком той же длины и порядка:\n"
        '[{"doc": 0, "sections": ["секции/смысловые блоки по порядку"], '
        '"format": "форма подачи (лонгрид/карусель/таблица/слайды/...)", '
        '"style": {"words": число_слов_примерно, "cta_count": число_призывов}, '
        '"moves": ["характерные приёмы автора"]}]\n'
        "НЕ выдумывай секций, которых нет.\n\n" + "\n\n".join(parts))
    data = await _llm_json(llm, prompt)
    out: List[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                out.append(item)
    return out


async def propose_structural_rules(structures: List[dict], llm,
                                   genre: str) -> List[dict]:
    """LLM формулирует правила-гипотезы по обучающим структурам. Каждое
    правило обязано иметь check — да/нет-вопрос, проверяемый на одном
    документе (иначе его не загейтить)."""
    prompt = (
        f"Структуры {len(structures)} документов жанра «{genre}» "
        "(извлечены из реальных работ компании):\n"
        + "\n".join(str(s) for s in structures[:20])
        + "\n\nСформулируй 3–8 правил, КАК эта компания делает такие "
        "документы. Ответь ТОЛЬКО JSON-списком:\n"
        '[{"rule": "правило (что и в каком порядке/когда)", '
        '"frame": "когда применимо / когда НЕ применимо", '
        '"check": "да/нет-вопрос для проверки правила на ОДНОМ документе"}]\n'
        "Правило без проверяемого check не пиши. Не выдумывай того, чего "
        "нет в структурах.")
    data = await _llm_json(llm, prompt)
    rules = []
    if isinstance(data, list):
        for r in data:
            if isinstance(r, dict) and r.get("rule") and r.get("check"):
                rules.append({"kind": "structural",
                              "rule": str(r["rule"])[:300],
                              "frame": str(r.get("frame") or "")[:200],
                              "check": str(r["check"])[:200],
                              "origin": "LLM-гипотеза по структурам обучения"})
    return rules[:8]


async def gate_structural_rules(rules: List[dict], heldout_docs: List[dict],
                                llm) -> Tuple[List[dict], List[str]]:
    """Гейт: держится ли правило на отложенных документах. Один LLM-вызов:
    матрица правило × документ → доля «держится» = trust."""
    if not rules:
        return [], []
    if not heldout_docs:
        for r in rules:
            r["trust"] = 0.5
            r["gate"] = "без гейта (мало документов) — пилот"
        return rules, []
    docs_part = "\n\n".join(
        f"### DOC {i}: {(d.get('title') or '')}\n"
        + (d.get("content") or "")[:_DOC_CHARS]
        for i, d in enumerate(heldout_docs[:8]))
    checks = "\n".join(f'{i}. {r["check"]}' for i, r in enumerate(rules))
    prompt = (
        "Проверь правила на документах (это отложенная проверка, отвечай "
        "строго по тексту, не додумывай). Ответь ТОЛЬКО JSON: "
        '{"matrix": [[true/false на каждый вопрос] на каждый документ]} — '
        f"строк {min(len(heldout_docs), 8)}, столбцов {len(rules)}.\n\n"
        f"ВОПРОСЫ:\n{checks}\n\nДОКУМЕНТЫ:\n{docs_part}")
    data = await _llm_json(llm, prompt)
    matrix = data.get("matrix") if isinstance(data, dict) else None
    kept: List[dict] = []
    discarded: List[str] = []
    n_docs = min(len(heldout_docs), 8)
    for ri, r in enumerate(rules):
        votes = []
        if isinstance(matrix, list):
            for row in matrix[:n_docs]:
                if isinstance(row, list) and ri < len(row):
                    votes.append(bool(row[ri]))
        if not votes:
            r["trust"] = 0.5
            r["gate"] = "гейт не отработал — пометка «шатко»"
            kept.append(r)
            continue
        share = sum(votes) / len(votes)
        r["trust"] = round(share, 2)
        r["gate"] = f"held-out {len(votes)} док.: держится в {share:.0%}"
        if share >= 0.5:
            kept.append(r)
        else:
            discarded.append(f"{r['rule']} — держится лишь в {share:.0%} "
                             "отложенных документов")
    return kept, discarded


# ── Сборка: документ-методология ────────────────────────────────────────

def _split(n: int) -> Tuple[List[int], List[int]]:
    """Train/held-out по хвосту. Меньше _MIN_GATE_DOCS — всё в train."""
    if n < _MIN_GATE_DOCS:
        return list(range(n)), []
    k = max(1, round(n * _HELDOUT_SHARE))
    return list(range(n - k)), list(range(n - k, n))


def render_methodology_md(*, genre: str, title: str, n_docs: int,
                          n_heldout: int, status: str, rules: List[dict],
                          discarded: List[str], skeleton: List[str],
                          notes: List[str]) -> str:
    md = [f"# {title}", "",
          f"**Жанр:** {genre} · **корпус:** {n_docs} документов "
          f"(held-out: {n_heldout}) · **статус:** {status}", ""]
    if status.startswith("пилот"):
        md += ["> ⚠️ Пар меньше 10 — это ПИЛОТ ГИПОТЕЗ, не методология. "
               "Правила не считать истиной, добирать примеры.", ""]
    if skeleton:
        md += ["## Скелет (общее ядро)", ""]
        md += [f"{i+1}. {s}" for i, s in enumerate(skeleton)]
        md += [""]
    if rules:
        md += ["## Правила (прошли гейт предсказанием)", ""]
        for r in rules:
            md += [f"### {r['rule']}",
                   f"- **Рамка:** {r.get('frame') or '—'}",
                   f"- **Происхождение:** {r.get('origin') or '—'}",
                   f"- **Trust:** {r.get('trust', 0)} · {r.get('gate', '')}",
                   ""]
    else:
        md += ["## Правила", "", "_Ни одно правило не прошло гейт — "
               "это результат (null — тоже данные). Нужно больше примеров "
               "или другие оси._", ""]
    if discarded:
        md += ["## Выброшено (не прошло гейт)", ""]
        md += [f"- {d}" for d in discarded]
        md += [""]
    if notes:
        md += ["## Ограничения честно", ""]
        md += [f"- {n}" for n in notes]
        md += [""]
    md += ["---", "_Методология — candidate, не истина: новые работы против "
           "неё питают trust; серия провалов правила → ревизия рамки._"]
    return "\n".join(md)


async def build_methodology(user_id: str, *, document_ids: List[str],
                            genre: str, title: str = "",
                            llm=None,
                            doc_loader: Optional[Callable] = None) -> dict:
    """Полный конвейер: документы → числовые + структурные правила →
    гейт → markdown-методология + сохранение (документ + заготовка)."""
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    if doc_loader is None:
        import functools
        from backend.core.analysis.run_engine import _default_document_loader
        doc_loader = functools.partial(_default_document_loader,
                                       owner_user_id=user_id)

    docs: List[dict] = []
    for did in document_ids[:_DOC_CAP]:
        try:
            d = await doc_loader(str(did))
        except Exception:
            d = None
        if d and (d.get("content") or "").strip():
            docs.append(d)
    if len(docs) < 2:
        return {"success": False,
                "error": "нужно минимум 2 документа с содержимым "
                         f"(загружено {len(docs)})"}

    genre = (genre or "документы").strip()[:60]
    title = (title or f"Методология: {genre}").strip()[:120]
    train_idx, held_idx = _split(len(docs))
    status = ("методология (с гейтом)" if len(docs) >= _MIN_PAIRS and held_idx
              else "пилот гипотез (мало пар)")
    notes: List[str] = []
    if not held_idx:
        notes.append(f"документов {len(docs)} < {_MIN_GATE_DOCS} — гейт "
                     "предсказанием не строился, все правила «шаткие»")
    elif len(docs) < _MIN_PAIRS:
        notes.append(f"пар {len(docs)} < {_MIN_PAIRS} — держим статус "
                     "«пилот», добирайте примеры")
    notes.append("самоотчёты автора не использовались — только артефакты "
                 "(мета-слой ≠ процедура)")

    # Числовые оси — код
    num = induce_numeric(docs, train_idx, held_idx)
    rules: List[dict] = list(num["rules"])
    discarded: List[str] = list(num["discarded"])
    notes.extend(num.get("notes") or [])

    # Структурные оси — LLM извлекает, код агрегирует, LLM-гейт на held-out
    skeleton: List[str] = []
    try:
        train_docs = [docs[i] for i in train_idx]
        held_docs = [docs[i] for i in held_idx]
        structures = await extract_structures(train_docs, llm, genre)
        if structures:
            # скелет: секции в ≥60% документов, порядок — средняя позиция
            pos: Dict[str, List[int]] = {}
            for s in structures:
                for p, sec in enumerate(s.get("sections") or []):
                    pos.setdefault(_norm(str(sec)), []).append(p)
            common = {k: v for k, v in pos.items()
                      if len(v) >= max(2, int(0.6 * len(structures)))}
            skeleton = [k for k, _ in sorted(
                common.items(), key=lambda kv: statistics.mean(kv[1]))][:12]
            proposed = await propose_structural_rules(structures, llm, genre)
            kept, disc = await gate_structural_rules(proposed, held_docs, llm)
            rules += kept
            discarded += disc
    except Exception as e:
        logger.warning(f"structural induction failed: {e}")
        notes.append(f"структурный анализ не отработал: {e}")

    md = render_methodology_md(
        genre=genre, title=title, n_docs=len(docs), n_heldout=len(held_idx),
        status=status, rules=rules, discarded=discarded,
        skeleton=skeleton, notes=notes)

    # Сохраняем как сгенерированный документ (папка «Методологии»)
    document_id = None
    try:
        from backend.core.documents.document_writer_agent import GeneratedDocument
        from backend.core.documents import doc_store
        now = datetime.utcnow()
        doc = GeneratedDocument(
            document_id=f"method_{uuid.uuid4().hex[:10]}",
            title=title, document_type="methodology", version="1.0",
            created_at=now, updated_at=now,
            summary=f"Методология «{genre}» из {len(docs)} примеров: "
                    f"{len(rules)} правил, статус: {status}",
            content_markdown=md, content_html="",
            topic=genre, keywords=[genre, "методология", "морм"],
            source_meetings=[], status="draft",
            # machine-readable правила — для пере-гейта по дрейфу
            sections=[{"kind": "morm_rules", "genre": genre,
                       "rules": rules}],
            confidence=round(statistics.mean(
                [r.get("trust", 0.5) for r in rules]), 2) if rules else 0.0,
            word_count=len(md.split()), user_id=user_id)
        doc.folder = "Методологии"
        await doc_store.save_document(doc, user_id)
        document_id = doc.document_id
    except Exception as e:
        logger.warning(f"methodology doc save failed: {e}")

    # И как заготовку — чтобы медиаплан/заполнение подхватывали галочкой
    # (upsert по названию: пересборка обновляет, а не плодит дубли)
    preset_id = None
    try:
        from backend.core.documents.fill_engine import (
            list_context_presets, save_context_preset)
        compact = "\n".join(
            f"- {r['rule']} [{r.get('frame') or 'без рамки'}; "
            f"trust {r.get('trust', 0)}]" for r in rules[:15])
        if compact:
            p_title = f"Методология: {genre}"[:120]
            existing = next((p for p in list_context_presets(user_id)
                             if p.get("title") == p_title), None)
            rec = save_context_preset(
                user_id, title=p_title,
                text=f"МЕТОДОЛОГИЯ КОМПАНИИ (жанр: {genre}, {status}):\n"
                     + compact,
                preset_id=existing.get("id") if existing else None)
            preset_id = rec.get("id")
    except Exception as e:
        logger.warning(f"methodology preset save failed: {e}")

    return {"success": True, "document_id": document_id,
            "preset_id": preset_id, "status": status, "genre": genre,
            "rules": rules, "discarded": discarded, "skeleton": skeleton,
            "markdown": md, "docs_used": len(docs),
            "heldout_docs": len(held_idx)}


# ── Пере-гейт по дрейфу (МОРМ §8: методология обязана дрейфовать) ────────

def _regate_numeric_rule(rule: dict, points, unit_of) -> Optional[dict]:
    """Проверить якорь/связку на свежих точках. None — данных нет."""
    t = rule.get("target") or {}
    if t.get("type") == "anchor":
        pts = points.get((t.get("label"), t.get("col"))) or []
        if not pts:
            return None
        vals = [v for _, v in pts]
        err = _median_err([(t.get("value") or 0, v) for v in vals])
        return {"err": err, "n": len(vals),
                "suggest": round(statistics.median(vals), 6)}
    if t.get("type") == "ratio":
        a, b = t.get("col_a"), t.get("col_b")
        row_vals: Dict[Tuple[int, str], Dict[str, float]] = {}
        for (label, col), pts in points.items():
            for di, v in pts:
                row_vals.setdefault((di, label), {})[col] = v
        pairs = [(vals[a], vals[b]) for vals in row_vals.values()
                 if a in vals and b in vals and vals[b]]
        if not pairs:
            return None
        k = t.get("k") or 0
        err = _median_err([(k * y, x) for x, y in pairs])
        ks = [x / y for x, y in pairs if y]
        return {"err": err, "n": len(pairs),
                "suggest": round(statistics.median(ks), 6)}
    return None


async def regate_methodology(user_id: str, *, methodology_document_id: str,
                             document_ids: List[str], llm=None,
                             doc_loader: Optional[Callable] = None) -> dict:
    """Пере-гейт методологии на СВЕЖИХ примерах: цены и процессы дрейфуют —
    правила перепроверяются предсказанием. Дрейфанувшие правила НЕ стираются
    (ревизия рамок, не забвение) — помечаются 🔴 с предложением нового
    значения; trust обновляется; версия документа растёт."""
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    if doc_loader is None:
        import functools
        from backend.core.analysis.run_engine import _default_document_loader
        doc_loader = functools.partial(_default_document_loader,
                                       owner_user_id=user_id)
    from backend.core.documents import doc_store

    row = await doc_store.get_document(methodology_document_id, user_id)
    if not row:
        return {"success": False, "error": "методология не найдена"}
    sec = next((s for s in (row.get("sections") or [])
                if isinstance(s, dict) and s.get("kind") == "morm_rules"), None)
    if not sec or not sec.get("rules"):
        return {"success": False,
                "error": "в документе нет machine-readable правил — "
                         "пересоберите методологию («🧬 Из примеров»)"}
    rules = [dict(r) for r in sec["rules"]]
    genre = sec.get("genre") or row.get("topic") or "документы"

    docs = []
    for did in document_ids[:_DOC_CAP]:
        try:
            d = await doc_loader(str(did))
        except Exception:
            d = None
        if d and (d.get("content") or "").strip():
            docs.append(d)
    if not docs:
        return {"success": False, "error": "нет свежих документов для проверки"}

    points, unit_of, _ = collect_numeric_points(docs)
    verdicts: List[dict] = []
    struct_rules = [r for r in rules if r.get("check")]

    for r in rules:
        if r.get("check"):
            continue  # структурные — ниже, пачкой
        if r.get("kind") == "signature":
            verdicts.append({"rule": r["rule"], "verdict": "не проверяется",
                             "detail": "описательная сигнатура"})
            continue
        res = _regate_numeric_rule(r, points, unit_of)
        if res is None or res.get("err") is None:
            verdicts.append({"rule": r["rule"], "verdict": "нет данных",
                             "detail": "в свежих документах нет этой оси"})
            continue
        err, n = res["err"], res["n"]
        drifted = err > _ERR_OK
        r["trust"] = round(max(0.0, 1 - err), 2)
        r["drift"] = drifted
        r["gate"] = f"пере-гейт {n} знач.: ошибка {err:.0%}"
        verdicts.append({
            "rule": r["rule"],
            "verdict": "дрейф" if drifted else "держится",
            "detail": f"ошибка {err:.0%} на {n} свежих значениях"
                      + (f"; свежая медиана: {res['suggest']:g} — кандидат "
                         "на новое значение" if drifted else "")})

    if struct_rules:
        try:
            checked, _disc = await gate_structural_rules(
                [dict(r) for r in struct_rules], docs, llm)
            by_rule = {c["rule"]: c for c in checked}
            for r in rules:
                if not r.get("check"):
                    continue
                c = by_rule.get(r["rule"])
                share = c.get("trust", 0.0) if c else 0.0
                drifted = share < 0.5
                r["trust"] = share
                r["drift"] = drifted
                r["gate"] = f"пере-гейт: держится в {share:.0%} свежих док."
                verdicts.append({
                    "rule": r["rule"],
                    "verdict": "дрейф" if drifted else "держится",
                    "detail": f"держится в {share:.0%} свежих документов"})
        except Exception as e:
            logger.warning(f"structural regate failed: {e}")
            verdicts.append({"rule": "(структурные правила)",
                             "verdict": "нет данных",
                             "detail": f"LLM-гейт не отработал: {e}"})

    drifted_rules = [v for v in verdicts if v["verdict"] == "дрейф"]
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    add_md = [f"\n\n## Пере-гейт {stamp} ({len(docs)} свежих документов)", ""]
    for v in verdicts:
        icon = {"держится": "✅", "дрейф": "🔴",
                "нет данных": "⚪", "не проверяется": "◽"}[v["verdict"]]
        add_md.append(f"- {icon} {v['rule']} — {v['verdict']}: {v['detail']}")
    if drifted_rules:
        add_md.append("\n> 🔴 Дрейфанувшие правила не удалены — обновите "
                      "значение/рамку или пересоберите методологию с новыми "
                      "примерами.")

    # Обновляем документ: версия +0.1, правила с новым trust, markdown-хвост
    try:
        from backend.core.documents.document_writer_agent import GeneratedDocument
        try:
            ver = f"{float(row.get('version') or 1.0) + 0.1:.1f}"
        except (TypeError, ValueError):
            ver = "1.1"
        now = datetime.utcnow()

        def _dt(v):
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")
                                              ).replace(tzinfo=None)
            except (TypeError, ValueError):
                return now
        doc = GeneratedDocument(
            document_id=row["document_id"], title=row.get("title") or "",
            document_type="methodology", version=ver,
            created_at=_dt(row.get("created_at")), updated_at=now,
            summary=(row.get("summary") or "")[:400]
                    + f" · пере-гейт {stamp}: дрейф {len(drifted_rules)}",
            content_markdown=(row.get("content_markdown") or "")
                             + "\n".join(add_md),
            content_html="", topic=row.get("topic") or genre,
            keywords=row.get("keywords") or [],
            source_meetings=row.get("source_meetings") or [],
            sections=[{"kind": "morm_rules", "genre": genre, "rules": rules}],
            status=row.get("status") or "draft",
            confidence=round(statistics.mean(
                [r.get("trust", 0.5) for r in rules]), 2) if rules else 0.0,
            word_count=len((row.get("content_markdown") or "").split()),
            user_id=user_id)
        doc.folder = row.get("folder") or "Методологии"
        await doc_store.save_document(doc, user_id)
    except Exception as e:
        logger.warning(f"regate doc update failed: {e}")

    # Обновляем заготовку (upsert по названию, не плодим дубли)
    try:
        from backend.core.documents.fill_engine import (
            list_context_presets, save_context_preset)
        p_title = f"Методология: {genre}"[:120]
        existing = next((p for p in list_context_presets(user_id)
                         if p.get("title") == p_title), None)
        compact = "\n".join(
            f"- {'🔴 ДРЕЙФ: ' if r.get('drift') else ''}{r['rule']} "
            f"[{r.get('frame') or 'без рамки'}; trust {r.get('trust', 0)}]"
            for r in rules[:15])
        if compact:
            save_context_preset(
                user_id, title=p_title,
                text=f"МЕТОДОЛОГИЯ КОМПАНИИ (жанр: {genre}, пере-гейт "
                     f"{stamp}):\n{compact}",
                preset_id=existing.get("id") if existing else None)
    except Exception as e:
        logger.warning(f"regate preset update failed: {e}")

    return {"success": True, "document_id": methodology_document_id,
            "verdicts": verdicts, "drifted": len(drifted_rules),
            "docs_used": len(docs),
            "markdown": "\n".join(add_md).strip()}
