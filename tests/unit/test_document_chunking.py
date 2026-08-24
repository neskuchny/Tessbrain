# -*- coding: utf-8 -*-
"""Регрессия чанкинга документов: короткий «хвост» не должен плодить сотни
почти одинаковых чанков (баг: 5 КБ pptx → 205 чанков)."""
from __future__ import annotations

from backend.core.documents.document_indexer import split_text_into_chunks


def _multiline_text(n: int = 60) -> str:
    return "\n".join(
        f"--- Slide {i+1} ---\nЗаголовок {i+1}\nПункт про цену {i*100} руб"
        for i in range(n))


def test_short_tail_does_not_explode_chunks() -> None:
    text = _multiline_text(60)               # ~3.2 КБ, много коротких строк
    chunks = split_text_into_chunks(text, chunk_size=1500, overlap=200)
    # раньше выходило ~198; здоровое число — единицы
    assert len(chunks) <= 6, f"слишком много чанков: {len(chunks)}"
    assert len(chunks) >= 2


def test_chunks_cover_full_text_monotonic() -> None:
    text = _multiline_text(40)
    chunks = split_text_into_chunks(text, chunk_size=400, overlap=52)
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(text)
    # старты строго возрастают (нет топтания на месте)
    starts = [c["char_start"] for c in chunks]
    assert all(b > a for a, b in zip(starts, starts[1:]))


def test_text_shorter_than_chunk_is_single() -> None:
    assert len(split_text_into_chunks("привет мир", 1500, 200)) == 1


def test_large_doc_reasonable_chunk_count() -> None:
    text = "Абзац с несколькими предложениями. " * 500   # ~17.5 КБ
    chunks = split_text_into_chunks(text, chunk_size=1500, overlap=200)
    # ~17500 / (1500-200) ≈ 13; допускаем запас, но не сотни
    assert 8 <= len(chunks) <= 25, f"неожиданное число чанков: {len(chunks)}"
