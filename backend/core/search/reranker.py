# -*- coding: utf-8 -*-
"""Реранкер вторым проходом: пара «запрос-документ» вместо одного вектора.

Зачем. Разбор предела одновекторного поиска (arXiv 2508.21038) назвал
кросс-энкодер одним из выходов: он оценивает пару «запрос-документ»
напрямую, без бутылочного горлышка размерности. Разбор gbrain показал
то же в бою — у них реранкер стоит в продовой цепочке. У нас его не
было: слияние рангов и временной буст переставляют УЖЕ НАЙДЕННОЕ по
уже посчитанным сигналам, а второй проход читает текст заново.

Дисциплина осторожности (в отличие от прежних включений, здесь код
НОВЫЙ и на наших данных не проверенный — поэтому):
- ВЫКЛЮЧЕН по умолчанию: RERANKER_ENABLED=on включает осознанно;
- переставляет, а не фильтрует: набор документов после реранка тот же,
  ни один не выброшен и не добавлен — меняется только порядок;
- best-effort: нет пакета, нет модели, сбой, таймаут → исходный порядок
  без ошибки; модель грузится лениво и один раз на процесс;
- модель локальная (sentence-transformers CrossEncoder) — работает и в
  закрытом контуре; ВАЖНО для air-gap: веса при первом запуске
  скачиваются с HuggingFace — для полной изоляции положите их в образ
  и укажите путь в RERANKER_MODEL (та же оговорка, что у эмбеддингов).

Конфигурация:
  RERANKER_ENABLED   off|on            (default off)
  RERANKER_MODEL     имя или путь      (default BAAI/bge-reranker-base —
                                        многоязычный, русский включён)
  RERANKER_TOP_N     сколько кандидатов читать вторым проходом (default 20)
  RERANKER_TIMEOUT_S предел ожидания   (default 8.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-reranker-base"
DEFAULT_TOP_N = 20
DEFAULT_TIMEOUT_S = 8.0

_MODEL_CACHE: dict = {}


def reranker_enabled() -> bool:
    """Default OFF: второй проход читает top-N текстов моделью на каждый
    запрос — это цена и задержка, включать её надо осознанно."""
    return os.getenv("RERANKER_ENABLED", "off").strip().lower() in (
        "1", "on", "true", "yes")


def _model_name() -> str:
    return os.getenv("RERANKER_MODEL", "").strip() or DEFAULT_MODEL


def _get_model():
    """Ленивая загрузка CrossEncoder, один раз на процесс. None при сбое."""
    name = _model_name()
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(name)
    except Exception as exc:
        logger.warning("reranker: модель недоступна (%s) — порядок прежний",
                       exc)
        model = None
    _MODEL_CACHE[name] = model
    return model


def _score_pairs(query: str, texts: List[str]) -> Optional[List[float]]:
    """Синхронный скоринг пар. None при сбое — вызывающий оставит порядок."""
    model = _get_model()
    if model is None:
        return None
    try:
        scores = model.predict([(query, t) for t in texts])
        return [float(s) for s in scores]
    except Exception:
        logger.warning("reranker: скоринг упал — порядок прежний",
                       exc_info=True)
        return None


async def rerank(
    query: str,
    results: List[Any],
    *,
    text_of: Callable[[Any], str],
    top_n: Optional[int] = None,
    timeout_s: Optional[float] = None,
    scorer: Optional[Callable[[str, List[str]], Optional[List[float]]]] = None,
) -> List[Any]:
    """Переставить top-N результатов вторым проходом. Никогда не raises.

    Гарантии:
    - возвращается ТОТ ЖЕ набор объектов: ничего не выброшено и не
      добавлено, хвост за пределами top-N сохраняет свой порядок;
    - пустой текст у кандидата → он не участвует в скоринге и остаётся
      на своём месте относительно других неоценённых;
    - сбой или таймаут → вход возвращается как есть.

    `scorer` инъектируется в тестах — гарантии проверяются без модели.
    """
    if not query or len(results) < 2:
        return results
    n = top_n if top_n is not None else int(
        os.getenv("RERANKER_TOP_N", str(DEFAULT_TOP_N)))
    t = timeout_s if timeout_s is not None else float(
        os.getenv("RERANKER_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))

    head, tail = list(results[:n]), list(results[n:])
    texts = []
    idx_map = []            # позиция в head → позиция в texts
    for i, r in enumerate(head):
        try:
            txt = (text_of(r) or "").strip()
        except Exception:
            txt = ""
        if txt:
            idx_map.append(i)
            texts.append(txt[:2000])   # энкодеру хватает начала документа
    if len(texts) < 2:
        return results

    score_fn = scorer or _score_pairs
    try:
        loop = asyncio.get_running_loop()
        scores = await asyncio.wait_for(
            loop.run_in_executor(None, score_fn, query, texts), timeout=t)
    except asyncio.TimeoutError:
        logger.info("reranker: не уложился в %.1fс — порядок прежний", t)
        return results
    except Exception:
        logger.warning("reranker failed — порядок прежний", exc_info=True)
        return results
    if not scores or len(scores) != len(texts):
        return results

    # Переставляем ТОЛЬКО оценённые позиции между собой: неоценённые
    # (пустой текст) остаются на своих местах.
    order = sorted(range(len(texts)), key=lambda j: -scores[j])
    reordered = list(head)
    slots = idx_map                      # куда можно ставить
    for slot, j in zip(slots, order):
        reordered[slot] = head[idx_map[j]]
    return reordered + tail


__all__ = ["DEFAULT_MODEL", "rerank", "reranker_enabled"]
