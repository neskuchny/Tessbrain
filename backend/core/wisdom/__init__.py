"""
Business Wisdom Engine (BWE) — Система корпоративной мудрости Tessbrain

Превращает накопленный корпоративный опыт в оперативную мудрость.
Обучается на встречах, решениях и их исходах, способна быстро
диагностировать новые бизнес-ситуации на основе архитектуры похожих
прошлых паттернов.

Архитектура:
  Слой 0: Tessbrain (сырьё — транскрипты, решения)
  Слой 1: Decision Event Extractor
  Слой 2: Outcome Linker
  Слой 3: JEPA-inspired Pattern Encoder
  Слой 4: Wisdom Index
  Слой 5: Wisdom Query Engine
"""

from backend.core.wisdom.wisdom_engine import WisdomEngine, get_wisdom_engine

__all__ = ["WisdomEngine", "get_wisdom_engine"]
