# -*- coding: utf-8 -*-
"""
Движок запросов к датасету — «палантир-модуль», часть 2.

«Система пишет код для анализа» сделано БЕЗОПАСНО: LLM не пишет
произвольный код — она (дешёвая модель) переводит вопрос в СТРУКТУРНЫЙ
ПЛАН (op/column/group_by/filters), план валидируется против онтологии
колонок и исполняется детерминированным кодом. Считает код, LLM только
формулирует. Без LLM работает фолбэк-парсер типовых вопросов.

Оценка результата (skeleton, честно): сколько строк реально учтено,
сколько пустых пропущено, заземлена ли колонка в контексте компании
(known KPI / сущность из графа), нет ли подозрений к данным. Ответ —
не «новый вывод», а цифры с контекстной оценкой.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from backend.core.ontology.dataset_registry import to_number, unit_label

OPS = ("count", "sum", "avg", "min", "max", "distinct", "top", "group_sum",
       "group_count", "group_avg", "list")

_FILTER_OPS = ("eq", "ne", "gt", "lt", "gte", "lte", "contains")


def join_rows(left: List[dict], right: List[dict],
              left_on: str, right_on: str) -> tuple[List[dict], List[str], int]:
    """LEFT JOIN двух таблиц по нормализованному ключу (строка, lower, trim).

    Количество строк слева сохраняется (при нескольких совпадениях справа
    берётся первое — предсказуемость важнее полноты, это отражается в
    notes). Возвращает (строки, добавленные колонки, сколько строк слева
    нашли пару). Коллизии имён колонок получают суффикс «__2»."""
    def key(v: Any) -> str:
        return str(v if v is not None else "").strip().lower()

    index: Dict[str, dict] = {}
    for r in right:
        k = key(r.get(right_on))
        if k and k not in index:
            index[k] = r

    left_cols = set(left[0].keys()) if left else set()
    added_cols: List[str] = []
    rename: Dict[str, str] = {}
    if right:
        for c in right[0].keys():
            if c == right_on:
                continue
            name = c if c not in left_cols else f"{c}__2"
            rename[c] = name
            added_cols.append(name)

    merged: List[dict] = []
    matched = 0
    for r in left:
        row = dict(r)
        hit = index.get(key(r.get(left_on)))
        if hit is not None:
            matched += 1
            for c, name in rename.items():
                row[name] = hit.get(c)
        merged.append(row)
    return merged, added_cols, matched


def validate_plan(plan: dict, columns: List[str]) -> tuple[Optional[dict], List[str]]:
    """План против онтологии: операция из списка, колонки существуют.
    Возвращает (чистый план | None, ошибки)."""
    errors: List[str] = []
    if not isinstance(plan, dict):
        return None, ["план не dict"]
    op = str(plan.get("op", "")).lower()
    if op not in OPS:
        errors.append(f"op должен быть из {OPS}")
    colset = {c.lower(): c for c in columns}

    def _col(name: Any) -> Optional[str]:
        if name in (None, ""):
            return None
        c = colset.get(str(name).strip().lower())
        if c is None:
            errors.append(f"колонки «{name}» нет в датасете")
        return c

    clean: dict = {"op": op,
                   "column": _col(plan.get("column")),
                   "group_by": _col(plan.get("group_by")),
                   "limit": max(1, min(int(plan.get("limit") or 10), 100)),
                   "filters": []}
    if op != "count" and op != "list" and not clean["column"]:
        errors.append("для этой операции нужна column")
    if op.startswith("group_") and not clean["group_by"]:
        errors.append("для group_* нужна group_by")
    for f in (plan.get("filters") or [])[:5]:
        col = _col((f or {}).get("column"))
        fop = str((f or {}).get("op", "eq")).lower()
        if fop not in _FILTER_OPS:
            errors.append(f"фильтр-op {fop} не поддержан")
            continue
        if col:
            clean["filters"].append({"column": col, "op": fop,
                                     "value": (f or {}).get("value")})
    return (None, errors) if errors else (clean, [])


def parse_question_fallback(question: str, columns: List[str]) -> Optional[dict]:
    """Детерминированный разбор типовых вопросов (работает без LLM):
    «сколько строк», «сумма/среднее/максимум/минимум <колонка>»,
    «<агрегат> <колонка> по <колонка>», «топ N по <колонка>»."""
    q = (question or "").lower()
    colset = {c.lower(): c for c in columns}

    def find_col(text: str) -> Optional[str]:
        for lc in sorted(colset, key=len, reverse=True):
            if lc in text:
                return colset[lc]
        return None

    if re.search(r"сколько (строк|запис|всего)", q):
        return {"op": "count"}
    m = re.search(r"(сумм|средн|сколько)\w*\s+(.+?)\s+по\s+месяц", q)
    if m:
        col = find_col(m.group(2))
        if col:
            op = {"сумм": "group_sum", "средн": "group_avg"}.get(
                m.group(1), "group_count")
            return {"op": op, "column": col, "group_by": "__month__"}
    m = re.search(r"топ[- ]?(\d+)?\s*по\s+(.+)", q)
    if m:
        col = find_col(m.group(2))
        if col:
            return {"op": "top", "column": col,
                    "limit": int(m.group(1) or 10)}
    agg_map = {"сумм": "sum", "средн": "avg", "максим": "max",
               "миним": "min", "уникальн": "distinct", "сколько": "count"}
    for word, op in agg_map.items():
        if word in q:
            # агрегируемая колонка ищется ДО «по …», группировка — после
            head, _, tail = q.partition(" по ")
            col = find_col(head.replace(word, "", 1))
            if col:
                group = find_col(tail) if tail else None
                if group and group != col and op in ("sum", "avg", "count"):
                    return {"op": f"group_{op}", "column": col,
                            "group_by": group}
                return {"op": op, "column": col}
    return None


def execute_plan(plan: dict, rows: List[dict],
                 date_columns: Optional[set] = None) -> dict:
    """Исполнить валидированный план. Возвращает {result, rows_used,
    nulls_skipped}. group_by по date-колонке → ключ месяца YYYY-MM
    (динамика по времени)."""
    date_columns = date_columns or set()
    data = rows
    for f in plan.get("filters") or []:
        col, fop, val = f["column"], f["op"], f.get("value")
        fnum = to_number(val)

        def keep(r: dict, col=col, fop=fop, val=val, fnum=fnum) -> bool:
            cell = r.get(col)
            cnum = to_number(cell)
            if fop == "contains":
                return str(val or "").lower() in str(cell or "").lower()
            if fnum is not None and cnum is not None:
                a, b = cnum, fnum
            else:
                a, b = str(cell or ""), str(val or "")
            return {"eq": a == b, "ne": a != b, "gt": a > b,
                    "lt": a < b, "gte": a >= b, "lte": a <= b}[fop]
        data = [r for r in data if keep(r)]

    op, col = plan["op"], plan.get("column")
    out: dict = {"rows_used": len(data), "nulls_skipped": 0}

    def nums() -> List[float]:
        vals = [to_number(r.get(col)) for r in data]
        out["nulls_skipped"] = sum(1 for v in vals if v is None)
        return [v for v in vals if v is not None]

    if op == "count":
        out["result"] = len(data)
    elif op == "distinct":
        out["result"] = len({str(r.get(col)) for r in data
                             if r.get(col) not in (None, "")})
    elif op in ("sum", "avg", "min", "max"):
        ns = nums()
        if not ns:
            out["result"] = None
        elif op == "sum":
            out["result"] = round(sum(ns), 4)
        elif op == "avg":
            out["result"] = round(sum(ns) / len(ns), 4)
        else:
            out["result"] = (min if op == "min" else max)(ns)
    elif op == "top":
        ranked = sorted(
            ((r, to_number(r.get(col))) for r in data),
            key=lambda t: (t[1] is None, -(t[1] or 0)))
        out["result"] = [
            {**{k: r.get(k) for k in list(r)[:6]}, "_value": v}
            for r, v in ranked[:plan["limit"]]]
    elif op.startswith("group_"):
        agg = op.split("_")[1]
        groups: Dict[str, List[float]] = {}
        counts: Dict[str, int] = {}
        by_month = plan["group_by"] in date_columns
        for r in data:
            key = str(r.get(plan["group_by"]) or "—")
            if by_month:
                key = key[:7]        # 2026-05-12 → 2026-05
            counts[key] = counts.get(key, 0) + 1
            n = to_number(r.get(col)) if col else None
            if n is not None:
                groups.setdefault(key, []).append(n)
        keyfn = (lambda kv: kv[0]) if by_month else (lambda kv: -kv[1])
        if agg == "count":
            out["result"] = dict(sorted(counts.items(), key=keyfn)[:50])
        else:
            res = {}
            for k, ns in groups.items():
                res[k] = round(sum(ns) if agg == "sum" else sum(ns) / len(ns), 4)
            out["result"] = dict(sorted(res.items(), key=keyfn)[:50])
    else:  # list
        out["result"] = data[:plan["limit"]]
    return out


def assess_result(plan: dict, exec_out: dict, dataset: dict) -> dict:
    """Честная оценка цифры (skeleton, без LLM): покрытие, заземление
    колонок в контексте компании, свежесть, аномалии."""
    notes: List[str] = []
    profile = dataset.get("profile") or {}
    rows_total = profile.get("rows_total", 0)
    used = exec_out.get("rows_used", 0)
    skipped = exec_out.get("nulls_skipped", 0)
    coverage = round(used / rows_total, 3) if rows_total else 0.0
    if plan.get("filters") and used == 0:
        notes.append("под фильтры не попала ни одна строка — проверь значения")
    if skipped:
        notes.append(f"пропущено пустых/нечисловых ячеек: {skipped}")

    # смысловые ловушки цифр (проценты/дубли/смешанные единицы)
    by_col = {p["name"]: p for p in profile.get("columns", [])}
    col_prof = by_col.get(plan.get("column") or "", {})
    if (col_prof.get("dtype") == "percent"
            and plan.get("op") in ("sum", "group_sum")):
        notes.append(f"колонка «{plan['column']}» — проценты: их СУММА "
                     "почти всегда бессмысленна, используй среднее")
    if col_prof.get("unit_warning"):
        notes.append(f"в «{plan['column']}» огромный разброс величин — "
                     "возможно смешаны единицы (млн рядом с абсолютными)")
    # Деньги без подтверждённой единицы: не гадаем, просим уточнить —
    # клик по колонке в «Онтологии» → корректировка (валюта/масштаб).
    if col_prof.get("money_like"):
        notes.append(f"«{plan['column']}» похоже на деньги, но валюта/масштаб "
                     "не подтверждены (₽ или $? тысячи или млн?) — уточни "
                     "корректировкой колонки, чтобы цифры сравнивались верно")
    dup = profile.get("duplicate_rows", 0)
    if dup and plan.get("op") in ("sum", "count", "group_sum", "group_count"):
        notes.append(f"в таблице {dup} полностью одинаковых строк — "
                     "сумма/количество могут быть задвоены")

    grounding = (dataset.get("ontology") or {}).get("grounding", {})
    involved = [c for c in (plan.get("column"), plan.get("group_by")) if c]
    grounded, unknown = [], []
    for c in involved:
        g = grounding.get(c) or {}
        if g.get("known"):
            tag = f"{c}→{g.get('entity_type') or g.get('kpi') or '?'}"
            if g.get("corrected_by_user"):
                tag += " (подтверждено вами)"
            grounded.append(tag)
        else:
            unknown.append(c)
    if grounded:
        notes.append("колонки узнаны в контексте компании: " + ", ".join(grounded))
    if unknown:
        notes.append("колонки НЕ привязаны к известным сущностям: "
                     + ", ".join(unknown) + " — цифра верна по таблице, "
                     "но бизнес-смысл не подтверждён")
    refreshed = dataset.get("refreshed_at", "")
    return {
        "coverage": coverage,
        "grounded_columns": grounded,
        "ungrounded_columns": unknown,
        "unit": unit_label(col_prof) or None,   # «млн ₽» — для показа цифры
        "data_as_of": refreshed,
        "confidence": ("high" if coverage >= 0.8 and not unknown
                       else "medium" if coverage >= 0.5 else "low"),
        "notes": notes,
    }
