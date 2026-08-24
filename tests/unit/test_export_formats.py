# -*- coding: utf-8 -*-
"""Мультиформатный экспорт документа: markdown → pptx/xlsx (+ docx как база).

Реальная генерация (python-pptx / openpyxl / python-docx уже в requirements),
без заглушек. PDF тут не проверяем — он опционален (fpdf2/LibreOffice) и может
вернуть None на CI, что штатно (роут отдаёт понятную ошибку 503)."""
from __future__ import annotations

import io
import zipfile

from backend.core.analysis.export import (
    markdown_to_docx_bytes,
    markdown_to_pptx_bytes,
    markdown_to_xlsx_bytes,
    _markdown_tables_to_sheets,
)

MD = """# Коммерческое предложение

## Тарифная сетка
| Объём | Тариф |
|---|---|
| 10 001 – 100 000 мин | 2,2 ₽/мин |
| до 10 000 мин | 37 200 ₽/мес |

## Расчёт
- 12 000 минут × 2,2 = 26 400 ₽/мес
- **Итог:** 22 000 – 26 400 ₽/мес
"""


def _is_zip(b: bytes) -> bool:
    return isinstance(b, bytes) and b[:4] == b"PK\x03\x04"


def test_pptx_bytes_valid_and_has_slides() -> None:
    b = markdown_to_pptx_bytes(MD, title="КП")
    assert b and _is_zip(b), "pptx должен быть валидным zip-контейнером"
    names = zipfile.ZipFile(io.BytesIO(b)).namelist()
    slides = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert len(slides) >= 2, f"ожидались титул + разделы, получено {len(slides)}"


def test_xlsx_bytes_valid_and_table_parsed() -> None:
    b = markdown_to_xlsx_bytes(MD, title="КП")
    assert b and _is_zip(b), "xlsx должен быть валидным zip"
    # markdown-таблица распознана как лист с колонками
    sheets = _markdown_tables_to_sheets(MD)
    assert any(s.get("columns") == ["Объём", "Тариф"] for s in sheets)
    assert any(len(s.get("rows") or []) >= 2 for s in sheets)


def test_docx_bytes_valid() -> None:
    b = markdown_to_docx_bytes(MD, title="КП")
    assert b and _is_zip(b)


def test_no_tables_still_makes_one_sheet() -> None:
    sheets = _markdown_tables_to_sheets("# Заголовок\n\nПросто текст без таблиц")
    assert len(sheets) == 1 and sheets[0]["rows"], "без таблиц — один лист с текстом"


def test_xlsx_survives_control_chars() -> None:
    # Регрессия: управляющие символы (\x00, ESC) в тексте роняли openpyxl
    # (IllegalCharacterError). Должны вырезаться, файл — собраться.
    bad = "# Отчёт\n\n| a\x00b | c\x1bd |\n|---|---|\n| 1\x1f | 2\x08 |"
    b = markdown_to_xlsx_bytes(bad, title="t")
    assert b and _is_zip(b), "xlsx должен собраться, вырезав управляющие символы"
