# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Document Indexer
Сервис индексации документов в векторную базу для поиска

Включает:
- Chunking документов на части
- Создание эмбеддингов для каждого чанка
- Сохранение в граф знаний
- Интеграция с общим поиском Brain
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Настройки чанкинга
DEFAULT_CHUNK_SIZE = 1500  # символов
DEFAULT_CHUNK_OVERLAP = 200  # символов


def split_text_into_chunks(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP
) -> List[Dict[str, Any]]:
    """
    Разбить текст на чанки с перекрытием.

    Args:
        text: Исходный текст
        chunk_size: Размер чанка в символах
        overlap: Перекрытие между чанками

    Returns:
        Список чанков с метаданными
    """
    if not text or not text.strip():
        return []

    # Нормализуем текст
    text = text.strip()

    # Если текст короткий, возвращаем как один чанк
    if len(text) <= chunk_size:
        return [{
            "index": 0,
            "text": text,
            "char_start": 0,
            "char_end": len(text),
            "total_chunks": 1
        }]

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        # Определяем конец чанка
        end = start + chunk_size

        # Если это не последний чанк, ищем хорошую точку разрыва
        if end < len(text):
            # Ищем конец предложения или абзаца
            best_break = end

            # Приоритет: конец абзаца
            paragraph_break = text.rfind('\n\n', start, end)
            if paragraph_break > start + chunk_size // 2:
                best_break = paragraph_break + 2
            else:
                # Ищем конец предложения
                for sep in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
                    sentence_break = text.rfind(sep, start, end)
                    if sentence_break > start + chunk_size // 2:
                        best_break = sentence_break + len(sep)
                        break
                else:
                    # Ищем пробел
                    space_break = text.rfind(' ', start, end)
                    if space_break > start + chunk_size // 2:
                        best_break = space_break + 1

            end = best_break
        else:
            end = len(text)

        chunk_text = text[start:end].strip()

        if chunk_text:
            chunks.append({
                "index": chunk_index,
                "text": chunk_text,
                "char_start": start,
                "char_end": end,
            })
            chunk_index += 1

        # Дошли до конца текста — последний чанк уже добавлен, выходим. Без
        # этого при коротком «хвосте» (меньше overlap) start полз бы по +1
        # символу до конца и плодил сотни почти одинаковых чанков (5 КБ → 205!):
        # end упирается в len(text), а end-overlap ≤ start, поэтому шаг = +1.
        if end >= len(text):
            break

        # Следующий чанк начинается с перекрытием
        start = max(start + 1, end - overlap)

    # Добавляем total_chunks к каждому чанку
    total = len(chunks)
    for chunk in chunks:
        chunk["total_chunks"] = total

    return chunks


class DocumentIndexer:
    """
    Индексатор документов в граф знаний и векторную базу.

    Пример использования:
    ```python
    indexer = DocumentIndexer(user_id="...")
    await indexer.initialize()

    result = await indexer.index_document(
        document_id="doc_123",
        title="Мой документ",
        content="Содержимое документа...",
        doc_type="text"
    )

    # Поиск по документам
    results = await indexer.search_documents("запрос")
    ```
    """

    def __init__(
        self,
        user_id: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    ):
        self.user_id = user_id
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.graph_builder = None
        self.vector_indexer = None
        self._initialized = False

    async def initialize(self):
        """Инициализация компонентов"""
        if self._initialized:
            return

        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user
        from backend.core.store.vector_indexer import VectorIndexer

        # Используем tenant-aware пути для изоляции данных пользователя
        self.graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect: uses Neo4j if available
            graph_storage_path=graph_path_for_user(self.user_id)
        )
        await self.graph_builder.connect()

        from backend.core.search.bm25_searcher import get_bm25_searcher
        self.bm25_searcher = get_bm25_searcher(user_id=self.user_id)
        self.vector_indexer = VectorIndexer(
            use_qdrant=False,
            namespace=self.user_id,
            storage_path=vector_index_path_for_user(self.user_id)
        )
        await self.vector_indexer.connect()

        self._initialized = True
        logger.info(f"✅ DocumentIndexer initialized for user {self.user_id}")

    async def index_document(
        self,
        document_id: str,
        title: str,
        content: str,
        doc_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        context: str = "document"
    ) -> Dict[str, Any]:
        """
        Индексировать документ в граф и векторную базу.

        Args:
            document_id: ID документа
            title: Название документа
            content: Содержимое документа
            doc_type: Тип документа
            metadata: Дополнительные метаданные

        Returns:
            Статистика индексации
        """
        if not self._initialized:
            await self.initialize()

        result = {
            "document_id": document_id,
            "chunks_created": 0,
            "vectors_created": 0,
            "graph_nodes_created": 0,
            "errors": []
        }

        try:
            # 1. Создаём узел документа в графе
            doc_node_id = f"document_{document_id}"

            await self.graph_builder.create_node(
                node_id=doc_node_id,
                label="Document",
                properties={
                    "document_id": document_id,
                    "title": title,
                    "doc_type": doc_type,
                    "user_id": self.user_id,
                    "content_length": len(content),
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                    **(metadata or {})
                }
            )
            result["graph_nodes_created"] += 1

            # 2. Индексируем документ целиком для общего поиска
            doc_text = f"Документ: {title}\n\nТип: {doc_type}\n\n{content[:500]}..."

            await self.vector_indexer.upsert_vector_public(
                collection="tessent_documents",
                vector_id=doc_node_id,
                text=doc_text,
                metadata={
                    "document_id": document_id,
                    "title": title,
                    "doc_type": doc_type,
                    "user_id": self.user_id,
                    "type": "document"
                }
            )
            result["vectors_created"] += 1

            # 2b. BM25 для документа: раньше документы вообще не попадали в
            # лексический канал гибрида (только векторный) — по точному
            # термину/номеру/названию их было не найти. Метаданные несут
            # document_id, чтобы работал фильтр выбора документов в чате.
            try:
                self.bm25_searcher.add_document(
                    doc_id=doc_node_id,
                    text=f"{title}\n{content[:4000]}",
                    metadata={"type": "document", "document_id": document_id,
                              "title": title, "doc_type": doc_type})
            except Exception as _be:
                logger.debug(f"bm25 document add skipped: {_be}")

            # 3. Разбиваем на чанки. Контекст-зависимая гранулярность за флагом OFF
            # (MEMORY_DESIGN_PRINCIPLES §6 №6): профиль 'document' == текущие
            # DEFAULT → при context='document' поведение идентично. Best-effort.
            chunk_size, chunk_overlap = self.chunk_size, self.chunk_overlap
            try:
                from backend.core.config.feature_flags import get_feature_flags
                if get_feature_flags().enable_context_granularity:
                    from backend.core.chunking.granularity_policy import chunk_kwargs
                    kw = chunk_kwargs(context)
                    chunk_size, chunk_overlap = kw["chunk_size"], kw["overlap"]
            except Exception as e:
                logger.debug(f"granularity policy skipped (non-critical): {e}")

            chunks = split_text_into_chunks(
                content,
                chunk_size=chunk_size,
                overlap=chunk_overlap
            )

            result["chunks_created"] = len(chunks)

            # C10: prior компании для table-aware индексации (один раз на документ).
            # Best-effort: без флага / без снапшота — пусто.
            _table_prior: Optional[Dict[str, Any]] = None
            try:
                from backend.core.config.feature_flags import get_feature_flags
                if get_feature_flags().enable_table_aware_indexing:
                    from backend.core.sleep.enhanced_snapshot import (
                        get_enhanced_snapshot_generator,
                    )
                    from backend.core.think.table_understanding_agent import (
                        build_schema_prior,
                    )
                    gen = get_enhanced_snapshot_generator(user_id=self.user_id)
                    snap = await gen.get_company_snapshot()
                    if snap and hasattr(snap, "to_dict"):
                        _table_prior = build_schema_prior(snap.to_dict())
            except Exception as e:
                logger.debug(f"table-aware prior unavailable: {e}")

            # 4. Индексируем каждый чанк
            for chunk in chunks:
                chunk_id = f"doc_chunk_{document_id}_{chunk['index']}"

                # Текст для индексации с контекстом
                chunk_text = f"[Документ: {title}] {chunk['text']}"

                chunk_meta = {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "document_title": title,
                    "chunk_index": chunk["index"],
                    "total_chunks": chunk["total_chunks"],
                    "char_start": chunk["char_start"],
                    "char_end": chunk["char_end"],
                    "user_id": self.user_id,
                    "type": "document_chunk",
                    "source": "document"
                }

                # C10: если в чанке таблица — добавляем понимание (типы колонок,
                # сущности компании). Best-effort; не роняет индексацию.
                if _table_prior is not None:
                    try:
                        from backend.core.documents.table_aware_indexing import (
                            enrich_chunk_metadata,
                        )
                        chunk_meta = enrich_chunk_metadata(
                            chunk["text"], chunk_meta, prior=_table_prior)
                    except Exception as e:
                        logger.debug(f"table-aware chunk skipped: {e}")

                # Индексируем в векторную базу
                await self.vector_indexer.upsert_vector_public(
                    collection="tessent_chunks",
                    vector_id=chunk_id,
                    text=chunk_text,
                    metadata=chunk_meta
                )
                result["vectors_created"] += 1
                # чанк документа → BM25 (тот же лексический канал, что чанки
                # встреч); document_id в метаданных для фильтра выбора
                try:
                    self.bm25_searcher.add_document(
                        doc_id=f"chunk_{chunk_id}",
                        text=chunk["text"],
                        metadata={"type": "document_chunk",
                                  "document_id": document_id,
                                  "document_title": title,
                                  "source": "document"})
                except Exception as _be:
                    logger.debug(f"bm25 chunk add skipped: {_be}")

                # Создаём связь чанка с документом в графе
                chunk_node_id = f"chunk_{chunk_id}"
                await self.graph_builder.create_node(
                    node_id=chunk_node_id,
                    label="DocumentChunk",
                    properties={
                        "chunk_id": chunk_id,
                        "document_id": document_id,
                        "chunk_index": chunk["index"],
                        "char_start": chunk["char_start"],
                        "char_end": chunk["char_end"],
                        "user_id": self.user_id
                    }
                )
                result["graph_nodes_created"] += 1

                # Связываем чанк с документом
                await self.graph_builder.create_relationship(
                    from_id=doc_node_id,
                    to_id=chunk_node_id,
                    rel_type="HAS_CHUNK"
                )

            # 4b. ЛЁГКАЯ СШИВКА С ГРАФОМ (за флагом, дефолт OFF — ноль
            # LLM-стоимости по умолчанию). Включён TESSENT_DOCUMENT_GRAPH_EXTRACT
            # → ОДИН вызов EntityAgent извлекает людей/компании/проекты из
            # документа, резолвит к КАНОНИЧЕСКИМ узлам встреч (EntityResolver)
            # и ставит Document -MENTIONS-> Entity. Так файл перестаёт быть
            # «отдельным островом» и сшивается с тем, что «сказано». Никогда
            # не роняет индексацию.
            if os.getenv("TESSENT_DOCUMENT_GRAPH_EXTRACT", "").lower() in (
                    "on", "true", "1"):
                try:
                    mentions = await self._extract_and_link_entities(
                        doc_node_id, title, content)
                    result["graph_nodes_created"] += mentions.get("nodes", 0)
                    result["entities_linked"] = mentions.get("linked", 0)
                except Exception as _ee:
                    logger.warning(f"document graph-extract skipped: {_ee}")

            # 5. Сохраняем граф
            await self.graph_builder.save()

            # 6. Сохраняем векторный индекс + BM25
            self.vector_indexer.save_index()
            try:
                self.bm25_searcher.save_index()
            except Exception as _be:
                logger.debug(f"bm25 save skipped: {_be}")

            # 7. P2 мульти-аккаунта: документ участника орги — в общий мозг
            # (как встречи: узел+чанки копируются с уровнями/отделом автора).
            # Личные источники (PERSONAL_SOURCES) не поднимаются; best-effort.
            try:
                from backend.core.ingest.meeting_promote import (
                    promote_document_to_org,
                    should_promote,
                )
                _source = (metadata or {}).get("source")
                org_id = should_promote(self.user_id, _source)
                if org_id:
                    promo = await promote_document_to_org(
                        user_id=self.user_id, document_id=document_id,
                        org_id=org_id, source=_source)
                    result["org_promotion"] = {
                        "promoted": promo.get("promoted"),
                        "nodes": promo.get("nodes_copied"),
                    }
            except Exception as e:
                logger.debug(f"document org-promotion skipped: {e}")

            # 8. ГЛУБОКИЙ АНАЛИЗ КАК У ВСТРЕЧИ (за флагом, дефолт OFF → ноль
            # изменений в текущем поведении). Люди грузят обычные документы
            # (презентации, прайсы, договоры) — «мозг» должен понимать их так же,
            # как встречи: извлечь факты/цены/сущности, разрешить их к
            # каноническим узлам, построить снапшот продукта и граф. Тогда цена
            # «2,2 ₽/мин» из презентации становится НАСТОЯЩИМ фактом в графе и
            # снапшоте (а не только слабым вектор-чанком таблицы) — и поиск ТЗ
            # находит её по смыслу. Документ подаётся как транскрипт в ТОТ ЖЕ
            # CaptureOrchestrator и сохраняется тем же _save_orchestrator_
            # results_to_graph, что и встречи. Best-effort: никогда не роняет
            # индексацию (базовый индекс уже сохранён на шагах 5–6).
            if os.getenv("TESSENT_DOCUMENT_DEEP_EXTRACT", "").lower() in (
                    "on", "true", "1"):
                try:
                    deep = await self._deep_analyze_like_meeting(
                        document_id, title, content, metadata)
                    result["deep_extract"] = deep
                except Exception as _de:
                    logger.warning(f"document deep-analyze skipped: {_de}")

            logger.info(
                f"✅ Document indexed: {title} ({document_id}) - "
                f"{result['chunks_created']} chunks, "
                f"{result['vectors_created']} vectors"
            )

        except Exception as e:
            logger.error(f"❌ Error indexing document {document_id}: {e}")
            result["errors"].append(str(e))

        return result

    async def _extract_and_link_entities(
        self, doc_node_id: str, title: str, content: str
    ) -> Dict[str, int]:
        """B4b-light: 1 LLM-вызов → сущности документа → резолв к
        каноническим узлам → Document -MENTIONS-> Entity. Best-effort."""
        from backend.core.capture.agents.entity_agent import EntityAgent
        from backend.core.llm.router import get_llm_router
        from backend.core.store.entity_resolver import EntityResolver

        stats = {"nodes": 0, "linked": 0}
        agent = EntityAgent()
        agent.set_llm(get_llm_router())
        # документ — это текст; заголовок в начало для контекста
        text = f"{title}\n\n{content}"[:20000]
        entities = await agent.extract(text, {"source": "document"})
        if not entities:
            return stats

        # линкуем только «якорные» типы, где резолвер осмыслен
        LINKABLE = {"person", "project", "company", "organization",
                    "product", "team", "department"}
        resolver = EntityResolver(graph_builder=self.graph_builder,
                                  vector_indexer=self.vector_indexer)
        seen = set()
        for ent in entities[:20]:
            etype = str(ent.get("entity_type") or "").lower()
            name = (ent.get("name") or "").strip()
            if not name or etype not in LINKABLE:
                continue
            key = (etype, name.lower())
            if key in seen:
                continue
            seen.add(key)
            try:
                resolved = await resolver.resolve(
                    name=name, entity_type=etype,
                    organization_id=self.user_id)
                canonical_id = getattr(resolved, "canonical_id", None)
                if not canonical_id:
                    continue
                await self.graph_builder.create_relationship(
                    from_id=doc_node_id, to_id=canonical_id,
                    rel_type="MENTIONS",
                    properties={"source": "document", "entity_type": etype})
                stats["linked"] += 1
            except Exception:
                logger.debug("document entity link failed", exc_info=True)
        return stats

    async def _deep_analyze_like_meeting(
        self,
        document_id: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Прогнать документ через ТОТ ЖЕ конвейер «мозга», что и встречу.

        Документ подаётся как транскрипт в CaptureOrchestrator (те же ~20
        агентов: сущности/решения/задачи/KPI/knowledge/…), затем результат
        сохраняется ТЕМ ЖЕ `_save_orchestrator_results_to_graph`, что и встречи
        — с entity resolution, temporal-версионированием, вектором, BM25 и
        обновлением снапшота компании/продукта. Так информация из документа
        «верно достаётся» (как в синхронизации встреч), «верно обобщается»
        (снапшоты/граф) и потом «верно ищется» (по смыслу/снапшотам/связям).

        Best-effort. Граф/вектор берём общие с индексатором (память
        подкрепляется на тех же узлах). Возвращает краткую статистику.
        """
        from backend.core.capture.orchestrator import CaptureOrchestrator
        from backend.core.knowledge_sync import (
            _save_orchestrator_results_to_graph,
        )

        text = (content or "").strip()
        if not text:
            return {"skipped": "empty"}

        # Псевдо-«источник» из документа: отдельный префикс id и source=document,
        # чтобы узел-источник был отличим от реальных встреч в графе/снапшоте.
        doc_source_id = f"docsource_{document_id}"
        meeting_context = {
            "meeting_id": doc_source_id,
            "title": title or "Документ",
            "date": (metadata or {}).get("date"),
            "user_id": self.user_id,
            "participants": [],
            "source": "document",
            "document_id": document_id,
            # для полнотекстовой индексации чанками внутри пайплайна
            "transcript": text,
        }

        orchestrator = CaptureOrchestrator(
            graph_builder=self.graph_builder,
            vector_indexer=self.vector_indexer,
        )
        # co-occurrence откладываем в _save_orchestrator_results_to_graph —
        # там появляются реальные id узлов (как в потоке встреч).
        orchestrator.defer_cooccurrence = True

        # ЭКОНОМИЯ LLM: у документа нет «участников/динамики встречи», поэтому
        # митинг-поведенческие фазы для него бессмысленны и просто жгут вызовы
        # (в логах давали 0 профилей / 0 целей / 0 решений / 0 critical). Гасим
        # их и оставляем только то, что реально достаёт факты из документа:
        # извлечение сущностей/KPI (это и вытащило цены), knowledge/regulation/
        # template-экстракторы, cross-analysis и метаданные поиска.
        try:
            if isinstance(getattr(orchestrator, "config", None), dict):
                orchestrator.config.update({
                    "enable_psychological_analysis": False,
                    "enable_future_prediction": False,
                    "enable_intelligence_weights": False,
                    "enable_goals_tracking": False,
                    "enable_decision_learning": False,
                    "enable_synthesis_analysis": False,
                    "enable_cross_meeting_links": False,
                })
        except Exception as _ce:
            logger.debug(f"doc orchestrator config trim skipped: {_ce}")

        analysis = await orchestrator.process_meeting(
            transcript=text,
            meeting_id=doc_source_id,
            meeting_context=meeting_context,
        )

        stats: Dict[str, Any] = {"entities_created": 0, "snapshot_updated": False}
        if analysis:
            # Тот же персист, что и для встреч: типизированные узлы + entity
            # resolution + temporal + вектор + BM25 + апдейт снапшота.
            saved = await _save_orchestrator_results_to_graph(
                analysis_result=analysis,
                meeting_id=doc_source_id,
                user_id=self.user_id,
                meeting_context=meeting_context,
            )
            stats["entities_created"] = saved.get("entities_created", 0)
            stats["snapshot_updated"] = saved.get("snapshot_updated", False)
        logger.info(
            "🧠 Документ проанализирован как встреча: %s — сущностей=%s, снапшот=%s",
            title, stats["entities_created"], stats["snapshot_updated"])
        return stats

    async def delete_document_index(self, document_id: str) -> Dict[str, Any]:
        """
        Удалить индекс документа из графа и векторной базы.

        Args:
            document_id: ID документа

        Returns:
            Статистика удаления
        """
        if not self._initialized:
            await self.initialize()

        result = {
            "document_id": document_id,
            "vectors_deleted": 0,
            "nodes_deleted": 0
        }

        try:
            doc_node_id = f"document_{document_id}"

            # Удаляем из векторной базы
            # TODO: Implement delete in VectorIndexer

            # Удаляем из графа
            if hasattr(self.graph_builder, 'delete_node'):
                await self.graph_builder.delete_node(doc_node_id)
                result["nodes_deleted"] += 1

            logger.info(f"✅ Document index deleted: {document_id}")

        except Exception as e:
            logger.error(f"❌ Error deleting document index {document_id}: {e}")

        return result

    async def search_documents(
        self,
        query: str,
        limit: int = 10,
        doc_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Поиск по индексированным документам.

        Args:
            query: Поисковый запрос
            limit: Максимальное количество результатов
            doc_type: Фильтр по типу документа

        Returns:
            Список найденных чанков с метаданными
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Поиск по чанкам документов
            filters = {
                "user_id": self.user_id,
                "source": "document"
            }

            if doc_type:
                filters["doc_type"] = doc_type

            results = await self.vector_indexer.search(
                query=query,
                collection="tessent_chunks",
                limit=limit,
                filters=filters
            )

            return results

        except Exception as e:
            logger.error(f"❌ Error searching documents: {e}")
            return []

    async def get_document_context(
        self,
        document_id: str,
        query: Optional[str] = None,
        max_chunks: int = 5
    ) -> str:
        """
        Получить контекст документа для чата.

        Args:
            document_id: ID документа
            query: Опциональный запрос для фильтрации релевантных чанков
            max_chunks: Максимальное количество чанков

        Returns:
            Текстовый контекст документа
        """
        if not self._initialized:
            await self.initialize()

        try:
            if query:
                # Ищем релевантные чанки
                results = await self.vector_indexer.search(
                    query=query,
                    collections=["tessent_chunks"],
                    limit=max_chunks,
                    filter_conditions={
                        "user_id": self.user_id,
                        "document_id": document_id
                    }
                )

                if results:
                    chunks_text = []
                    for r in results:
                        text = r.get("text", r.get("payload", {}).get("text", ""))
                        chunks_text.append(text)
                    return "\n\n---\n\n".join(chunks_text)

            # Если нет запроса, берём первые чанки
            results = await self.vector_indexer.search(
                query=f"document {document_id}",
                collections=["tessent_chunks"],
                limit=max_chunks,
                filter_conditions={
                    "user_id": self.user_id,
                    "document_id": document_id
                }
            )

            if results:
                # Сортируем по индексу чанка
                sorted_results = sorted(
                    results,
                    key=lambda x: x.get("chunk_index", x.get("payload", {}).get("chunk_index", 0))
                )
                chunks_text = []
                for r in sorted_results:
                    text = r.get("text", r.get("payload", {}).get("text", ""))
                    chunks_text.append(text)
                return "\n\n".join(chunks_text)

            return ""

        except Exception as e:
            logger.error(f"❌ Error getting document context: {e}")
            return ""


async def sync_documents_to_knowledge(
    user_id: str,
    document_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Синхронизировать документы в базу знаний.

    Args:
        user_id: ID пользователя
        document_ids: Список ID документов для синхронизации (None = все)

    Returns:
        Статистика синхронизации
    """
    from backend.db.supabase_client import get_supabase_client

    supabase = get_supabase_client()
    indexer = DocumentIndexer(user_id=user_id)
    await indexer.initialize()

    stats = {
        "total": 0,
        "synced": 0,
        "errors": 0,
        "documents": []
    }

    try:
        # Получаем документы для синхронизации
        params = {
            "user_id": f"eq.{user_id}",
            "select": "*"
        }

        if document_ids:
            params["id"] = f"in.({','.join(document_ids)})"
        else:
            # Только не синхронизированные
            params["sync_status"] = "in.(not_synced,error)"

        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params=params
        )

        stats["total"] = len(documents)

        for doc in documents:
            doc_id = doc.get("id")
            title = doc.get("title", "Без названия")
            content = doc.get("content", "")
            doc_type = doc.get("doc_type", "text")

            if not content:
                logger.warning(f"⚠️ Document {doc_id} has no content, skipping")
                continue

            try:
                # Обновляем статус на syncing
                await supabase._request(
                    "PATCH",
                    "/rest/v1/documents",
                    params={"id": f"eq.{doc_id}"},
                    json_data={"sync_status": "syncing"}
                )

                # Индексируем документ
                result = await indexer.index_document(
                    document_id=doc_id,
                    title=title,
                    content=content,
                    doc_type=doc_type,
                    metadata=doc.get("metadata", {})
                )

                # Обновляем статус на synced
                await supabase._request(
                    "PATCH",
                    "/rest/v1/documents",
                    params={"id": f"eq.{doc_id}"},
                    json_data={
                        "sync_status": "synced",
                        "synced_at": datetime.now(timezone.utc).isoformat(),
                        "chunks_count": result.get("chunks_created", 0)
                    }
                )

                stats["synced"] += 1
                stats["documents"].append({
                    "id": doc_id,
                    "title": title,
                    "chunks": result.get("chunks_created", 0),
                    "status": "synced"
                })

                logger.info(f"✅ Document synced: {title}")

            except Exception as e:
                logger.error(f"❌ Error syncing document {doc_id}: {e}")

                # Обновляем статус на error
                await supabase._request(
                    "PATCH",
                    "/rest/v1/documents",
                    params={"id": f"eq.{doc_id}"},
                    json_data={
                        "sync_status": "error",
                        "metadata": {**doc.get("metadata", {}), "sync_error": str(e)}
                    }
                )

                stats["errors"] += 1
                stats["documents"].append({
                    "id": doc_id,
                    "title": title,
                    "status": "error",
                    "error": str(e)
                })

        logger.info(
            f"✅ Documents sync completed: {stats['synced']}/{stats['total']} synced, "
            f"{stats['errors']} errors"
        )

    except Exception as e:
        logger.error(f"❌ Error in sync_documents_to_knowledge: {e}")
        stats["error"] = str(e)

    return stats

