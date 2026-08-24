# -*- coding: utf-8 -*-
"""
Data-Driven Task Specification System - Система ТЗ на основе РЕАЛЬНЫХ данных.

ГЛАВНЫЙ ПРИНЦИП: Никаких абстрактных ТЗ! Всё основано на данных компании.

Алгоритм работы:
1. ПОНЯТЬ задачу - что именно нужно сделать
2. ОПРЕДЕЛИТЬ какие данные нужны - цены, планы, расходы, мнения и т.д.
3. ПОИСКАТЬ эти данные - в графах, векторах, снепшотах
4. СОБРАТЬ весь контекст - из разных источников
5. ПРОАНАЛИЗИРОВАТЬ найденное - что нашли, чего не хватает
6. СОЗДАТЬ ТЗ или ВЫПОЛНИТЬ задачу - на основе реальных данных
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

logger = logging.getLogger("task_spec.data_driven")


class DataDrivenTaskSystem:
    """
    🧠 Система генерации ТЗ на основе РЕАЛЬНЫХ данных компании.

    В отличие от абстрактного генератора, эта система:
    - Сначала понимает, какие данные нужны для задачи
    - Ищет эти данные в графах, векторах, снепшотах
    - Собирает мнения, факты, цифры из реальных встреч
    - Создаёт ТЗ только на основе найденного
    - Чётко указывает, что найдено, а что предположено
    """

    def __init__(
        self,
        storage=None,
        llm_router=None,
        output_dir: str | None = None
    ):
        self.storage = storage
        self.llm_router = llm_router
        self.output_dir = Path(output_dir) if output_dir else Path("task_specifications")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Снепшот компании (загружается при первом использовании)
        self._company_snapshot = None

        logger.info("✅ DataDrivenTaskSystem инициализирован")

    async def process_task(
        self,
        task_description: str,
        additional_context: Optional[Dict[str, Any]] = None,
        mode: str = "full"  # full | quick | execute_only
    ) -> Dict[str, Any]:
        """
        Главная функция обработки задачи.

        Args:
            task_description: Описание задачи
            additional_context: Дополнительный контекст от пользователя
            mode: Режим работы (full - полный цикл, quick - быстро, execute_only - только выполнение)

        Returns:
            Результат с ТЗ или выполненной задачей
        """
        task_id = self._generate_task_id()
        start_time = datetime.utcnow()

        # Аудит #2: счётчик сбоев LLM за этот прогон. Раньше пустой/error-ответ
        # LLM не прерывал пайплайн, но status всё равно ставился "completed" —
        # «успешное» ТЗ могло быть пустышкой без фактов. Теперь считаем сбои и
        # маркируем результат честно (completed_degraded).
        self._llm_failures = 0

        logger.info("="*70)
        logger.info(f"🚀 НОВАЯ ЗАДАЧА: {task_description[:100]}...")
        logger.info(f"   ID: {task_id}")
        logger.info(f"   Режим: {mode}")
        logger.info("="*70)

        result = {
            "task_id": task_id,
            "task_description": task_description,
            "status": "processing",
            "stages": {},
            "found_data": {},
            "missing_data": [],
            "assumptions": [],
            "errors": []
        }

        # user_id — для источников за пределами графа (метрики/датасеты)
        self._current_user_id = (additional_context or {}).get("user_id")

        try:
            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 1: ПОНИМАНИЕ ЗАДАЧИ
            # Что нужно сделать? Какой тип задачи? Какие данные понадобятся?
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n📊 ЭТАП 1: Понимание задачи")
            task_understanding = await self._stage_1_understand_task(
                task_description, additional_context
            )
            result["stages"]["understanding"] = task_understanding
            logger.info(f"   ✅ Тип задачи: {task_understanding.get('task_type', 'unknown')}")
            logger.info(f"   ✅ Нужные данные: {len(task_understanding.get('required_data', []))} категорий")

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 2: ПЛАНИРОВАНИЕ ПОИСКА
            # Где искать данные? В каких графах? По каким запросам?
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n🔍 ЭТАП 2: Планирование поиска")
            search_plan = await self._stage_2_plan_search(
                task_understanding, additional_context
            )
            result["stages"]["search_plan"] = search_plan
            logger.info(f"   ✅ Запросов запланировано: {len(search_plan.get('search_queries', []))}")

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 3: СБОР ДАННЫХ
            # Выполняем поиск по графам, векторам, снепшотам
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n📥 ЭТАП 3: Сбор данных")
            collected_data = await self._stage_3_collect_data(search_plan)
            result["stages"]["collected_data"] = collected_data
            result["found_data"] = collected_data.get("categorized_data", {})
            logger.info(f"   ✅ Найдено источников: {collected_data.get('stats', {}).get('total_sources', 0)}")
            logger.info(f"   ✅ Найдено фактов: {collected_data.get('stats', {}).get('total_facts', 0)}")

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 4: АНАЛИЗ ПОЛНОТЫ
            # Что нашли? Чего не хватает? Нужен ли дополнительный поиск?
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n🔎 ЭТАП 4: Анализ полноты данных")
            completeness = await self._stage_4_analyze_completeness(
                task_understanding, collected_data
            )
            result["stages"]["completeness"] = completeness
            result["missing_data"] = completeness.get("missing_data", [])
            logger.info(f"   ✅ Полнота данных: {completeness.get('completeness_score', 0)}%")
            logger.info(f"   ⚠️ Пробелов: {len(completeness.get('gaps', []))}")

            # Если данных мало - дополнительный поиск
            if completeness.get("completeness_score", 0) < 50 and mode == "full":
                logger.info("\n🔄 ЭТАП 4.5: Дополнительный поиск")
                additional_data = await self._stage_4_5_additional_search(
                    completeness, task_understanding
                )
                # Объединяем данные
                collected_data = self._merge_data(collected_data, additional_data)
                result["stages"]["collected_data"] = collected_data
                result["found_data"] = collected_data.get("categorized_data", {})

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 5: СБОРКА КОНТЕКСТА
            # Структурируем найденное: факты, мнения, цифры, предположения
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n📋 ЭТАП 5: Сборка контекста")
            context_assembly = await self._stage_5_assemble_context(
                task_understanding, collected_data, completeness
            )
            result["stages"]["context"] = context_assembly
            result["assumptions"] = context_assembly.get("assumptions", [])
            logger.info(f"   ✅ Фактов: {len(context_assembly.get('facts', []))}")
            logger.info(f"   ✅ Мнений: {len(context_assembly.get('opinions', []))}")
            logger.info(f"   ✅ Цифр: {len(context_assembly.get('numbers', []))}")
            logger.info(f"   ⚠️ Предположений: {len(context_assembly.get('assumptions', []))}")

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 5.5: ОПЫТ ПРОШЛЫХ РЕШЕНИЙ (за флагом wisdom_in_tasker_enabled)
            # Подмешиваем мудрость BWE в key_insights → попадает в генерацию ТЗ.
            # ═══════════════════════════════════════════════════════════════════
            wisdom_brief = await self._stage_5_5_wisdom_brief(
                task_description, task_understanding, additional_context
            )
            if wisdom_brief:
                context_assembly["wisdom_brief"] = wisdom_brief
                # Префиксуем insights — мудрость идёт первой в промпт генерации.
                context_assembly["key_insights"] = (
                    wisdom_brief + (context_assembly.get("key_insights") or [])
                )
                result["stages"]["wisdom_brief"] = wisdom_brief

            # ЭТАП 5.6: ЗАЗЕМЛЕНИЕ ЗАДАЧИ (за тем же флагом). Несуществующие
            # сущности задачи + РЕЛЕВАНТНЫЕ рассинхроны/слепые зоны (только
            # касающиеся задачи — нужное, не свалка) → в insights генерации ТЗ.
            grounding = await self._stage_5_6_task_grounding(
                task_description, task_understanding, additional_context
            )
            if grounding:
                context_assembly["task_grounding"] = grounding
                context_assembly["key_insights"] = grounding + (context_assembly.get("key_insights") or [])
                result["stages"]["task_grounding"] = grounding

            # ═══════════════════════════════════════════════════════════════════
            # ЭТАП 6: ГЕНЕРАЦИЯ РЕЗУЛЬТАТА
            # Создаём ТЗ или выполняем задачу на основе РЕАЛЬНЫХ данных
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n📝 ЭТАП 6: Генерация результата")
            final_result = await self._stage_6_generate_result(
                task_description, task_understanding, context_assembly, mode
            )
            result["stages"]["result"] = final_result

            # Сохраняем файлы
            file_paths = await self._save_result(result, task_id)
            result["file_paths"] = file_paths

            # Аудит #2: ЧЕСТНЫЙ статус. Если LLM-этапы падали/возвращали пустоту,
            # ТЗ может быть неполным — не врём про "completed".
            llm_failures = getattr(self, "_llm_failures", 0)
            if llm_failures > 0:
                result["status"] = "completed_degraded"
                result["degraded"] = True
                result["llm_failures"] = llm_failures
                result["errors"].append(
                    f"LLM-этапов с ошибкой/пустотой: {llm_failures} — "
                    f"результат может быть неполным")
                logger.warning(f"⚠️ ТЗ с деградацией: {llm_failures} LLM-сбоев")
            else:
                result["status"] = "completed"

        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            result["status"] = "failed"
            result["errors"].append(str(e))

        # Метаданные
        end_time = datetime.utcnow()
        result["metadata"] = {
            "started_at": start_time.isoformat(),
            "completed_at": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
            "mode": mode
        }

        logger.info("="*70)
        logger.info(f"✅ ЗАДАЧА ЗАВЕРШЕНА: {result['status']}")
        logger.info(f"   Время: {result['metadata']['duration_seconds']:.1f} сек")
        logger.info("="*70)

        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # ЭТАПЫ ОБРАБОТКИ
    # ═══════════════════════════════════════════════════════════════════════════

    async def _stage_1_understand_task(
        self,
        task_description: str,
        additional_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        ЭТАП 1: Понимание задачи.

        Определяем:
        - Что именно нужно сделать
        - Какой тип задачи (финмодель, лендинг, отчёт и т.д.)
        - Какие данные понадобятся для выполнения
        - Какой продукт/проект упоминается
        """

        # Загружаем снепшот компании
        company_snapshot = await self._load_company_snapshot()

        prompt = f"""Ты — аналитик задач. Проанализируй задачу и определи, какие КОНКРЕТНЫЕ данные нужны для её выполнения.

СНЕПШОТ КОМПАНИИ (что мы знаем о компании):
{company_snapshot[:3000] if company_snapshot else "Снепшот не загружен"}

ЗАДАЧА: {task_description}

ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ: {json.dumps(additional_context, ensure_ascii=False) if additional_context else "Нет"}

Проанализируй и верни JSON:
{{
    "task_type": "тип задачи (finmodel|landing|report|analysis|content|planning|other)",
    "task_summary": "краткое описание что нужно сделать",
    "target_entity": {{
        "type": "product|project|company|department|person",
        "name": "название сущности (если указано) или null",
        "is_main_product": true/false
    }},
    "required_data": [
        {{
            "category": "категория данных (prices|sales|costs|plans|opinions|history|team|etc)",
            "description": "что именно нужно найти",
            "importance": "critical|important|nice_to_have",
            "search_hints": ["подсказки для поиска"]
        }}
    ],
    "expected_output": {{
        "format": "формат результата (document|spreadsheet|text|presentation)",
        "key_sections": ["ключевые разделы результата"]
    }},
    "complexity": {{
        "level": "low|medium|high|critical",
        "score": 1-10,
        "reasoning": "обоснование сложности"
    }}
}}"""

        return await self._generate_llm_response(prompt)

    async def _stage_2_plan_search(
        self,
        task_understanding: Dict[str, Any],
        additional_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        ЭТАП 2: Планирование поиска.

        LLM САМА определяет какие термины искать на основе:
        - Понимания задачи
        - Снепшота компании (какие сущности реально есть в данных)
        """

        task_understanding.get("required_data", [])
        task_understanding.get("target_entity", {})

        # Загружаем снепшот чтобы LLM видела реальные названия сущностей
        company_snapshot = await self._load_company_snapshot()

        prompt = f"""Ты — эксперт по поиску данных. Спланируй стратегию поиска информации.

ПОНИМАНИЕ ЗАДАЧИ:
{json.dumps(task_understanding, ensure_ascii=False, indent=2)}

РЕАЛЬНЫЕ ДАННЫЕ КОМПАНИИ (снепшот):
{company_snapshot[:4000] if company_snapshot else "Снепшот не загружен"}

ДОСТУПНЫЕ ИСТОЧНИКИ ДАННЫХ:
1. ГРАФ ЗНАНИЙ - узлы (Product, Concept, Organization, Task, KPI, Decision, Person, Meeting) и связи
2. ВЕКТОРНАЯ БАЗА - семантический поиск по документам и встречам

ВАЖНО: Используй РЕАЛЬНЫЕ названия из снепшота выше! Не придумывай названия.
Бери названия продуктов/проектов/систем ТОЛЬКО из снепшота — ровно в том
виде, как они там написаны. Названий, которых нет в снепшоте, НЕ существует.

Создай план поиска и верни JSON:
{{
    "search_queries": [
        {{
            "query": "поисковый запрос с РЕАЛЬНЫМИ названиями из снепшота",
            "source": "graph|vector|all",
            "purpose": "что ищем",
            "data_category": "категория данных",
            "priority": 1-10
        }}
    ],
    "graph_traversal": {{
        "start_nodes": ["РЕАЛЬНЫЕ названия узлов из снепшота"],
        "target_node_types": ["Product", "Concept", "Task", "KPI", "Decision"],
        "depth": 2
    }},
    "fallback_strategy": {{
        "if_not_found": "что делать если не найдено",
        "alternative_queries": ["альтернативные запросы"]
    }}
}}"""

        return await self._generate_llm_response(prompt)

    async def _stage_3_collect_data(
        self,
        search_plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ЭТАП 3: Сбор данных.

        КЛЮЧЕВОЕ: ВСЕГДА ищем в графе - это наш основной источник данных!
        Не зависим от того, что LLM указал в source.
        """

        collected = {
            "raw_results": [],
            "categorized_data": {},
            "sources_used": [],
            "stats": {
                "total_sources": 0,
                "total_facts": 0,
                "by_category": {}
            }
        }

        search_queries = search_plan.get("search_queries", [])

        # Сортируем по приоритету
        search_queries.sort(key=lambda x: x.get("priority", 5), reverse=True)

        # Собираем данные из графа по всем запросам
        all_graph_results = []
        processed_nodes = set()  # Чтобы не дублировать

        for sq in search_queries[:20]:
            query = sq.get("query", "")
            category = sq.get("data_category", "general")
            purpose = sq.get("purpose", "")

            if not query:
                continue

            logger.info(f"   🔍 Поиск: '{query[:50]}...' [graph]")

            # ВСЕГДА ищем в графе - это наш основной источник!
            if self.storage:
                try:
                    graph_results = await self._graph_search(query, depth=2)

                    for r in graph_results:
                        node_id = r.get("node_id", "")
                        if node_id and node_id not in processed_nodes:
                            processed_nodes.add(node_id)
                            r["search_query"] = query
                            r["data_category"] = category
                            r["purpose"] = purpose
                            all_graph_results.append(r)

                            # Категоризируем сразу
                            if category not in collected["categorized_data"]:
                                collected["categorized_data"][category] = []
                            collected["categorized_data"][category].append(r)

                    if graph_results:
                        collected["sources_used"].append(f"graph:{query[:30]}")

                except Exception as e:
                    logger.warning(f"   ⚠️ Графовый поиск failed: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())

        # ═══ ПОИСК ПО ДОКУМЕНТАМ (семантический — как в brain) ═══
        # Стадия выше ходит ТОЛЬКО по графу и НЕ видит загруженные документы
        # (презентации/прайсы) — их чанки лежат в векторном индексе. Собираем
        # фрагменты параллельно; ниже — единый relevance-rerank графа+документов.
        doc_items: List[Dict[str, Any]] = []
        try:
            import asyncio as _aio
            queries = [sq.get("query", "") for sq in search_queries[:8] if sq.get("query")]
            hit_lists = await _aio.gather(
                *[self._vector_search(q, top_k=10) for q in queries],
                return_exceptions=True)
            seen_doc: set = set()
            for q, hits in zip(queries, hit_lists):
                if isinstance(hits, Exception) or not hits:
                    continue
                for hit in hits:
                    raw = hit.get("content") if (isinstance(hit, dict) and "content" in hit) else hit
                    payload = raw.get("payload", raw) if isinstance(raw, dict) else {}
                    text = ""
                    for k in ("text", "content", "chunk_text", "chunk"):
                        v = (payload.get(k) if isinstance(payload, dict) else None) \
                            or (raw.get(k) if isinstance(raw, dict) else None)
                        if isinstance(v, str) and v.strip():
                            text = v.strip()
                            break
                    if not text or len(text) < 15:
                        continue
                    key = text[:80]
                    if key in seen_doc:
                        continue
                    seen_doc.add(key)
                    try:
                        rel = float(raw.get("score") if isinstance(raw, dict) else 0) or 0.0
                    except (TypeError, ValueError):
                        rel = 0.0
                    title = ((payload.get("title") if isinstance(payload, dict) else None)
                             or (payload.get("document_title") if isinstance(payload, dict) else None)
                             or "фрагмент документа")
                    doc_items.append({"label": "Document", "name": str(title)[:120],
                                      "content": text[:1500], "source": "документ",
                                      "node_id": f"doc::{key}", "search_query": q,
                                      "data_category": "document", "rel_score": rel})
                    if len(doc_items) >= 20:
                        break
                if len(doc_items) >= 20:
                    break
        except Exception as e:
            logger.debug("   document vector search skipped: %s", e)

        # ═══ ТОЧНЫЕ СОВПАДЕНИЯ (BM25) — третий канал ═══
        # Аудит SEARCH_CONSUMERS_AUDIT, правка №4: стадия ходила графом и
        # векторами, а канал ТОЧНЫХ СЛОВ отсутствовал — имена, артикулы,
        # идентификаторы, которые вектор размывает. Добираем BM25-фрагменты
        # теми же запросами и отдаём в общий RRF ниже наравне с остальными.
        # Дедуп — те же ключи-префиксы, что у документов. Never-raise.
        # Выключатель: TESSENT_TZ_BM25=off (полный перевод стадии на hybrid —
        # отдельная работа; этот шаг закрывает главный пробел).
        bm25_items: List[Dict[str, Any]] = []
        try:
            import os as _os
            if (_os.getenv("TESSENT_TZ_BM25", "") or "").strip().lower() not in (
                    "off", "0", "false", "no"):
                # ТЕНАНТНОСТЬ: у оркестратора поле называется
                # _current_user_id; без него per-user индекса нет — и тогда
                # добор ПРОПУСКАЕМ, а не лезем в общий singleton-индекс.
                _uid = getattr(self, "_current_user_id", None)
                if not _uid:
                    raise LookupError("нет user_id — bm25-добор пропущен")
                from backend.core.search.bm25_searcher import get_bm25_searcher
                _bm = get_bm25_searcher(user_id=_uid)
                _seen_bm = {d["node_id"][5:] for d in doc_items}  # doc::<key>
                for q in [sq.get("query", "") for sq in search_queries[:8]
                          if sq.get("query")]:
                    for hit in _bm.search(q, top_k=5):
                        text = str(getattr(hit, "text", "") or "").strip()
                        if not text or len(text) < 15:
                            continue
                        key = text[:80]
                        if key in _seen_bm:
                            continue
                        _seen_bm.add(key)
                        try:
                            rel = float(getattr(hit, "score", 0) or 0)
                        except (TypeError, ValueError):
                            rel = 0.0
                        bm25_items.append({
                            "label": "Document", "name": "точное совпадение",
                            "content": text[:1500], "source": "bm25",
                            "node_id": f"bm25::{key}", "search_query": q,
                            "data_category": "document", "rel_score": rel})
                        if len(bm25_items) >= 12:
                            break
                    if len(bm25_items) >= 12:
                        break
        except Exception as e:
            logger.debug("   bm25 supplement skipped: %s", e)

        # ═══ RELEVANCE-RERANK (RRF) — единое ранжирование всех источников ═══
        # Никаких правил «источник A раньше B» и «документы первыми». Граф
        # ранжируем по match_score, документы — по cosine-score, объединяем
        # методом RRF (обратный ранг). Топ-релевантные фрагменты ЛЮБОГО источника
        # (в т.ч. прайс из презы) поднимаются наверх и переживают усечения ниже
        # (raw_results[:40], extracted_facts[:25], facts[:60]) — решает СМЫСЛ, а
        # не тип источника. Recency становится вторичным сигналом (тай-брейк).
        _K = 60
        g_ranked = sorted(all_graph_results,
                          key=lambda r: float(r.get("match_score", 0) or 0), reverse=True)
        d_ranked = sorted(doc_items,
                          key=lambda r: float(r.get("rel_score", 0) or 0), reverse=True)
        rrf: Dict[int, float] = {}
        for rank, it in enumerate(g_ranked, 1):
            rrf[id(it)] = rrf.get(id(it), 0.0) + 1.0 / (_K + rank)
        for rank, it in enumerate(d_ranked, 1):
            rrf[id(it)] = rrf.get(id(it), 0.0) + 1.0 / (_K + rank)
        b_ranked = sorted(bm25_items,
                          key=lambda r: float(r.get("rel_score", 0) or 0), reverse=True)
        for rank, it in enumerate(b_ranked, 1):
            rrf[id(it)] = rrf.get(id(it), 0.0) + 1.0 / (_K + rank)
        combined = list(all_graph_results) + list(doc_items) + list(bm25_items)
        combined.sort(key=lambda it: rrf.get(id(it), 0.0), reverse=True)
        collected["raw_results"] = combined
        if doc_items:
            collected.setdefault("categorized_data", {}).setdefault(
                "document", []).extend(doc_items)
            collected["sources_used"].extend(f"doc:{d['search_query'][:30]}" for d in doc_items)
        if bm25_items:
            collected.setdefault("categorized_data", {}).setdefault(
                "document", []).extend(bm25_items)
            collected["sources_used"].extend(
                f"bm25:{d['search_query'][:30]}" for d in bm25_items)
        logger.info("   🎯 Relevance-rerank (RRF): %d граф + %d документов + %d bm25",
                    len(all_graph_results), len(doc_items), len(bm25_items))

        # ═══ ЦИФРЫ ИЗ ОНТОЛОГИИ ДАННЫХ (метрики + таблицы/CRM) ═══
        # Раньше финмодель/медиаплан видели только цифры, ПРОГОВОРЁННЫЕ на
        # встречах (KPI-узлы графа). Подмешиваем реальные: единые метрики
        # (план/факт, сверка «встреча vs данные») и заземлённые числовые
        # колонки датасетов. Never-raise.
        try:
            uid = getattr(self, "_current_user_id", None)
            if uid:
                from backend.core.ontology.numbers_context import numbers_block
                q_text = " ".join(
                    sq.get("query", "") for sq in search_queries[:10])
                nb = numbers_block(uid, q_text)
                if nb.get("numbers"):
                    collected["ontology_numbers"] = nb["numbers"]
                    collected["sources_used"].extend(nb.get("sources", []))
                    logger.info(f"   📈 Цифры из онтологии данных: "
                                f"{len(nb['numbers'])}")
        except Exception as e:
            logger.debug(f"ontology numbers skipped: {e}")

        # Обновляем статистику
        collected["stats"]["total_sources"] = len(set(collected["sources_used"]))
        collected["stats"]["total_facts"] = len(collected["raw_results"])
        for cat, items in collected["categorized_data"].items():
            collected["stats"]["by_category"][cat] = len(items)

        logger.info(f"   ✅ Найдено источников: {collected['stats']['total_sources']}")
        logger.info(f"   ✅ Найдено фактов: {collected['stats']['total_facts']}")

        return collected

    async def _stage_4_analyze_completeness(
        self,
        task_understanding: Dict[str, Any],
        collected_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ЭТАП 4: Анализ полноты данных.

        Проверяем:
        - Какие данные нашли
        - Чего не хватает
        - Нужен ли дополнительный поиск
        """

        required_data = task_understanding.get("required_data", [])
        list(collected_data.get("categorized_data", {}).keys())

        gaps = []
        found = []

        for req in required_data:
            category = req.get("category", "")
            importance = req.get("importance", "important")

            cat_data = collected_data.get("categorized_data", {}).get(category, [])

            if cat_data and len(cat_data) > 0:
                found.append({
                    "category": category,
                    "count": len(cat_data),
                    "importance": importance
                })
            else:
                gaps.append({
                    "category": category,
                    "description": req.get("description", ""),
                    "importance": importance,
                    "search_hints": req.get("search_hints", [])
                })

        # Рассчитываем полноту
        total_required = len(required_data)
        total_found = len(found)
        completeness_score = int((total_found / total_required * 100)) if total_required > 0 else 0

        return {
            "completeness_score": completeness_score,
            "found": found,
            "gaps": gaps,
            "missing_data": [g["category"] for g in gaps if g["importance"] == "critical"],
            "needs_additional_search": completeness_score < 60,
            "summary": f"Найдено {total_found}/{total_required} категорий данных ({completeness_score}%)"
        }

    async def _stage_4_5_additional_search(
        self,
        completeness: Dict[str, Any],
        task_understanding: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ЭТАП 4.5: Дополнительный поиск.

        Ищем недостающие данные с альтернативными запросами.
        """

        gaps = completeness.get("gaps", [])
        additional_results = {
            "raw_results": [],
            "categorized_data": {},
            "sources_used": [],
            "stats": {"total_sources": 0, "total_facts": 0, "by_category": {}}
        }

        for gap in gaps[:5]:  # Топ-5 пробелов
            category = gap.get("category", "")
            hints = gap.get("search_hints", [])

            for hint in hints[:3]:
                logger.info(f"   🔄 Доп. поиск: '{hint[:50]}...'")

                results = []

                if self.storage:
                    try:
                        vector_results = await self._vector_search(hint, top_k=5)
                        results.extend(vector_results)
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                for r in results:
                    r["data_category"] = category
                    r["search_type"] = "additional"

                additional_results["raw_results"].extend(results)

                if category not in additional_results["categorized_data"]:
                    additional_results["categorized_data"][category] = []
                additional_results["categorized_data"][category].extend(results)

        additional_results["stats"]["total_facts"] = len(additional_results["raw_results"])

        return additional_results

    async def _stage_5_assemble_context(
        self,
        task_understanding: Dict[str, Any],
        collected_data: Dict[str, Any],
        completeness: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ЭТАП 5: Сборка контекста.

        УЛУЧШЕННАЯ ВЕРСИЯ:
        - Извлекает РЕАЛЬНЫЕ факты из данных графа
        - Форматирует данные по типам узлов
        - Сохраняет источники информации
        """

        raw_results = collected_data.get("raw_results", [])
        gaps = completeness.get("gaps", [])

        # Сначала извлекаем факты напрямую из данных графа
        extracted_facts = []
        extracted_numbers = []
        extracted_opinions = []

        # Реальные цифры из онтологии данных (этап 3): метрики план/факт и
        # ряды из таблиц — идут ПЕРВЫМИ, они точнее пересказов со встреч.
        extracted_numbers.extend(collected_data.get("ontology_numbers") or [])

        for item in raw_results:
            label = item.get("label", "Unknown")
            name = item.get("name", "")
            content = item.get("content", "")
            data = item.get("data", {})

            if not name:
                continue

            # Извлекаем факты по типу узла
            if label == "Product":
                extracted_facts.append({
                    "fact": content or f"Продукт: {name}",
                    "source": "граф знаний / Product",
                    "confidence": "high",
                    "category": "product"
                })

            elif label == "Concept":
                extracted_facts.append({
                    "fact": content or f"Концепция: {name}",
                    "source": "граф знаний / Concept",
                    "confidence": "high",
                    "category": "concept"
                })

            elif label == "Organization":
                extracted_facts.append({
                    "fact": content or f"Организация: {name}",
                    "source": "граф знаний / Organization",
                    "confidence": "high",
                    "category": "organization"
                })

            elif label == "KPI":
                value = data.get("value", "")
                unit = data.get("unit", "")
                if value:
                    extracted_numbers.append({
                        "metric": name,
                        "value": str(value),
                        "unit": unit or "",
                        "source": "граф знаний / KPI",
                        "is_verified": True
                    })
                extracted_facts.append({
                    "fact": content or f"KPI: {name}",
                    "source": "граф знаний / KPI",
                    "confidence": "high",
                    "category": "kpi"
                })

            elif label == "Decision":
                extracted_facts.append({
                    "fact": content or f"Решение: {name}",
                    "source": "граф знаний / Decision",
                    "confidence": "high",
                    "category": "decision"
                })

            elif label == "Task":
                status = data.get("status", "")
                extracted_facts.append({
                    "fact": content or f"Задача: {name}" + (f" [статус: {status}]" if status else ""),
                    "source": "граф знаний / Task",
                    "confidence": "high",
                    "category": "task"
                })

            elif label == "Document":
                # Фрагмент загруженного документа (презентация/прайс/файл) —
                # реальные факты и ЦИФРЫ из файлов пользователя. Идёт в контекст
                # генерации, чтобы КП/отчёт брали цены из документа, а не выдумку.
                if content:
                    extracted_facts.append({
                        "fact": content,
                        "source": f"документ / {name}",
                        "confidence": "high",
                        "category": "document"
                    })

        # Форматируем найденные данные для LLM
        data_summary = []
        for item in raw_results[:40]:
            content = item.get("content", "")
            if content:
                data_summary.append(content)

        # Если нашли много данных - используем их напрямую
        if len(extracted_facts) >= 5:
            logger.info(f"   ✅ Извлечено фактов напрямую: {len(extracted_facts)}")

            # Дополняем через LLM только если нужно
            prompt = f"""Ты — аналитик данных. На основе НАЙДЕННЫХ ДАННЫХ сформируй ключевые инсайты.

ЗАДАЧА: {task_understanding.get('task_summary', '')}

НАЙДЕННЫЕ ФАКТЫ ({len(extracted_facts)}):
{chr(10).join([f"- {f['fact']}" for f in extracted_facts[:25]])}

ПРОБЕЛЫ В ДАННЫХ:
{chr(10).join([f"- {g.get('category', '')}: {g.get('description', '')}" for g in gaps[:5]])}

Верни JSON с инсайтами и предположениями (факты уже извлечены):
{{
    "key_insights": [
        "ключевой инсайт на основе данных 1",
        "ключевой инсайт на основе данных 2"
    ],
    "assumptions": [
        {{
            "assumption": "что предполагаем (только если данных не хватает)",
            "reason": "почему",
            "based_on": "на чём основано",
            "confidence": "high|medium|low"
        }}
    ],
    "data_quality": {{
        "overall_score": 1-10,
        "strengths": ["что хорошо в данных"],
        "weaknesses": ["чего не хватает"]
    }}
}}"""

            llm_response = await self._generate_llm_response(prompt)

            return {
                "facts": extracted_facts,
                "opinions": extracted_opinions,
                "numbers": extracted_numbers,
                "assumptions": llm_response.get("assumptions", []),
                "key_insights": llm_response.get("key_insights", []),
                "data_quality": llm_response.get("data_quality", {"overall_score": 5})
            }

        # Если данных мало - используем LLM для анализа
        prompt = f"""Ты — аналитик данных. Структурируй найденную информацию для задачи.

ЗАДАЧА: {task_understanding.get('task_summary', '')}

НАЙДЕННЫЕ ДАННЫЕ:
{chr(10).join(data_summary[:30]) if data_summary else "Данные не найдены"}

ПРОБЕЛЫ В ДАННЫХ:
{json.dumps(gaps, ensure_ascii=False, indent=2)}

Структурируй информацию и верни JSON:
{{
    "facts": [
        {{
            "fact": "конкретный факт из данных",
            "source": "откуда взято",
            "confidence": "high|medium|low",
            "category": "категория"
        }}
    ],
    "opinions": [],
    "numbers": [],
    "assumptions": [
        {{
            "assumption": "что предполагаем",
            "reason": "почему",
            "based_on": "на чём основано",
            "confidence": "high|medium|low"
        }}
    ],
    "key_insights": ["инсайт 1", "инсайт 2"],
    "data_quality": {{
        "overall_score": 1-10,
        "strengths": [],
        "weaknesses": []
    }}
}}"""

        return await self._generate_llm_response(prompt)

    async def _stage_5_5_wisdom_brief(
        self,
        task_description: str,
        task_understanding: Dict[str, Any],
        additional_context: Optional[Dict[str, Any]],
    ) -> List[str]:
        """ЭТАП 5.5 (за флагом wisdom_in_tasker_enabled): «опыт прошлых решений».

        Дёргает BWE по ситуации задачи и возвращает строки-инсайты для
        вложения в key_insights → попадают во все промпты генерации без их
        правки. Любой сбой / нет user_id / wisdom недоступен → [] (ТЗ идёт
        без мудрости, как и раньше). Граница ответственности: ТЗ не падает
        из-за wisdom.
        """
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "wisdom_in_tasker_enabled", False):
                return []
            ctx = additional_context or {}
            user_id = ctx.get("user_id")
            if not user_id:
                return []  # без тенанта мудрость не запрашиваем

            situation = (
                task_understanding.get("task_summary")
                or task_description
                or ""
            )
            if not situation.strip():
                return []

            from backend.core.wisdom.wisdom_engine import get_wisdom_engine

            engine = await get_wisdom_engine()
            brief = await engine.build_wisdom_brief(
                user_id=user_id,
                situation=situation,
                company_vitek=int(ctx.get("company_vitek", 1) or 1),
                market_context=ctx.get("market_context") or "",
            )
            if brief:
                logger.info(f"   ✅ Wisdom-бриф: {len(brief)} пунктов из BWE")
            return brief
        except Exception as e:
            logger.warning(f"[Tasker] wisdom brief unavailable, ТЗ без мудрости: {e}")
            return []

    async def _stage_5_6_task_grounding(self, task_description, task_understanding, additional_context):
        """ЭТАП 5.6 (за флагом wisdom_in_tasker_enabled): заземление задачи.
        Сущности задачи, которых в компании НЕТ + РЕЛЕВАНТНЫЕ (касающиеся задачи)
        рассинхроны/слепые зоны. Несущее: НУЖНОЕ, не свалка — фильтр по сущностям.
        Любой сбой / нет user_id → [] (ТЗ идёт как раньше). Graceful."""
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "wisdom_in_tasker_enabled", False):
                return []
            ctx = additional_context or {}
            user_id = ctx.get("user_id")
            if not user_id:
                return []

            # Сущности задачи: target_entity + продукты + явно переданные.
            ents: list = []
            te = task_understanding.get("target_entity") or {}
            if isinstance(te, dict) and te.get("name"):
                ents.append(te["name"])
            for p in (task_understanding.get("products") or []):
                ents.append(p.get("name") if isinstance(p, dict) else p)
            ents += [e for e in (ctx.get("entities") or []) if e]
            ents = [e for e in ents if e and str(e).strip()]
            if not ents:
                return []

            # Best-effort: узлы графа → known + desyncs + blind-spots (read-only).
            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.tenant_paths import graph_path_for_user
            from backend.core.think.blind_spot_scanner import (
                contradictions_from_graph_nodes,
                recurring_from_pattern_nodes,
            )
            from backend.core.think.cross_department_desync import (
                dept_goals_from_graph_nodes,
                find_desyncs,
            )
            from backend.core.think.table_understanding_agent import prior_from_graph_nodes
            from backend.core.think.task_grounding import grounding_notes

            gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
            await gb.connect()
            nxg = getattr(gb, "nx_graph", None)
            nodes = [d for _i, d in nxg.nodes(data=True)] if nxg is not None else []
            try:
                await gb.close(save=False)
            except Exception:
                pass

            prior = prior_from_graph_nodes(nodes)
            known = set().union(*prior.values()) if prior else set()
            desyncs = [d.to_dict() for d in find_desyncs(dept_goals_from_graph_nodes(nodes))]
            blind = [b.to_dict() for b in recurring_from_pattern_nodes(nodes)]
            blind += contradictions_from_graph_nodes(nodes)  # dict-форма уже подходит

            notes = grounding_notes(ents, known, desyncs=desyncs, blind_spots=blind)
            if notes:
                logger.info(f"   ✅ Заземление задачи: {len(notes)} релевантных предупреждений")
            return notes
        except Exception as e:
            logger.warning(f"[Tasker] task grounding unavailable: {e}")
            return []

    async def _stage_6_generate_result(
        self,
        task_description: str,
        task_understanding: Dict[str, Any],
        context_assembly: Dict[str, Any],
        mode: str
    ) -> Dict[str, Any]:
        """
        ЭТАП 6: Генерация результата.

        КЛЮЧЕВОЕ: Генерируем КОНКРЕТНЫЙ результат, а не шаблон!
        - Передаём ВСЕ найденные данные
        - LLM должен переформулировать их в готовый результат
        """

        facts = context_assembly.get("facts", [])
        opinions = context_assembly.get("opinions", [])
        numbers = context_assembly.get("numbers", [])
        assumptions = context_assembly.get("assumptions", [])
        insights = context_assembly.get("key_insights", [])

        # Форматируем данные для промпта - ВСЕ ДАННЫЕ, не только первые 25!
        # Группируем факты по категориям для лучшего понимания
        facts_by_category = {}
        for f in facts[:60]:  # Увеличиваем лимит
            cat = f.get('category', 'other')
            if cat not in facts_by_category:
                facts_by_category[cat] = []
            facts_by_category[cat].append(f)

        # Форматируем факты по категориям. Доступ ТОЛЬКО через .get с запасными
        # ключами: этап 5 — вывод LLM, схема плавает (metric/name, opinion/text),
        # и жёсткий n['metric'] ронял ВСЮ генерацию ТЗ (KeyError 'metric').
        def _val(d, *keys, default="?"):
            if not isinstance(d, dict):
                return str(d)
            for k in keys:
                if d.get(k):
                    return d[k]
            return default

        facts_lines = []
        for cat, cat_facts in facts_by_category.items():
            facts_lines.append(f"\n=== {cat.upper()} ===")
            for f in cat_facts:
                facts_lines.append(f"• {_val(f, 'fact', 'text', 'description')} "
                                   f"[{_val(f, 'source', default='N/A')}]")
        facts_text = "\n".join(facts_lines)

        opinions_text = "\n".join(
            f"• {_val(o, 'opinion', 'text', 'position')} ({_val(o, 'who_said', 'who', 'author', default='N/A')})"
            for o in opinions[:15])
        numbers_text = "\n".join(
            f"• {_val(n, 'metric', 'name', 'kpi', 'metric_name')}: {_val(n, 'value', default='')} "
            f"{_val(n, 'unit', default='')} [{_val(n, 'source', default='N/A')}]"
            for n in numbers[:30])

        # Сохранённые ссылки компании (сайт/прайс/шаблоны) → в контекст: раньше
        # КП генерилось «в вакууме» без единой ссылки на материалы компании
        try:
            uid = getattr(self, "_current_user_id", None)
            if uid:
                from backend.core.store.user_resources import resources_context_block
                rb = resources_context_block(uid)
                if rb:
                    facts_text = f"{facts_text}\n\n{rb}"
        except Exception:
            logger.debug("resources context skipped", exc_info=True)

        task_type = task_understanding.get('task_type', 'unknown')

        # Разные промпты для разных типов задач
        if task_type == "landing":
            prompt = self._get_landing_prompt(task_description, task_understanding, facts_text, opinions_text, numbers_text, insights)
        elif task_type == "finmodel":
            prompt = self._get_finmodel_prompt(task_description, task_understanding, facts_text, numbers_text, insights)
        else:
            prompt = self._get_general_prompt(task_description, task_understanding, facts_text, opinions_text, numbers_text, insights, assumptions)

        result = await self._generate_llm_response(prompt)

        # Генерируем Markdown версию
        result["markdown"] = self._generate_markdown(result, task_description, context_assembly)

        return result

    def _get_landing_prompt(self, task_description, task_understanding, facts_text, opinions_text, numbers_text, insights):
        """Промпт для создания лендинга - ГОТОВЫЙ РЕЗУЛЬТАТ с реальными данными."""

        # Извлекаем название продукта из task_understanding
        target_entity = task_understanding.get('target_entity', {})
        product_name = target_entity.get('name', 'Продукт')

        return f"""Ты — копирайтер. Твоя задача: НАПИСАТЬ ГОТОВЫЕ ТЕКСТЫ для лендинга.

КРИТИЧЕСКИ ВАЖНО:
- НЕ ПИШИ "нужно написать" или "следует разработать" - ПИШИ САМИ ТЕКСТЫ!
- Используй ТОЛЬКО данные ниже - переформулируй их в маркетинговые тексты
- Каждый текст должен быть ГОТОВ к использованию

═══════════════════════════════════════════════════════════════════════
ЗАДАЧА: {task_description}
ПРОДУКТ: {product_name}
═══════════════════════════════════════════════════════════════════════

ДАННЫЕ О ПРОДУКТЕ (используй их для написания текстов):
{facts_text if facts_text else "Данные не найдены"}

ИДЕИ КОМАНДЫ:
{opinions_text if opinions_text else "Нет"}

МЕТРИКИ:
{numbers_text if numbers_text else "Нет"}

═══════════════════════════════════════════════════════════════════════

ПРИМЕР ПРАВИЛЬНОГО ОТВЕТА — это ШАБЛОН СТРУКТУРЫ. Все названия и факты
подставляй из ДАННЫХ ВЫШЕ; никакие сущности из этого шаблона в ответ не
переносить:

**HERO SECTION:**
Заголовок: "<название продукта из данных> — <главная ценность одним предложением>"
Подзаголовок: "<2-3 ключевые возможности из фактов>"
CTA: "<целевое действие>"
(Источник: <название концепции/задачи из данных>)

**ВОЗМОЖНОСТИ:**
1. **<Возможность из фактов>** — <описание из данных> (Источник: <факт/концепция из данных>)
2. **<Возможность из фактов>** — <описание из данных> (Источник: <факт/концепция из данных>)
3. **<Возможность из фактов>** — <описание из данных> (Источник: <факт/концепция из данных>)

═══════════════════════════════════════════════════════════════════════

Теперь НАПИШИ ГОТОВЫЙ ЛЕНДИНГ в таком же стиле, используя ДАННЫЕ ВЫШЕ.

Верни JSON:
{{
    "title": "Лендинг: {product_name}",
    "executive_summary": "Готовые тексты лендинга на основе данных компании",
    "sections": [
        {{
            "title": "HERO SECTION",
            "content": "**Заголовок:** [ГОТОВЫЙ ТЕКСТ заголовка на основе данных]\\n\\n**Подзаголовок:** [ГОТОВЫЙ ТЕКСТ подзаголовка]\\n\\n**CTA:** [текст кнопки]\\n\\n(Источник: [конкретные источники из данных выше])",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "ПРОБЛЕМА",
            "content": "[ГОТОВЫЙ ТЕКСТ о проблемах - 2-3 предложения, основанные на данных]\\n\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "РЕШЕНИЕ",
            "content": "[ГОТОВЫЙ ТЕКСТ описания продукта - 2-3 абзаца]\\n\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "ВОЗМОЖНОСТИ",
            "content": "**1. [Название из данных]**\\n[Описание из данных]\\n(Источник: [откуда])\\n\\n**2. [Название]**\\n[Описание]\\n(Источник: [откуда])\\n\\n**3. [Название]**\\n[Описание]\\n(Источник: [откуда])\\n\\n**4. [Название]**\\n[Описание]\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "КАК РАБОТАЕТ",
            "content": "**Шаг 1:** [описание на основе данных]\\n**Шаг 2:** [описание]\\n**Шаг 3:** [описание]\\n\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "ДЛЯ КОГО",
            "content": "[ГОТОВЫЙ ТЕКСТ о целевой аудитории на основе данных]\\n\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "medium"
        }},
        {{
            "title": "CTA FOOTER",
            "content": "**Заголовок:** [готовый текст]\\n**Подзаголовок:** [готовый текст]\\n**Кнопка:** [текст]",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "ДОПОЛНИТЕЛЬНЫЕ ИДЕИ",
            "content": "Из данных также можно использовать:\\n- [идея 1 с источником]\\n- [идея 2 с источником]",
            "data_sources": ["источники"],
            "confidence": "medium"
        }}
    ],
    "data_used": {{"facts_count": 0, "opinions_count": 0, "numbers_count": 0, "assumptions_count": 0}},
    "limitations": ["чего не хватает"],
    "recommendations": ["где взять"],
    "next_steps": ["что дальше"]
}}"""

    def _get_finmodel_prompt(self, task_description, task_understanding, facts_text, numbers_text, insights):
        """Промпт для финмодели - ГОТОВЫЙ РЕЗУЛЬТАТ."""
        return f"""Ты — финансовый аналитик. У тебя есть данные компании.
Твоя задача: СОЗДАТЬ ГОТОВУЮ ФИНМОДЕЛЬ, а не описать как её делать.

═══════════════════════════════════════════════════════════════════════
ЗАДАЧА: {task_description}
═══════════════════════════════════════════════════════════════════════

ДАННЫЕ О ПРОДУКТЕ/ПРОЕКТЕ:
{facts_text if facts_text else "Данные не найдены"}

ЧИСЛОВЫЕ ДАННЫЕ (цены, объёмы, планы, KPI):
{numbers_text if numbers_text else "Числовые данные не найдены"}

═══════════════════════════════════════════════════════════════════════

СОЗДАЙ ГОТОВУЮ ФИНМОДЕЛЬ! С конкретными цифрами где они есть, и с пометками где нужны данные.

Верни JSON:
{{
    "title": "Финансовая модель: [название]",
    "executive_summary": "Финмодель построена на основе [X] числовых данных из системы",
    "sections": [
        {{
            "title": "ИСХОДНЫЕ ДАННЫЕ (из системы)",
            "content": "[Перечисли ВСЕ числа которые нашёл в данных с источниками]\\n- [метрика]: [значение] (Источник: [откуда])\\n- ...",
            "data_sources": ["источники"],
            "confidence": "high"
        }},
        {{
            "title": "НУЖНЫ ДАННЫЕ (не найдено)",
            "content": "[Перечисли какие данные нужны для полной модели и ОТКУДА их взять]\\n- [что нужно] → спросить у [кого/где]\\n- ...",
            "data_sources": [],
            "confidence": "low"
        }},
        {{
            "title": "СТРУКТУРА ДОХОДОВ",
            "content": "[ГОТОВАЯ ТАБЛИЦА/РАСЧЁТ]\\n| Источник дохода | Цена | Объём | Выручка |\\n|---|---|---|---|\\n| [название] | [цена или ?] | [объём или ?] | [расчёт] |\\n\\nПОЧЕМУ ТАК: [объяснение логики]\\n(Источник: [откуда данные])",
            "data_sources": ["источники"],
            "confidence": "medium"
        }},
        {{
            "title": "СТРУКТУРА РАСХОДОВ",
            "content": "[ГОТОВАЯ ТАБЛИЦА/РАСЧЁТ расходов]\\n\\nПОЧЕМУ ТАК: [объяснение]\\n(Источник: [откуда])",
            "data_sources": ["источники"],
            "confidence": "medium"
        }},
        {{
            "title": "UNIT-ЭКОНОМИКА",
            "content": "[ГОТОВЫЕ РАСЧЁТЫ]\\n- CAC: [значение или формула]\\n- LTV: [значение или формула]\\n- Маржа: [значение]\\n\\nПОЧЕМУ ТАК: [объяснение]",
            "data_sources": ["источники"],
            "confidence": "medium"
        }},
        {{
            "title": "ПРОГНОЗ",
            "content": "[ГОТОВАЯ ТАБЛИЦА прогноза по месяцам/кварталам]\\n\\nПОЧЕМУ ТАКОЙ ПРОГНОЗ: [объяснение на основе данных]",
            "data_sources": ["источники"],
            "confidence": "low"
        }},
        {{
            "title": "💡 РЕКОМЕНДАЦИИ",
            "content": "На основе данных рекомендую:\\n1. [рекомендация с обоснованием]\\n2. [рекомендация]\\n3. [рекомендация]",
            "data_sources": ["источники"],
            "confidence": "medium"
        }}
    ],
    "data_used": {{"facts_count": 0, "opinions_count": 0, "numbers_count": 0, "assumptions_count": 0}},
    "limitations": ["какие данные отсутствуют"],
    "recommendations": ["где взять недостающие данные"],
    "next_steps": ["что делать с этой финмоделью дальше"]
}}"""

    def _get_general_prompt(self, task_description, task_understanding, facts_text, opinions_text, numbers_text, insights, assumptions):
        """Общий промпт - ГОТОВЫЙ РЕЗУЛЬТАТ."""
        task_type = task_understanding.get('task_type', 'unknown')

        return f"""Ты — идеальный сотрудник компании с доступом ко ВСЕМ данным.
Твоя задача: ВЫПОЛНИТЬ ЗАДАЧУ максимально, а не описать как её выполнить.

═══════════════════════════════════════════════════════════════════════
ЗАДАЧА: {task_description}
ТИП: {task_type}
═══════════════════════════════════════════════════════════════════════

ВСЕ ДАННЫЕ ИЗ СИСТЕМЫ:
{facts_text if facts_text else "Данные не найдены"}

МНЕНИЯ И ИДЕИ КОМАНДЫ:
{opinions_text if opinions_text else "Мнения не найдены"}

ЧИСЛОВЫЕ ДАННЫЕ:
{numbers_text if numbers_text else "Числовые данные не найдены"}

═══════════════════════════════════════════════════════════════════════

ВАЖНО:
1. ВЫПОЛНИ задачу, а не опиши как её выполнить
2. Каждый факт/вывод сопровождай источником: (Источник: ...)
3. Объясняй ПОЧЕМУ именно так
4. Предлагай дополнительные идеи из контекста
5. Укажи что ещё нужно и ОТКУДА это взять

Верни JSON:
{{
    "title": "[Что сделано]: [название]",
    "executive_summary": "Выполнено на основе [X] фактов из данных компании. [Краткий результат]",
    "sections": [
        {{
            "title": "[Название раздела]",
            "content": "[ГОТОВЫЙ РЕЗУЛЬТАТ, не описание!]\\n\\nПОЧЕМУ ТАК: [объяснение на основе данных]\\n(Источник: [откуда])",
            "data_sources": ["конкретные источники"],
            "confidence": "high|medium|low"
        }}
    ],
    "data_used": {{"facts_count": 0, "opinions_count": 0, "numbers_count": 0, "assumptions_count": 0}},
    "limitations": ["чего не хватает"],
    "recommendations": ["где взять недостающее"],
    "next_steps": ["что делать дальше"]
}}"""

    # ═══════════════════════════════════════════════════════════════════════════
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ═══════════════════════════════════════════════════════════════════════════

    async def _load_company_snapshot(self) -> str:
        """Загрузка снепшота компании."""
        if self._company_snapshot:
            return self._company_snapshot

        try:
            # Пробуем загрузить из файла - ищем в разных местах
            snapshot_paths = [
                Path("data/company_snapshot.md"),
                Path("data/company_snapshot.txt"),
                Path("company_snapshot.md"),
                Path("backend/data/company_snapshot.md"),
            ]  # аудит: убран мёртвый захардкоженный Windows-путь E:/synlabs/...

            for path in snapshot_paths:
                if path.exists():
                    self._company_snapshot = path.read_text(encoding='utf-8')
                    logger.info(f"   📄 Снепшот загружен: {path}")
                    return self._company_snapshot

            # Если файла нет - генерируем из графа
            if self.storage:
                snapshot = await self._generate_snapshot_from_graph()
                if snapshot:
                    self._company_snapshot = snapshot
                    return self._company_snapshot

        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось загрузить снепшот: {e}")

        return ""

    async def _generate_snapshot_from_graph(self) -> str:
        """
        Генерация ДЕТАЛЬНОГО снепшота из графа пользователя.

        Граф пользователя создаётся при синхронизации папок и содержит:
        - Документы и их чанки (DocumentChunk)
        - Регламенты и правила (Regulation)
        - Сущности из документов (Entity)
        - Шаблоны (Template)
        - Задачи (Task)
        - Решения (Decision)
        - Участники (Person)
        - Встречи (Meeting)
        - KPI и метрики
        """
        try:
            # Получаем ВСЕ графы
            all_graphs = self._get_all_graphs()

            if not all_graphs:
                return ""

            # Собираем данные по категориям (все типы из графа пользователя)
            products = []
            concepts = []
            organizations = []
            kpis = []
            decisions = []
            tasks_list = []
            people = []
            regulations = []  # Регламенты
            entities = []     # Сущности из документов
            templates = []    # Шаблоны
            documents = []    # Документы
            meetings = []     # Встречи

            # Статистика по типам узлов
            node_types_stats = {}

            total_nodes = 0
            total_edges = 0

            # Проходим по всем графам
            for _graph_name, nx_graph in all_graphs:
                total_nodes += nx_graph.number_of_nodes()
                total_edges += nx_graph.number_of_edges()

                for _node_id, data in nx_graph.nodes(data=True):
                    label = data.get("_label", "Unknown")
                    name = data.get("name", "") or data.get("title", "") or ""
                    description = data.get("description", "") or data.get("content", "")[:200] or ""

                    # Считаем статистику по типам
                    node_types_stats[label] = node_types_stats.get(label, 0) + 1

                    if not name:
                        continue

                    if label == "Product":
                        products.append({
                            "name": name,
                            "description": description,
                            "data": data
                        })
                    elif label == "Concept":
                        concepts.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "Organization":
                        organizations.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "KPI":
                        value = data.get("value", "")
                        unit = data.get("unit", "")
                        kpis.append({
                            "name": name,
                            "value": value,
                            "unit": unit,
                            "description": description
                        })
                    elif label == "Decision":
                        decisions.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "Task":
                        status = data.get("status", "")
                        tasks_list.append({
                            "name": name,
                            "status": status,
                            "description": description
                        })
                    elif label == "Person":
                        role = data.get("role", "")
                        people.append({
                            "name": name,
                            "role": role
                        })
                    elif label == "Regulation":
                        regulations.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "Entity":
                        entity_type = data.get("entity_type", "")
                        entities.append({
                            "name": name,
                            "type": entity_type,
                            "description": description
                        })
                    elif label == "Template":
                        templates.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "Document":
                        documents.append({
                            "name": name,
                            "description": description
                        })
                    elif label == "Meeting":
                        date = data.get("date", "") or data.get("created_at", "")
                        meetings.append({
                            "name": name,
                            "date": date,
                            "description": description
                        })

            # Формируем детальный снепшот
            lines = []
            lines.append("# Данные компании из графа знаний")
            lines.append("")
            lines.append(f"**Всего узлов:** {total_nodes}")
            lines.append(f"**Всего связей:** {total_edges}")
            lines.append("")

            # Статистика по типам узлов - важно для понимания что есть в графе!
            lines.append("## 📊 Типы данных в графе")
            lines.append("")
            for node_type, count in sorted(node_types_stats.items(), key=lambda x: -x[1])[:15]:
                lines.append(f"- **{node_type}**: {count} узлов")
            lines.append("")

            # Регламенты и правила - основа знаний компании!
            if regulations:
                lines.append("## 📜 Регламенты и правила")
                lines.append("")
                for r in regulations[:20]:
                    desc = r['description'][:150] + "..." if len(r['description']) > 150 else r['description']
                    lines.append(f"- **{r['name']}**: {desc}")
                lines.append("")

            # Сущности из документов
            if entities:
                lines.append("## 🔖 Ключевые сущности")
                lines.append("")
                for e in entities[:30]:
                    type_str = f" [{e['type']}]" if e['type'] else ""
                    lines.append(f"- **{e['name']}**{type_str}")
                lines.append("")

            # Задачи
            if tasks_list:
                lines.append("## ✅ Задачи")
                lines.append("")
                for t in tasks_list[:25]:
                    status_str = f" [{t['status']}]" if t['status'] else ""
                    lines.append(f"- {t['name']}{status_str}")
                lines.append("")

            # Решения
            if decisions:
                lines.append("## 💡 Решения")
                lines.append("")
                for d in decisions[:20]:
                    desc = d['description'][:100] + "..." if len(d['description']) > 100 else d['description']
                    lines.append(f"- {d['name']}: {desc}")
                lines.append("")

            # Шаблоны
            if templates:
                lines.append("## 📋 Шаблоны")
                lines.append("")
                for t in templates[:15]:
                    lines.append(f"- **{t['name']}**")
                lines.append("")

            # Встречи
            if meetings:
                lines.append("## 📅 Встречи")
                lines.append("")
                for m in meetings[:15]:
                    date_str = f" ({m['date']})" if m['date'] else ""
                    lines.append(f"- {m['name']}{date_str}")
                lines.append("")

            # Люди
            if people:
                lines.append("## 👥 Участники")
                lines.append("")
                for p in people[:20]:
                    role_str = f" ({p['role']})" if p['role'] else ""
                    lines.append(f"- {p['name']}{role_str}")
                lines.append("")

            # KPI
            if kpis:
                lines.append("## 📈 KPI и метрики")
                lines.append("")
                for k in kpis[:15]:
                    value_str = f"{k['value']} {k['unit']}" if k['value'] else ""
                    lines.append(f"- **{k['name']}**: {value_str} {k['description']}")
                lines.append("")

            # Продукты
            if products:
                lines.append("## 🏷️ Продукты и системы")
                lines.append("")
                for p in products:
                    lines.append(f"### {p['name']}")
                    if p['description']:
                        lines.append(f"{p['description']}")
                    lines.append("")

            # Концепции
            if concepts:
                lines.append("## 💭 Концепции и идеи")
                lines.append("")
                for c in concepts[:20]:
                    desc = c['description'][:150] + "..." if len(c['description']) > 150 else c['description']
                    lines.append(f"- **{c['name']}**: {desc}")
                lines.append("")

            # Организации
            if organizations:
                lines.append("## 🏢 Организации")
                lines.append("")
                for o in organizations[:15]:
                    lines.append(f"- **{o['name']}**: {o['description']}")
                lines.append("")

            # Документы
            if documents:
                lines.append("## 📄 Документы")
                lines.append("")
                for d in documents[:15]:
                    lines.append(f"- {d['name']}")
                lines.append("")

            snapshot = "\n".join(lines)
            logger.info(f"   📄 Снепшот сгенерирован из графа пользователя: {total_nodes} узлов")
            return snapshot

        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка генерации снепшота: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return ""

    async def _vector_search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """Векторный поиск."""
        if not self.storage:
            return []

        try:
            # Если storage - dict с graph_builder
            if isinstance(self.storage, dict):
                # Пробуем использовать vector_indexer если есть
                vector_indexer = self.storage.get("vector_indexer")
                if vector_indexer and hasattr(vector_indexer, 'search'):
                    # Пониженный порог: критические факты (напр. прайс в презе)
                    # часто на границе 0.4–0.5 по cosine и при дефолтном 0.5
                    # НЕ доставались вовсе — RRF ранжирует, но ранжировать нечего,
                    # если не извлекли. 0.3 повышает recall; релевантность потом
                    # решает rerank. Best-effort к сигнатуре search().
                    try:
                        results = await vector_indexer.search(query, limit=top_k, score_threshold=0.3)
                    except TypeError:
                        results = await vector_indexer.search(query, limit=top_k)
                    return [{"type": "vector", "content": r, "source": "vector_indexer"} for r in results]
                return []

            # Если storage - объект с методами
            if hasattr(self.storage, 'vector_search'):
                return await self.storage.vector_search(query, limit=top_k)
            elif hasattr(self.storage, 'search'):
                # Пробуем limit, если не работает - top_k
                try:
                    return await self.storage.search(query, limit=top_k)
                except TypeError:
                    return await self.storage.search(query, top_k=top_k)
        except Exception as e:
            logger.warning(f"   ⚠️ Векторный поиск error: {e}")

        return []

    async def _graph_search(self, query: str, depth: int = 2) -> List[Dict[str, Any]]:
        """
        Графовый поиск во ВСЕХ доступных графах.

        Ищет в:
        1. Графе пользователя (задачи, документы, встречи)
        2. Системном графе (продукты, концепции)

        БЕЗ ХАРДКОДА:
        - Просто ищет по словам из запроса
        - Ищет по всем полям узла
        - Возвращает РЕАЛЬНЫЕ данные из узлов
        """
        results = []

        try:
            # Термины считаем ДО загрузки графа: из Neo4j тянем сразу те узлы,
            # где они встречаются, а не первые 1000 подряд.
            search_terms = self._expand_query_terms(query)
            if not search_terms:
                return []

            all_graphs = self._get_all_graphs(terms=search_terms)

            if not all_graphs:
                logger.warning("   ⚠️ Ни один граф не найден")
                return []

            # Ищем во всех графах
            for graph_name, nx_graph in all_graphs:
                for node_id, node_data in nx_graph.nodes(data=True):
                    # Собираем весь текст узла
                    node_text_parts = []
                    node_name = node_data.get("name", "") or node_data.get("title", "") or ""
                    node_text_parts.append(node_name)

                    # Добавляем все текстовые поля
                    text_fields = ["description", "content", "text", "summary", "_label",
                                  "entity_type", "category", "status"]
                    for field in text_fields:
                        value = node_data.get(field)
                        if value:
                            node_text_parts.append(str(value))

                    node_text_lower = " ".join(node_text_parts).lower()

                    # Проверяем совпадение с терминами из запроса
                    match_score = 0
                    matched_terms = []
                    for term in search_terms:
                        if term in node_text_lower:
                            match_score += 1
                            matched_terms.append(term)

                    if match_score > 0:
                        # Формируем РЕАЛЬНЫЙ контент из данных узла
                        content = self._format_node_as_fact(node_data)

                        results.append({
                            "type": "graph_node",
                            "node_id": node_id,
                            "name": node_name,
                            "label": node_data.get("_label", "Unknown"),
                            "content": content,  # РЕАЛЬНЫЕ данные!
                            "data": {k: v for k, v in node_data.items()
                                    if not k.startswith("_") and k not in ["embedding"]},
                            "match_score": match_score / len(search_terms) if search_terms else 0,
                            "matched_terms": matched_terms,
                            "source": f"graph:{graph_name}"  # Указываем какой граф
                        })

            # Сортируем по релевантности
            results.sort(key=lambda x: x.get("match_score", 0), reverse=True)

            logger.info(f"   ✅ Графовый поиск '{query[:30]}...': найдено {len(results)} узлов из {len(all_graphs)} графов")

            # Возвращаем топ результаты
            return results[:30]

        except Exception as e:
            logger.warning(f"   ⚠️ Графовый поиск error: {e}")
            import traceback
            logger.debug(traceback.format_exc())

        return results

    def _rerank_by_recency(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Пересортировать графовые результаты по релевантности × свежести.

        Составитель усекает raw_results[:40] / extracted_facts[:25] — без учёта
        дат свежие факты могут не попасть в окно. Ключ = match_score ×
        recency_factor(date узла). Гейт: enable_temporal_retrieval_boost.

        Гарантии (гардрейл MEMORY_DESIGN_PRINCIPLES §1):
        - флаг OFF → список возвращается КАК ЕСТЬ (байт-в-байт прежний порядок);
        - узлы без даты → factor=1.0 (буст ≤ +20%, не доминирует над релевантностью);
        - исходные dict НЕ мутируются — только сортировка (stable)."""
        try:
            from backend.core.config.feature_flags import get_feature_flags
            if not get_feature_flags().enable_temporal_retrieval_boost:
                return results
        except Exception:
            return results
        if not results:
            return results
        try:
            from backend.core.search.temporal_rerank import (
                extract_doc_date,
                recency_factor,
            )
        except Exception:
            return results

        def _key(r: Dict[str, Any]) -> float:
            base = float(r.get("match_score", 0) or 0)
            date = extract_doc_date(r.get("data") or {})
            return base * recency_factor(date)

        # stable sort: при равном ключе исходный относительный порядок сохраняется
        return sorted(results, key=_key, reverse=True)

    def _get_nx_graph(self):
        """Получить NetworkX граф из storage."""
        nx_graph = None

        if isinstance(self.storage, dict):
            nx_graph = self.storage.get("nx_graph")
            if not nx_graph:
                graph_builder = self.storage.get("graph_builder")
                if graph_builder:
                    nx_graph = getattr(graph_builder, "nx_graph", None)
        elif hasattr(self.storage, 'nx_graph'):
            nx_graph = self.storage.nx_graph
        elif hasattr(self.storage, 'graph'):
            nx_graph = getattr(self.storage.graph, 'nx_graph', None)

        return nx_graph

    def _get_graph_builder(self):
        """Получить GraphBuilder из storage.

        ВАЖНО (инцидент «Таскер сочинил про Vibe Tasking»): chat.py передаёт
        storage=_graph_builder, то есть САМ GraphBuilder. Раньше распознавались
        только dict-с-ключом и объект-с-атрибутом .graph_builder, поэтому здесь
        возвращался None → Neo4j даже не пробовался → «Ни один граф не найден»
        → 0 фактов → модель добирала недостающее из головы. Теперь принимаем и
        сам объект (утиная типизация по driver/get_all_nodes_async)."""
        if isinstance(self.storage, dict):
            return self.storage.get("graph_builder")
        if getattr(self.storage, "graph_builder", None) is not None:
            return self.storage.graph_builder
        if hasattr(self.storage, "driver") or hasattr(self.storage, "get_all_nodes_async"):
            return self.storage          # storage САМ является GraphBuilder
        return None

    def _get_all_graphs(self, terms: Optional[List[str]] = None) -> list:
        """
        Получить граф пользователя для поиска.

        Поддерживает:
        1. Neo4j (если подключен)
        2. NetworkX (fallback)

        terms — термины запроса. Заданы → из Neo4j тянем узлы, где они реально
        встречаются (иначе брались первые 1000 подряд и нужный мог не попасть).
        None → прежнее поведение (общий срез для сводки по категориям).
        """
        graphs = []

        # Проверяем, есть ли Neo4j
        graph_builder = self._get_graph_builder()
        if graph_builder and hasattr(graph_builder, 'driver') and graph_builder.driver:
            # Используем Neo4j - создаём временный NetworkX граф из данных Neo4j
            try:
                import networkx as nx
                from neo4j import GraphDatabase

                # Создаём синхронный драйвер для получения данных
                sync_driver = GraphDatabase.driver(
                    graph_builder.uri,
                    auth=(graph_builder.user, graph_builder.password)
                )

                neo4j_graph = nx.DiGraph()

                # Тенант-фильтр: раньше был голый MATCH (n) — узлы ЧУЖИХ
                # аккаунтов попадали в ТЗ. Legacy-узлы без штампа пропускаем
                # (как в graph_builder), явно чужие — отсекаем.
                _tid = getattr(self, "_current_user_id", None)
                _where = []
                _params: Dict[str, Any] = {}
                if _tid:
                    _where.append("(n.tenant_id IS NULL OR n.tenant_id = '' "
                                  "OR n.tenant_id = $tenant)")
                    _params["tenant"] = _tid
                # Релевантность: раньше брались ПЕРВЫЕ 1000 узлов подряд, и
                # нужный («vibe tasking») мог просто не попасть в срез. Если
                # известен запрос — сначала тянем узлы, где термины реально
                # встречаются в тексте.
                if terms:
                    _params["terms"] = [t.lower() for t in terms][:8]
                    _where.append(
                        "ANY(t IN $terms WHERE "
                        " toLower(coalesce(n.name,'')) CONTAINS t"
                        " OR toLower(coalesce(n.title,'')) CONTAINS t"
                        " OR toLower(coalesce(n.description,'')) CONTAINS t"
                        " OR toLower(coalesce(n.summary,'')) CONTAINS t"
                        " OR toLower(coalesce(n.content,'')) CONTAINS t)")
                _clause = (" WHERE " + " AND ".join(_where)) if _where else ""

                with sync_driver.session() as session:
                    # Получаем узлы
                    result = session.run(
                        f'''
                        MATCH (n){_clause}
                        RETURN elementId(n) as id, labels(n) as labels, properties(n) as props
                        LIMIT 1000
                    ''', _params)
                    for record in result:
                        node_id = record["id"]
                        props = dict(record["props"]) if record["props"] else {}
                        labels = list(record["labels"]) if record["labels"] else []
                        props["_label"] = labels[0] if labels else "Entity"
                        neo4j_graph.add_node(node_id, **props)

                    # Получаем связи
                    result = session.run('''
                        MATCH (a)-[r]->(b)
                        RETURN elementId(a) as from_id, elementId(b) as to_id, type(r) as rel_type
                        LIMIT 2000
                    ''')
                    for record in result:
                        _a, _b = record["from_id"], record["to_id"]
                        # nx.add_edge СОЗДАЁТ отсутствующие узлы — пустышками без
                        # свойств. С адресной выборкой узлов таких «призраков»
                        # было бы много, и они шли бы в анализ как безымянные
                        # сущности. Связываем только реально загруженные узлы.
                        if neo4j_graph.has_node(_a) and neo4j_graph.has_node(_b):
                            neo4j_graph.add_edge(_a, _b,
                                                 relationship=record["rel_type"])

                sync_driver.close()

                if neo4j_graph.number_of_nodes() > 0:
                    graphs.append(("neo4j", neo4j_graph))
                    logger.debug(f"   📊 Neo4j граф: {neo4j_graph.number_of_nodes()} узлов, {neo4j_graph.number_of_edges()} связей")

            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки Neo4j графа: {e}")

        # Fallback на NetworkX граф
        if not graphs:
            user_graph = self._get_nx_graph()
            if user_graph:
                graphs.append(("user", user_graph))
                logger.debug(f"   📊 Граф пользователя: {user_graph.number_of_nodes()} узлов")

        return graphs

    def _expand_query_terms(self, query: str) -> List[str]:
        """
        Разбивает запрос на термины для поиска.

        Без хардкода - просто разбивает на слова и убирает стоп-слова.
        """
        # Базовые термины из запроса
        words = query.lower().split()

        # Стоп-слова (общие слова которые не несут смысла для поиска)
        stop_words = {
            "и", "в", "на", "по", "для", "с", "к", "о", "у", "из", "от", "до",
            "что", "как", "это", "так", "но", "а", "или", "не", "да", "нет",
            "нашей", "наш", "наша", "наше", "нашу", "моей", "мой", "моя", "моё",
            "напиши", "создай", "сделай", "разработай", "подготовь"
        }

        # Фильтруем стоп-слова
        terms = [w for w in words if w not in stop_words and len(w) > 2]

        return terms

    def _format_node_as_fact(self, node_data: Dict[str, Any]) -> str:
        """
        Форматирует данные узла как читаемый факт.

        Вместо сырого JSON возвращает человекочитаемое описание.
        """
        label = node_data.get("_label", "Unknown")
        name = node_data.get("name", "") or node_data.get("title", "")
        description = node_data.get("description", "")

        # Формируем контент в зависимости от типа узла
        if label == "Product":
            content = f"Продукт '{name}'"
            if description:
                content += f": {description}"
            # Добавляем дополнительные поля если есть
            for field in ["features", "price", "target_audience"]:
                if node_data.get(field):
                    content += f". {field}: {node_data[field]}"
            return content

        elif label == "Concept":
            content = f"Концепция '{name}'"
            if description:
                content += f": {description}"
            return content

        elif label == "Organization":
            content = f"Организация '{name}'"
            if description:
                content += f": {description}"
            return content

        elif label == "KPI":
            value = node_data.get("value", "")
            unit = node_data.get("unit", "")
            content = f"KPI '{name}'"
            if value:
                content += f": {value}"
            if unit:
                content += f" {unit}"
            if description:
                content += f" ({description})"
            return content

        elif label == "Task":
            status = node_data.get("status", "")
            assignee = node_data.get("assignee", "")
            content = f"Задача '{name}'"
            if status:
                content += f" [статус: {status}]"
            if assignee:
                content += f" [исполнитель: {assignee}]"
            if description:
                content += f": {description}"
            return content

        elif label == "Decision":
            content = f"Решение: {name}"
            if description:
                content += f" - {description}"
            return content

        elif label == "Person":
            role = node_data.get("role", "")
            content = f"Участник '{name}'"
            if role:
                content += f" ({role})"
            return content

        elif label == "Meeting":
            date = node_data.get("date", "") or node_data.get("created_at", "")
            summary = node_data.get("summary", "")
            content = "Встреча"
            if name:
                content += f" '{name}'"
            if date:
                content += f" [{date[:10] if len(str(date)) > 10 else date}]"
            if summary:
                content += f": {summary[:200]}"
            return content

        else:
            # Общий формат
            content = f"{label}: {name}"
            if description:
                content += f" - {description}"
            return content

    async def _generate_llm_response(self, prompt: str) -> Dict[str, Any]:
        """Генерация ответа через LLM."""
        if not self.llm_router:
            logger.warning("   ⚠️ LLM Router не инициализирован")
            self._llm_failures = getattr(self, "_llm_failures", 0) + 1
            return {}

        system_prompt = "Ты — эксперт по анализу данных и созданию ТЗ. Возвращай ТОЛЬКО валидный JSON."
        try:
            from backend.config import get_settings
            if get_settings().human_thinking_enabled:
                from backend.core.think.human_thinking import with_thinking_discipline
                system_prompt = with_thinking_discipline(system_prompt, "specify")
        except Exception as e:
            logger.debug(f"   thinking-discipline skipped: {e}")

        async def _legacy(_p) -> str:
            return await self.llm_router.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.7,
                max_tokens=4096
            )

        try:
            # ТЗ — качество-критичный артефакт → premium-модель тенанта
            # (workload_policy 'tz_generation', 3-слойная Google-страховка).
            # ЗА флагом; по умолчанию/при сбое — прежний llm_router (flash-lite).
            response = None
            try:
                from backend.core.config import flags as _flags
                uid = getattr(self, "_current_user_id", None)
                if uid and getattr(_flags, "enable_tiered_extraction", False):
                    from backend.core.llm.workload_policy import generate_for_workload
                    response = await generate_for_workload(
                        uid, "tz_generation", f"{system_prompt}\n\n{prompt}",
                        fallback_generate=_legacy)
            except Exception as _e:
                logger.debug(f"   tz workload route fallback: {_e}")
            if not response:
                response = await _legacy(None)

            return self._parse_json_response(response)

        except Exception as e:
            logger.error(f"   ❌ LLM error: {e}")
            self._llm_failures = getattr(self, "_llm_failures", 0) + 1
            return {"error": str(e)}

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Парсинг JSON из ответа LLM."""
        if not response:
            return {}

        import re

        # Пробуем напрямую
        try:
            return json.loads(response)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # Ищем JSON в markdown
        json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', response)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # Ищем JSON объект
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # Аудит #2: невалидный JSON → секции ТЗ теряются. Считаем как деградацию.
        self._llm_failures = getattr(self, "_llm_failures", 0) + 1
        logger.warning("   ⚠️ LLM вернул невалидный JSON — секции потеряны")
        return {"raw_text": response}

    def _merge_data(self, existing: Dict, new: Dict) -> Dict:
        """Объединение данных из разных поисков."""
        if not new:
            return existing
        if not existing:
            return new

        merged = {
            "raw_results": existing.get("raw_results", []) + new.get("raw_results", []),
            "categorized_data": existing.get("categorized_data", {}).copy(),
            "sources_used": list(set(existing.get("sources_used", []) + new.get("sources_used", []))),
            "stats": existing.get("stats", {}).copy()
        }

        # Объединяем категории
        for cat, items in new.get("categorized_data", {}).items():
            if cat not in merged["categorized_data"]:
                merged["categorized_data"][cat] = []
            merged["categorized_data"][cat].extend(items)

        # Обновляем статистику
        merged["stats"]["total_facts"] = len(merged["raw_results"])
        merged["stats"]["total_sources"] = len(merged["sources_used"])

        return merged

    def _generate_markdown(
        self,
        result: Dict[str, Any],
        task_description: str,
        context: Dict[str, Any]
    ) -> str:
        """Генерация Markdown документа."""
        lines = []

        # Заголовок
        lines.append(f"# {result.get('title', 'Техническое задание')}")
        lines.append("")
        lines.append(f"**Задача:** {task_description}")
        lines.append(f"**Дата:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("")

        # Executive Summary
        if result.get("executive_summary"):
            lines.append("## Executive Summary")
            lines.append("")
            lines.append(result["executive_summary"])
            lines.append("")

        # Разделы
        for section in result.get("sections", []):
            lines.append(f"## {section.get('title', 'Раздел')}")
            lines.append("")
            lines.append(section.get("content", ""))

            sources = section.get("data_sources", [])
            if sources:
                lines.append("")
                lines.append(f"*Источники: {', '.join(sources)}*")

            confidence = section.get("confidence", "")
            if confidence:
                lines.append(f"*Уверенность: {confidence}*")

            lines.append("")

        # Использованные данные
        data_used = result.get("data_used", {})
        if data_used:
            lines.append("## 📊 Использованные данные")
            lines.append("")
            lines.append(f"- Фактов: {data_used.get('facts_count', 0)}")
            lines.append(f"- Мнений: {data_used.get('opinions_count', 0)}")
            lines.append(f"- Числовых данных: {data_used.get('numbers_count', 0)}")
            lines.append(f"- Предположений: {data_used.get('assumptions_count', 0)}")
            lines.append("")

        # Предположения
        assumptions = context.get("assumptions", [])
        if assumptions:
            lines.append("## ⚠️ Предположения (требуют проверки)")
            lines.append("")
            for a in assumptions[:10]:
                lines.append(f"- **{a.get('assumption', '')}**")
                lines.append(f"  - Основано на: {a.get('based_on', 'N/A')}")
                lines.append(f"  - Уверенность: {a.get('confidence', 'N/A')}")
            lines.append("")

        # Ограничения
        limitations = result.get("limitations", [])
        if limitations:
            lines.append("## ⚠️ Ограничения")
            lines.append("")
            for lim in limitations:
                lines.append(f"- {lim}")
            lines.append("")

        # Рекомендации
        recommendations = result.get("recommendations", [])
        if recommendations:
            lines.append("## 💡 Рекомендации")
            lines.append("")
            # REUSE readiness-gates / Q0: скелет гейтит рекомендации LLM — Q0
            # вверх, «лучше руками» помечается. За флагом; claim-guard (без
            # уверенного сигнала — чистый текст). OFF → секция прежняя.
            gated = False
            try:
                from backend.config import get_settings
                if get_settings().readiness_gates_enabled:
                    from backend.core.think.readiness_gates import (
                        annotate_recommendations,
                        render_recommendation_line,
                    )
                    for a in annotate_recommendations(recommendations):
                        lines.append(render_recommendation_line(a))
                    gated = True
            except Exception as e:
                logger.debug(f"   readiness-gates skipped: {e}")
            if not gated:
                for rec in recommendations:
                    lines.append(f"- {rec}")
            lines.append("")

        # Следующие шаги
        next_steps = result.get("next_steps", [])
        if next_steps:
            lines.append("## ➡️ Следующие шаги")
            lines.append("")
            for i, step in enumerate(next_steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

        # Футер
        lines.append("---")
        lines.append("")
        lines.append("*Документ создан системой DataDrivenTaskSystem на основе реальных данных компании.*")

        return "\n".join(lines)

    async def _save_result(self, result: Dict[str, Any], task_id: str) -> Dict[str, str]:
        """Сохранение результата.

        json.dump — с default=str: факты из графа несут neo4j DateTime и другие
        несериализуемые типы («Object of type DateTime is not JSON serializable»),
        из-за чего раньше падало ВСЁ сохранение (и markdown тоже) → ТЗ не
        открывалось на странице и его нельзя было подтвердить/отправить.
        Markdown пишем ПЕРВЫМ и в отдельном try — он и есть документ ТЗ."""
        paths = {}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Markdown (главный артефакт) — независимо от JSON
        try:
            final_result = result.get("stages", {}).get("result", {})
            if final_result.get("markdown"):
                md_path = self.output_dir / f"TASK_{task_id}_{timestamp}.md"
                with open(md_path, 'w', encoding='utf-8') as f:
                    f.write(final_result["markdown"])
                paths["markdown"] = str(md_path)
        except Exception as e:
            logger.error(f"   ❌ Ошибка сохранения markdown: {e}")

        # JSON (метаданные/факты)
        try:
            json_path = self.output_dir / f"TASK_{task_id}_{timestamp}.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                # Убираем большие данные
                save_data = {k: v for k, v in result.items() if k != "stages"}
                save_data["stages_summary"] = list(result.get("stages", {}).keys())
                json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
            paths["json"] = str(json_path)
        except Exception as e:
            logger.error(f"   ❌ Ошибка сохранения JSON: {e}")

        if paths:
            logger.info(f"   💾 Файлы сохранены: {list(paths.keys())}")

        return paths

    def _generate_task_id(self) -> str:
        """Генерация ID задачи."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]
        return f"{timestamp}_{unique}"

