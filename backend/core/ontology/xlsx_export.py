# -*- coding: utf-8 -*-
"""
Excel-экспорт (Cowork-перенос C2, docs/GLEAN_ADOPTION.md): датасет/отчёт
в .xlsx БЕЗ зависимостей — минимальный валидный OOXML через stdlib
zipfile (openpyxl в проде не обязателен). Детерминированно, без LLM.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any, List
from xml.sax.saxutils import escape

_MAX_ROWS = 10000


def _col_letter(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(r: int, c: int, value: Any) -> str:
    ref = f"{_col_letter(c)}{r}"
    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    # Живая формула: "=SUM(B2:B5)" → Excel пересчитывает при изменении ячеек
    if isinstance(value, str) and value.startswith("="):
        return f'<c r="{ref}"><f>{escape(value[1:])}</f></c>'
    s = escape(str(value if value is not None else ""))
    # запрещённые в XML управляющие символы
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
            f"{s}</t></is></c>")


def rows_to_xlsx(columns: List[str], rows: List[dict],
                 sheet_name: str = "Данные") -> bytes:
    """(columns, rows[dict]) → байты .xlsx с одним листом.
    Числа пишутся числами (Excel сможет суммировать), остальное — текст."""
    from backend.core.ontology.dataset_registry import to_number
    sheet_name = re.sub(r"[\\/?*\[\]:]", " ", sheet_name)[:31] or "Данные"

    body = ["<row r=\"1\">"]
    for c, col in enumerate(columns):
        body.append(_cell(1, c, col))
    body.append("</row>")
    for i, row in enumerate(rows[:_MAX_ROWS], start=2):
        body.append(f'<row r="{i}">')
        for c, col in enumerate(columns):
            v = row.get(col)
            n = to_number(v)
            body.append(_cell(i, c, n if n is not None else v))
        body.append("</row>")
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>'
             + "".join(body) + "</sheetData></worksheet>")

    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/'
          'spreadsheetml/2006/main" xmlns:r="http://schemas.'
          'openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" '
          'r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/'
               'package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.'
               'openxmlformats.org/officeDocument/2006/relationships/'
               'worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats'
            '.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/'
             '2006/content-types">'
             '<Default Extension="rels" ContentType="application/'
             'vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType='
             '"application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.sheet.main+xml"/>'
             '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
             '"application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.worksheet+xml"/></Types>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
