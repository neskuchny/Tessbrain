# -*- coding: utf-8 -*-
"""
Glue: Universal Entity Dossier для реального пользователя (tenant-пути).

dossier_for_user(user_id, entity_name) — собирает досье из per-user сторов
памяти (mentions/events/facts). Каждый стор подключается best-effort: нет
файла/упал импорт → источник честно отсутствует в providers (не 'error',
а просто не заявлен) ЛИБО даёт 'error' при падении чтения — сборка не падает
никогда. Внешние источники (граф, persona, снапшоты) добавляются через
extra_providers без правки этого модуля.

Конвенция ключей: mentions пишутся как "{user_id}::{name}"
(mention_ingestion.entity_key) — учитываем оба варианта ключа.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from backend.core.memory.entity_dossier import (
    EntityDossier,
    build_dossier,
    default_providers,
)

logger = logging.getLogger(__name__)


def _try_store(cls_path: str, path_fn_name: str, user_id: str):
    """Инстанс стора по tenant-пути; None если недоступен (best-effort)."""
    try:
        import importlib

        from backend.core.store import tenant_paths
        module_name, cls_name = cls_path.rsplit(".", 1)
        mod = importlib.import_module(module_name)
        cls = getattr(mod, cls_name)
        path = getattr(tenant_paths, path_fn_name)(user_id)
        return cls(persist_path=path)
    except Exception as e:
        logger.debug("dossier store unavailable: %s (%s)", cls_path, e)
        return None


def _auto_extra_providers(user_id: str, entity_name: str,
                          entity_type: str) -> Dict[str, Callable[[], List[dict]]]:
    """C12: автоматически добавить provider'ов из графа/persona/снапшотов
    (best-effort, любая ошибка → провайдер пропущен).

    Имена ключей совпадают с entity_dossier ('graph', 'persona', 'snapshot')
    — чтобы их можно было перекрыть из явного extra_providers вызывающим.
    """
    extra: Dict[str, Callable[[], List[dict]]] = {}
    try:
        from backend.core.memory.entity_dossier_providers import (
            _run_sync,
            build_extra_providers,
        )
    except Exception as e:
        logger.debug("extra providers module unavailable: %s", e)
        return extra

    # Граф (per-user)
    graph_builder = None
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        graph_builder = GraphBuilder(
            use_networkx=None,
            graph_storage_path=graph_path_for_user(user_id),
        )
        # ИСПРАВЛЕНИЕ аудита F3: для NetworkX-бэкенда граф грузится с диска
        # ТОЛЬКО в connect() — без него nx_graph=None и провайдер всегда пуст.
        # _run_sync безопасен в любом контексте (см. F5).
        _run_sync(graph_builder.connect())
    except Exception as e:
        logger.debug("graph_builder unavailable: %s", e)
        graph_builder = None

    # Persona (per-user)
    persona_obj = None
    try:
        from backend.core.persona.store import get_persona_store
        store = get_persona_store()
        # F5-fix: loop-безопасный раннер вместо голого asyncio.run
        persona_obj = _run_sync(store.get(user_id))
    except Exception as e:
        logger.debug("persona unavailable: %s", e)
        persona_obj = None

    # Snapshot generator (per-user; уже singleton по graph_builder)
    snapshot_generator = None
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        snapshot_generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
    except Exception as e:
        logger.debug("snapshot generator unavailable: %s", e)
        snapshot_generator = None

    try:
        extra = build_extra_providers(
            graph_builder=graph_builder,
            persona_obj=persona_obj,
            snapshot_generator=snapshot_generator,
            entity_name=entity_name,
            entity_type=entity_type,
        )
    except Exception as e:
        logger.debug("build_extra_providers failed: %s", e)
    return extra


def dossier_for_user(
    user_id: str,
    entity_name: str,
    *,
    entity_type: str = "unknown",
    extra_providers: Optional[Dict[str, Callable[[], List[dict]]]] = None,
) -> EntityDossier:
    """Досье сущности по всем доступным слоям памяти пользователя.

    C12: к памяти (mentions/events/facts) автоматически подключаются
    структурные источники — граф, persona, snapshots — best-effort.
    Явные extra_providers, переданные caller'ом, перекрывают авто-ключи.
    """
    mentions = _try_store("backend.core.memory.cross_source_mentions.CrossSourceMentions",
                          "cross_source_mentions_path_for_user", user_id)
    events = _try_store("backend.core.memory.event_log.EventLog",
                        "event_log_path_for_user", user_id)
    facts = _try_store("backend.core.memory.supersede_store.SupersedeStore",
                       "supersede_store_path_for_user", user_id)

    entity_key = f"{user_id}::{entity_name}"  # конвенция mention_ingestion

    # Авто + явные extra: явные перекрывают авто (caller всегда прав)
    auto_extra = _auto_extra_providers(user_id, entity_name, entity_type)
    if extra_providers:
        auto_extra.update(extra_providers)

    providers = default_providers(
        mentions_store=mentions, events_store=events, facts_store=facts,
        entity_key=entity_key, entity_name=entity_name, extra=auto_extra,
    )
    return build_dossier(entity_name, providers, entity_type=entity_type)
