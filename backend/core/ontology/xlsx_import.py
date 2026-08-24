# -*- coding: utf-8 -*-
"""Excel-импорт: .xlsx → (columns, rows) БЕЗ зависимостей — парный модуль
к xlsx_export (тот же принцип: минимальный OOXML через stdlib zipfile +
xml.etree). Пользователи живут в Excel — просить «пересохраните в CSV»
нельзя.

Поддержано: первый лист, sharedStrings, inline-строки, числа (остаются
числами!), даты Excel (serial → ISO, если ячейка в date-формате — по
эвристике styles пропускаем; serial-даты чаще приходят текстом). Формулы
берутся по закешированному значению <v>. Кап строк — MAX_ROWS_STORED.
"""
from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import List, Optional
from xml.etree import ElementTree as ET

from backend.core.ontology.dataset_registry import MAX_CELL_LEN, MAX_ROWS_STORED

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(letters: str) -> int:
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out: List[str] = []
    for si in root.findall(f"{_NS}si"):
        # <t> напрямую или rich-text из нескольких <r><t>
        out.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    return out


def _first_sheet_path(zf: zipfile.ZipFile) -> Optional[str]:
    names = zf.namelist()
    for cand in ("xl/worksheets/sheet1.xml",):
        if cand in names:
            return cand
    sheets = sorted(n for n in names
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    return sheets[0] if sheets else None


def parse_xlsx(content: bytes) -> tuple[List[str], List[dict]]:
    """.xlsx (bytes) → (columns, rows) в том же виде, что parse_tabular:
    первая строка — заголовки, числа — float. Пустой/битый файл → ([], [])."""
    try:
        zf = zipfile.ZipFile(BytesIO(content))
    except (zipfile.BadZipFile, ValueError):
        return [], []
    sheet = _first_sheet_path(zf)
    if not sheet:
        return [], []
    shared = _shared_strings(zf)
    try:
        root = ET.fromstring(zf.read(sheet))
    except (KeyError, ET.ParseError):
        return [], []

    table: List[List] = []
    for row_el in root.iter(f"{_NS}row"):
        if len(table) > MAX_ROWS_STORED:      # +1 на заголовок
            break
        cells: dict[int, object] = {}
        for c in row_el.findall(f"{_NS}c"):
            m = _REF_RE.match(c.get("r") or "")
            ci = _col_index(m.group(1)) if m else len(cells)
            ctype = c.get("t") or "n"
            if ctype == "inlineStr":
                cells[ci] = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                continue
            v = c.find(f"{_NS}v")
            if v is None or v.text is None:
                continue
            if ctype == "s":                  # shared string
                try:
                    cells[ci] = shared[int(v.text)]
                except (ValueError, IndexError):
                    cells[ci] = v.text
            elif ctype in ("str", "b"):
                cells[ci] = v.text
            else:                             # число (или кеш формулы)
                try:
                    num = float(v.text)
                    cells[ci] = int(num) if num.is_integer() else num
                except ValueError:
                    cells[ci] = v.text
        if cells:
            width = max(cells) + 1
            table.append([cells.get(i) for i in range(width)])

    table = [r for r in table
             if any(v not in (None, "") for v in r)]
    if len(table) < 2:
        return [], []
    header = table[0]
    cols = [str(c).strip()[:80] if c not in (None, "") else f"col{i}"
            for i, c in enumerate(header)]
    rows: List[dict] = []
    for raw in table[1:MAX_ROWS_STORED + 1]:
        row = {}
        for i, col in enumerate(cols):
            v = raw[i] if i < len(raw) else None
            if isinstance(v, str):
                v = v[:MAX_CELL_LEN]
            row[col] = v
        rows.append(row)
    return cols, rows
