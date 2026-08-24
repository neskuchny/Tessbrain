# -*- coding: utf-8 -*-
"""Политика: какой УРОВЕНЬ модели под какую нагрузку.

Принцип (docs/MODEL_ALLOCATION_STRATEGY.md): размер модели под задачу подбираем
по чувствительности к качеству × частоте/латентности.

  • Тяжёлое, редкое, качество-критичное  → premium (крупные модели):
      анализ встречи, ночная консолидация, докристаллизация, синтез deep-поиска.
      Это разово на встречу/ночь, но задаёт качество ВСЕЙ базы знаний.
  • Частое, латентное, дешёвое           → standard (модели поменьше):
      чат, быстрый/стандартный поиск, recon-шаги deep-поиска.
      Тысячи вызовов/день — тут важны скорость и цена.

Провайдера и «мой ключ» тенант выбирает в ЛК (model_router); УРОВЕНЬ под задачу
берём отсюда. Один провайдер обслуживает оба: напр. Anthropic → анализ на
opus-4.8, чат на sonnet-5. Всё за флагом enable_tiered_extraction."""
from __future__ import annotations

from typing import Any, Dict, Optional

# workload → level. Три уровня: fast (молниеносно, чат/интерактив) ·
# standard (рабочий анализ) · premium (крупная модель, качество-критично).
WORKLOAD_LEVEL: Dict[str, str] = {
    # тяжёлое → premium
    "extraction": "premium",              # анализ встречи
    "nightly": "premium",                 # ночная консолидация (синтез)
    "recrystallize": "premium",           # докристаллизация новых встреч
    "search_deep_synthesis": "premium",   # финальный синтез deep-поиска
    "tz_generation": "premium",           # генерация ТЗ (артефакт, качество-критично)
    # рабочий анализ → standard (deepseek-4-pro на платформе): обычный поиск —
    # это уже РАБОТА с ответом по базе знаний, а не болталка; ему нужен
    # средний уровень, иначе весь ретрив упирается в lite-синтез
    "search_standard": "standard",
    # молниеносный интерактив → fast (flash-lite на платформе)
    "chat": "fast",
    "search_quick": "fast",
    "search_deep_recon": "fast",          # recon-шаги deep-поиска (много, дёшево)
}

HEAVY = {"extraction", "nightly", "recrystallize", "search_deep_synthesis",
         "tz_generation"}


def level_for(workload: str) -> str:
    """'fast' | 'standard' | 'premium' для нагрузки (неизвестная → fast:
    безопаснее по латентности и цене, чем внезапный deepseek на неизвестном пути)."""
    return WORKLOAD_LEVEL.get(workload, "fast")


def is_heavy(workload: str) -> bool:
    """Тяжёлая нагрузка (крупная модель оправдана)."""
    return workload in HEAVY


async def resolve_for_workload(user_id: str, workload: str) -> Dict[str, Any]:
    """Полный маршрут (провайдер тенанта из ЛК + уровень по политике) с готовым
    caller. None-caller → откат на дефолтный LLM для этой нагрузки.

    Пользовательский override уровня (настройка «Модели»: Стандарт/Премиум =
    своя модель со своим ключом, tier_overrides) имеет высший приоритет; сбой
    его клиента → прежняя цепочка (модель платформы, Google-страховка)."""
    level = level_for(workload)
    try:
        from backend.core.llm.tier_overrides import caller_for_level
        caller, provider = caller_for_level(user_id, level)
        if caller is not None:
            return {"caller": caller, "native": provider, "level": level,
                    "override": True}
    except Exception:
        import logging
        logging.getLogger(__name__).debug("tier override skipped", exc_info=True)
    from backend.core.llm.extraction_route import build_extraction_route
    return await build_extraction_route(user_id, level_override=level)


async def _try_generate(caller, prompt: str) -> Optional[str]:
    """Один generate: строка при успехе, None при сбое/пустом. Never-raise, закрывает."""
    if caller is None:
        return None
    try:
        out = await caller.generate(prompt)
        return out if (out and out.strip()) else None
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("workload generate упал: %s", e)
        return None
    finally:
        try:
            await caller.aclose()
        except Exception:
            pass


async def generate_for_workload(user_id: str, workload: str, prompt: str,
                                *, fallback_generate=None) -> Optional[str]:
    """Одиночный вызов под нагрузку (чат/поиск/ночь) с 3-слойной страховкой,
    ЧТОБЫ НИЧЕГО НЕ ПАДАЛО:
      1) модель тенанта из ЛК нужного уровня (нативным API);
      2) при сбое/пустом — платформенный Google (Gemini, уже в проде);
      3) `fallback_generate` — существующий дефолтный генератор вызывающего.

    Адаптация на месте нагрузки — одна строка:
        text = await generate_for_workload(user_id, "chat", prompt,
                                           fallback_generate=self.llm.generate)
    """
    from backend.core.llm.extraction_route import google_fallback_caller

    # Атрибуция расхода: вызыватели флашат токены в usage_tracker при aclose(),
    # а user_id/тип нагрузки берутся из окружающего контекста. Без этого скоупа
    # весь тировый путь (чат/поиск/ночь) писался бы с user_id=NULL и квоты его
    # не видели. Скоупы вкладываемые — внешняя атрибуция (встреча/документ)
    # сохраняется. Пустой user_id не ставим, чтобы не затереть внешний.
    from backend.core.llm.usage_tracker import cost_scope
    _scope_kw = {"request_type": f"workload/{workload}"}
    if user_id:
        _scope_kw["user_id"] = user_id

    async with cost_scope(**_scope_kw):
        route = await resolve_for_workload(user_id, workload)

        out = await _try_generate(route.get("caller"), prompt)      # слой 1
        if out:
            return out
        if route.get("native") != "google":                        # слой 2
            out = await _try_generate(google_fallback_caller(), prompt)
            if out:
                return out
        if fallback_generate is not None:                          # слой 3
            try:
                return await fallback_generate(prompt)
            except Exception as e:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "workload default generate упал: %s", e)
        return None
