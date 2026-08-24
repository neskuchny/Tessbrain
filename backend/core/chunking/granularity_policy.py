# -*- coding: utf-8 -*-
"""
Granularity policy — контекст-зависимая гранулярность чанков
(MEMORY_DESIGN_PRINCIPLES §2, §6 №6).

Зачем: «не один размер на всех». Чат, документ и встреча — разные контексты
с разной природой ценности:
- Чат:     инкрементальные мелкие сообщения; ценность в ближайшем контексте.
- Документ: большое окно, ценность в широком контексте — не резать как чат.
- Встреча:  посередине — диалог во времени.

Этот модуль НЕ изменяет существующие splitter'ы (ChunkingService,
SemanticSplitter). Он отдаёт ПАРАМЕТРЫ (chunk_size, overlap), которые caller
передаёт в существующий splitter своего выбора. Тонкий, тестируемый,
backward-compatible. Дефолт `document` совпадает с DEFAULT_CHUNK_SIZE=1500 в
text_splitter — продукт не меняет поведение, пока caller не запросил иной
профиль.

Дизайн-инварианты (тестируется):
- Дефолтный профиль (document) совпадает с прежним DEFAULT_CHUNK_SIZE.
- Неизвестный context_type → дефолт (никогда не падает).
- chat < meeting < document по chunk_size (порядок по природе контекстов).
- overlap пропорционален chunk_size (10-15%) — overlap не «съедает» чанк.
- Pure stdlib, чисто данные/dataclass — без LLM/deps.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Канонические типы контекста (любая строка валидна; неизвестная → дефолт).
CONTEXT_CHAT = "chat"
CONTEXT_DOCUMENT = "document"
CONTEXT_MEETING = "meeting"
DEFAULT_CONTEXT = CONTEXT_DOCUMENT


@dataclass(frozen=True)
class ChunkProfile:
    """Параметры чанкинга для одного контекста."""
    chunk_size: int          # символов
    overlap: int             # символов (% от chunk_size)
    min_chunk_size: int      # ниже не дробить
    rationale: str           # человекочитаемое «почему такой размер»

    def as_kwargs(self) -> dict:
        """Готовые kwargs для ChunkingService/SemanticSplitter."""
        return {"chunk_size": self.chunk_size, "overlap": self.overlap}


# Профили — подобраны с оглядкой на природу контекста.
# document.chunk_size=1500 совпадает с DEFAULT_CHUNK_SIZE в text_splitter →
# дефолт продукта НЕ меняется, пока caller явно не запросит другой профиль.
_PROFILES: dict = {
    CONTEXT_CHAT: ChunkProfile(
        chunk_size=400, overlap=60, min_chunk_size=80,
        rationale="Чат: реплики коротки, ценность в ближайшем контексте — "
                  "мелкие чанки лучше для пер-сообщенческого recall.",
    ),
    CONTEXT_MEETING: ChunkProfile(
        chunk_size=900, overlap=120, min_chunk_size=150,
        rationale="Встреча: диалог во времени, нужна и реплика, и тема — средний "
                  "размер, чтобы не рвать аргумент пополам.",
    ),
    CONTEXT_DOCUMENT: ChunkProfile(
        chunk_size=1500, overlap=200, min_chunk_size=200,
        rationale="Документ: ценность в широком контексте; крупный размер, "
                  "совпадает с DEFAULT_CHUNK_SIZE — не меняет дефолтное поведение.",
    ),
}


def get_profile(context_type: Optional[str]) -> ChunkProfile:
    """Профиль чанкинга по типу контекста. Неизвестный → дефолт (document)."""
    key = (context_type or DEFAULT_CONTEXT).lower().strip()
    return _PROFILES.get(key, _PROFILES[DEFAULT_CONTEXT])


def chunk_kwargs(context_type: Optional[str], *, override_chunk_size: Optional[int] = None,
                 override_overlap: Optional[int] = None) -> dict:
    """Готовые kwargs для существующих splitter'ов.

    override_*: явные значения от caller'а перебивают политику (для краевых
    случаев — настройка под конкретный документ). Без override — берётся профиль.
    """
    p = get_profile(context_type)
    cs = override_chunk_size if override_chunk_size is not None else p.chunk_size
    ov = override_overlap if override_overlap is not None else p.overlap
    if cs <= 0:
        raise ValueError(f"chunk_size должен быть > 0, получено {cs}")
    if ov < 0 or ov >= cs:
        raise ValueError(f"overlap={ov} вне диапазона [0, chunk_size={cs})")
    return {"chunk_size": cs, "overlap": ov}


def supported_contexts() -> list:
    """Список поддерживаемых типов контекста (для UI/конфига)."""
    return sorted(_PROFILES.keys())
