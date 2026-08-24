# -*- coding: utf-8 -*-
"""
table-aware-indexing — C10: таблицы в документах индексируются с пониманием
их СВЯЗИ С КОМПАНИЕЙ (через TableUnderstandingAgent + prior из снапшота).

ЗАЧЕМ. Сейчас document_indexer кладёт всё в Document/Chunk без понимания.
Финансовая таблица «Q3 2024 KPI» индексируется как обычный текст — поиск
«сколько мы заработали в Q3» не знает, что это были НАШИ KPI компании.
С table-aware: при детекте табличного контента прогоняем через скелет (типы
колонок + привязка значений к узлам графа компании) и кладём результат В
МЕТАДАННЫЕ чанков — поиск/синтез теперь видит, что «эти цифры — наши, эти
имена — наши люди».

ДИЗАЙН (скелет в коде; LLM только если граф заземлил):
- detect_tabular_blocks: эвристика markdown-таблиц и CSV-like (детерминированно);
- parse_markdown_table: первая строка — columns, остальные — rows; чистый stdlib;
- enrich_chunk_metadata_with_table_understanding: запускает скелет
  TableUnderstandingAgent.profile_columns + ground (БЕЗ LLM на этом этапе —
  достаточно скелет-данных) и кладёт в метаданные:
    tabular: bool, is_known: bool, confidence: float,
    column_types: [...], matched_entity_types: [...]
- ЧЕСТНОСТЬ §1: не нашли таблицу / скелет не заземлил → метаданные не
  обогащаются (не выдумываем «понимание»).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# markdown-таблица: строка вида | col | col | col |, шапка-разделитель | --- |
_MD_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$")
_MD_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}\s*(?:\|\s*:?-{2,}\s*)+\|?\s*$")


def detect_tabular_blocks(content: str) -> List[Tuple[int, int]]:
    """Найти границы (start_line, end_line_inclusive) табличных блоков
    в тексте. Поддерживает: markdown-таблицы (с разделителем `|---|`).

    Возвращает список (i, j) — индексы первой и последней строки таблицы.
    Чистый детект, без LLM."""
    if not content:
        return []
    lines = content.splitlines()
    out: List[Tuple[int, int]] = []
    i = 0
    while i < len(lines) - 2:
        if (_MD_TABLE_LINE.match(lines[i]) and
                _MD_SEPARATOR.match(lines[i + 1])):
            j = i + 2
            while j < len(lines) and _MD_TABLE_LINE.match(lines[j]):
                j += 1
            out.append((i, j - 1))
            i = j
            continue
        i += 1
    return out


def _split_md_row(line: str) -> List[str]:
    """| a | b | c | → ['a', 'b', 'c']."""
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def parse_markdown_table(content: str, start: int, end: int) -> Optional[Dict[str, Any]]:
    """Извлечь columns/rows из MD-таблицы (start..end включительно).
    Возвращает None при невалидной структуре."""
    lines = content.splitlines()[start:end + 1]
    if len(lines) < 3:
        return None
    columns = _split_md_row(lines[0])
    if not columns:
        return None
    rows: List[List[str]] = []
    for ln in lines[2:]:
        cells = _split_md_row(ln)
        if len(cells) == len(columns):
            rows.append(cells)
    if not rows:
        return None
    return {"columns": columns, "rows": rows}


def _normalize_prior(prior: Dict[str, Any]) -> Dict[str, set]:
    """match_column_to_prior ждёт значения в lowercase (так делает _norm
    внутри table_understanding_agent). Вызывающие модуль передают prior как
    есть из снапшота — нормализуем для них, чтобы не дублировать знание."""
    out: Dict[str, set] = {}
    for k, names in (prior or {}).items():
        out[k] = {str(n).strip().lower() for n in (names or []) if n}
    return out


def summarize_understanding(
    columns: List[str], rows: List[List[str]], prior: Dict[str, Any],
) -> Dict[str, Any]:
    """Прогнать скелет TableUnderstandingAgent (БЕЗ LLM — нам достаточно
    структурного вердикта) и собрать сводку для метаданных.

    Не зовём async-метод understand; используем чистые функции профиля/ground."""
    # Импортируем по пути файла — обход think/__init__ → autogen в окружениях
    # без ag2 (тесты). Но если пакет уже импортируется нормально в проде —
    # пытаемся обычным импортом первым.
    try:
        from backend.core.think.table_understanding_agent import (
            assess_grounding,
            infer_column_type,
            match_column_to_prior,
        )
    except Exception:
        import importlib.util as ilu
        import os as _os
        import sys as _sys
        path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "think", "table_understanding_agent.py")
        spec = ilu.spec_from_file_location("_tessent_tua", path)
        mod = ilu.module_from_spec(spec)
        _sys.modules["_tessent_tua"] = mod
        spec.loader.exec_module(mod)
        assess_grounding = mod.assess_grounding
        infer_column_type = mod.infer_column_type
        match_column_to_prior = mod.match_column_to_prior

    norm_prior = _normalize_prior(prior)

    col_values: Dict[str, list] = {c: [] for c in columns}
    for r in rows:
        for i, c in enumerate(columns):
            col_values[c].append(r[i] if i < len(r) else None)

    column_types: List[str] = []
    matched_entity_types: List[str] = []
    for c in columns:
        ctype = infer_column_type(col_values[c])
        etype, _examples = match_column_to_prior(col_values[c], norm_prior)
        if etype:
            column_types.append("entity_ref")
            matched_entity_types.append(etype)
        else:
            column_types.append(ctype)

    # сборка profile-объектов для assess_grounding (он принимает их форму)
    class _P:
        def __init__(self, name, inferred_type, matched_entity_type):
            self.name = name
            self.inferred_type = inferred_type
            self.matched_entity_type = matched_entity_type
            self.sample_values = []

    profiles = [
        _P(c, column_types[i],
           matched_entity_types[i] if i < len(matched_entity_types) else None)
        for i, c in enumerate(columns)
    ]
    # assess_grounding(profiles, prior) -> (confidence, is_known)
    confidence, is_known = assess_grounding(profiles, norm_prior)

    return {
        "tabular": True,
        "is_known": bool(is_known),
        "confidence": round(float(confidence), 4),
        "column_types": column_types,
        "matched_entity_types": sorted(set(matched_entity_types)),
        "n_columns": len(columns),
        "n_rows": len(rows),
    }


def enrich_chunk_metadata(
    chunk_text: str,
    chunk_metadata: Dict[str, Any],
    *,
    prior: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Если чанк содержит таблицу — обогатить метаданные пониманием.
    Возвращает НОВЫЙ словарь (не мутирует вход). Если таблицы нет / парс
    не удался — возвращает chunk_metadata без изменений."""
    base = dict(chunk_metadata or {})
    blocks = detect_tabular_blocks(chunk_text or "")
    if not blocks:
        return base
    # Берём ПЕРВУЮ таблицу в чанке (для MVP). Множественные таблицы — TODO.
    start, end = blocks[0]
    table = parse_markdown_table(chunk_text, start, end)
    if not table:
        return base
    summary = summarize_understanding(table["columns"], table["rows"], prior or {})
    base["table_understanding"] = summary
    return base
