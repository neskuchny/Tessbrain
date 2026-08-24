# -*- coding: utf-8 -*-
"""
DocumentGenerationSystem — главный оркестратор генерации документов.

Поддерживает два режима:
1. REAL-TIME: Быстрая классификация при обработке встречи
2. BACKGROUND: Полная генерация документов из графа знаний

Гибридный подход:
- При синхронизации: помечаем кандидатов на регламент
- Ночью: генерируем/обновляем документы из накопленных данных
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.core.llm.usage_tracker import UsageContext

from .content_aggregator_agent import ContentAggregatorAgent
from .document_writer_agent import DocumentWriterAgent, GeneratedDocument
from .regulation_classifier_agent import (
    ClassificationResult,
    RegulationClassifierAgent,
)
from .topic_clustering_agent import TopicCluster, TopicClusteringAgent

logger = logging.getLogger(__name__)


@dataclass
class DocumentCandidate:
    """Кандидат на создание документа (помечается при real-time обработке)."""
    candidate_id: str
    meeting_id: str
    meeting_title: str
    classification: ClassificationResult
    created_at: datetime
    processed: bool = False


@dataclass
class DocumentGenerationResult:
    """Результат генерации документов."""
    documents: List[GeneratedDocument] = field(default_factory=list)
    candidates_found: int = 0
    clusters_created: int = 0
    documents_generated: int = 0
    documents_updated: int = 0
    errors: List[str] = field(default_factory=list)


class DocumentGenerationSystem:
    """
    Система генерации документов из встреч.

    Режимы работы:
    1. quick_classify() — быстрая классификация при real-time обработке
    2. discover_and_generate() — полный цикл обнаружения и генерации (для ночной обработки)
    3. generate_for_topic() — генерация документа по конкретной теме
    4. update_document() — обновление существующего документа новыми данными
    """

    def __init__(self, llm_router, graph_builder=None, vector_indexer=None):
        self.llm = llm_router
        self.graph = graph_builder
        self.vectors = vector_indexer

        # Инициализация агентов
        self.classifier = RegulationClassifierAgent(llm_router)
        self.clusterer = TopicClusteringAgent(llm_router)
        self.aggregator = ContentAggregatorAgent(llm_router)
        self.writer = DocumentWriterAgent(llm_router)

        # Хранилище кандидатов и документов (в памяти, можно заменить на БД)
        self.candidates: Dict[str, DocumentCandidate] = {}
        self.documents: Dict[str, GeneratedDocument] = {}

        # Путь к файлу для автосохранения
        from pathlib import Path
        data_dir = Path(__file__).parent.parent.parent.parent / "data"
        data_dir.mkdir(exist_ok=True)
        self._storage_file = str(data_dir / "generated_documents.json")

        # Автозагрузка при инициализации
        self.load_from_file(self._storage_file)

        logger.info(f"✅ DocumentGenerationSystem initialized, loaded {len(self.documents)} documents")

    # ==========================================
    # РЕЖИМ 1: REAL-TIME (при обработке встречи)
    # ==========================================

    async def quick_classify(
        self,
        meeting_id: str,
        meeting_title: str,
        content: str
    ) -> Optional[DocumentCandidate]:
        """
        Быстрая классификация встречи при real-time обработке.

        Вызывается из knowledge_sync при обработке каждой встречи.
        Если встреча содержит потенциальный регламент — помечаем её.

        Args:
            meeting_id: ID встречи
            meeting_title: Название встречи
            content: Транскрипт или резюме

        Returns:
            DocumentCandidate если это потенциальный регламент, иначе None
        """
        # Классифицируем контент с трекингом
        async with UsageContext(agent_mode="documents", request_type="regulation_classify"):
            classification = await self.classifier.classify(content, meeting_title)

        if not classification.is_regulation_worthy:
            logger.debug(f"❌ Meeting '{meeting_title}' is NOT regulation-worthy")
            return None

        # Создаём кандидата
        candidate = DocumentCandidate(
            candidate_id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            meeting_title=meeting_title,
            classification=classification,
            created_at=datetime.utcnow(),
            processed=False
        )

        # Сохраняем
        self.candidates[candidate.candidate_id] = candidate

        logger.info(f"✅ Meeting '{meeting_title}' marked as regulation candidate: {classification.content_type.value}")

        # Помечаем в графе если доступен
        if self.graph:
            try:
                self.graph.update_node(
                    f"meeting_{meeting_id}",
                    {
                        "is_regulation_candidate": True,
                        "regulation_type": classification.content_type.value,
                        "regulation_topic": classification.topic,
                        "regulation_confidence": classification.confidence
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to update graph: {e}")

        return candidate

    # ==========================================
    # РЕЖИМ 2: BACKGROUND (ночная обработка)
    # ==========================================

    async def discover_and_generate(
        self,
        meetings: List[Dict[str, Any]],
        existing_documents: Optional[List[GeneratedDocument]] = None,
        min_meetings_for_document: int = 1,
        force_regenerate: bool = False,
        user_id: str = ""
    ) -> DocumentGenerationResult:
        """
        Полный цикл обнаружения и генерации документов.

        Вызывается ночным процессом для:
        1. Поиска кластеров встреч по темам
        2. Фильтрации тех, что подходят для регламентов
        3. Генерации новых документов
        4. Обновления существующих документов

        Args:
            meetings: Все встречи пользователя
            existing_documents: Уже существующие документы
            min_meetings_for_document: Минимум встреч для создания документа
            force_regenerate: Пересоздать все документы

        Returns:
            DocumentGenerationResult
        """
        result = DocumentGenerationResult()
        existing_documents = existing_documents or []

        logger.info(f"🔍 Starting document discovery for {len(meetings)} meetings...")

        # Все LLM вызовы в контексте документов
        async with UsageContext(agent_mode="documents", request_type="document_generation"):
            # Шаг 1: Отфильтровать только те встречи, что подходят для регламентов
            worthy_meetings = await self.classifier.filter_regulation_worthy(meetings)
            result.candidates_found = len(worthy_meetings)

            if not worthy_meetings:
                logger.info("❌ No regulation-worthy meetings found")
                return result

            logger.info(f"✅ Found {len(worthy_meetings)} regulation-worthy meetings")

            # Шаг 2: Кластеризовать по темам
            clusters = await self.clusterer.cluster_meetings(
                worthy_meetings,
                min_cluster_size=min_meetings_for_document
            )
            result.clusters_created = len(clusters)

            logger.info(f"📊 Created {len(clusters)} topic clusters")

            # Шаг 3: Для каждого кластера — генерировать или обновлять документ
            for cluster in clusters:
                try:
                    topic_lower = cluster.topic.lower()

                    # Проверяем, есть ли уже документ по этой теме
                    existing_doc = None
                    for doc in existing_documents:
                        if doc.topic.lower() == topic_lower or self._topics_similar(doc.topic, cluster.topic):
                            existing_doc = doc
                            break

                    if existing_doc and not force_regenerate:
                        # Обновляем существующий документ
                        updated_doc = await self.update_document(existing_doc, cluster.meetings)
                        if updated_doc:
                            result.documents.append(updated_doc)
                            result.documents_updated += 1
                            logger.info(f"📝 Updated document: {updated_doc.title}")
                    else:
                        # Генерируем новый документ
                        new_doc = await self.generate_for_cluster(cluster, user_id=user_id)
                        if new_doc:
                            result.documents.append(new_doc)
                            result.documents_generated += 1
                            self.documents[new_doc.document_id] = new_doc
                            self._auto_save()
                            logger.info(f"📄 Generated new document: {new_doc.title}")

                except Exception as e:
                    error_msg = f"Failed to process cluster '{cluster.topic}': {e}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)

        logger.info(f"✅ Document generation complete: {result.documents_generated} new, {result.documents_updated} updated")

        return result

    async def generate_for_cluster(self, cluster: TopicCluster, user_id: str = "") -> Optional[GeneratedDocument]:
        """Сгенерировать документ для кластера встреч."""

        # Агрегируем контент
        aggregated = await self.aggregator.aggregate(
            cluster.meetings,
            cluster.topic,
            cluster.recommended_document_type
        )

        # Проверяем, достаточно ли данных
        if not aggregated.key_points and not aggregated.steps:
            logger.warning(f"Not enough data for document on topic: {cluster.topic}")
            return None

        # Генерируем документ
        document = await self.writer.generate_document(
            aggregated,
            document_type=cluster.recommended_document_type
        )

        # Привязываем к пользователю
        if document and user_id:
            document.user_id = user_id

        return document

    async def generate_for_topic(
        self,
        topic: str,
        meetings: List[Dict[str, Any]],
        document_type: str = "regulation",
        user_id: str = "",
        model_tier: str = "standard",
    ) -> Optional[GeneratedDocument]:
        """
        Сгенерировать документ по конкретной теме.

        Args:
            topic: Тема документа
            meetings: Встречи для использования
            document_type: Тип документа
            user_id: ID пользователя-владельца
            model_tier: "standard" | "premium" — реально влияет на выбор модели

        Returns:
            GeneratedDocument или None
        """
        from backend.core.llm.router import ModelTier
        tier = ModelTier.PREMIUM if model_tier == "premium" else ModelTier.STANDARD

        # Агрегируем контент
        aggregated = await self.aggregator.aggregate(
            meetings, topic, document_type, model_tier=tier)

        # Генерируем документ
        document = await self.writer.generate_document(
            aggregated, document_type, model_tier=tier)

        # Привязываем к пользователю
        if document and user_id:
            document.user_id = user_id

        # Сохраняем
        self.documents[document.document_id] = document
        self._auto_save()

        return document

    async def update_document(
        self,
        existing: GeneratedDocument,
        new_meetings: List[Dict[str, Any]]
    ) -> Optional[GeneratedDocument]:
        """
        Обновить существующий документ новыми данными.

        Args:
            existing: Существующий документ
            new_meetings: Новые встречи с дополнительной информацией

        Returns:
            Обновлённый GeneratedDocument
        """
        # Фильтруем только новые встречи (которых ещё нет в источниках)
        {
            m.get("id", m.get("meeting_id", ""))
            for m in new_meetings
        }
        truly_new = [
            m for m in new_meetings
            if m.get("id", m.get("meeting_id", "")) not in existing.source_meetings
        ]

        if not truly_new:
            logger.debug(f"No new meetings for document: {existing.title}")
            return None

        # Агрегируем новый контент
        await self.aggregator.aggregate(
            truly_new,
            existing.topic,
            existing.document_type
        )

        # Объединяем с существующим (упрощённо — перегенерируем)
        # В продакшене можно делать умное слияние
        all_meetings = truly_new  # + загрузить старые встречи

        aggregated = await self.aggregator.aggregate(
            all_meetings,
            existing.topic,
            existing.document_type
        )

        # Генерируем обновлённый документ
        updated = await self.writer.generate_document(aggregated, existing.document_type)

        # Сохраняем версионность
        version_parts = existing.version.split(".")
        new_minor = int(version_parts[1]) + 1 if len(version_parts) > 1 else 1
        updated.version = f"{version_parts[0]}.{new_minor}"
        updated.document_id = existing.document_id  # Сохраняем ID
        updated.created_at = existing.created_at
        updated.source_meetings = list(set(existing.source_meetings + updated.source_meetings))

        # Обновляем в хранилище
        self.documents[updated.document_id] = updated
        self._auto_save()

        return updated

    def _topics_similar(self, topic1: str, topic2: str) -> bool:
        """Проверить схожесть тем (простая эвристика)."""
        words1 = set(topic1.lower().split())
        words2 = set(topic2.lower().split())

        # Убираем стоп-слова
        stop_words = {"и", "в", "на", "для", "по", "с", "к", "о", "от", "из"}
        words1 -= stop_words
        words2 -= stop_words

        if not words1 or not words2:
            return False

        # Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return (intersection / union) > 0.5 if union > 0 else False

    # ==========================================
    # API для получения документов
    # ==========================================

    def _reload_if_changed(self):
        """Перечитать файл, если он изменён другим воркером (granian --workers N).

        Стор — per-process singleton; документ, созданный в воркере A, не виден
        воркеру B → «клик по документу из списка ничего не делает». Дёшево
        сверяем mtime и перечитываем только при изменении."""
        try:
            import os
            mtime = os.path.getmtime(self._storage_file)
        except OSError:
            return
        if mtime != getattr(self, "_storage_mtime", None):
            self.load_from_file(self._storage_file)
            self._storage_mtime = mtime

    def get_all_documents(self) -> List[GeneratedDocument]:
        """Получить все документы."""
        self._reload_if_changed()
        return list(self.documents.values())

    def get_document(self, document_id: str) -> Optional[GeneratedDocument]:
        """Получить документ по ID."""
        doc = self.documents.get(document_id)
        if doc is None:
            # cache-miss: возможно, документ создан другим воркером — перечитать
            self._reload_if_changed()
            doc = self.documents.get(document_id)
        return doc

    def get_documents_by_type(self, document_type: str) -> List[GeneratedDocument]:
        """Получить документы по типу."""
        self._reload_if_changed()
        return [d for d in self.documents.values() if d.document_type == document_type]

    def get_candidates(self, processed: Optional[bool] = None) -> List[DocumentCandidate]:
        """Получить кандидатов на документы."""
        candidates = list(self.candidates.values())
        if processed is not None:
            candidates = [c for c in candidates if c.processed == processed]
        return candidates

    def mark_candidates_processed(self, candidate_ids: List[str]) -> int:
        """Пометить кандидатов обработанными (после прогона discover).

        Раньше флаг processed НЕ ставился нигде → те же кандидаты
        обрабатывались при каждом запуске (лишние LLM-вызовы и дубли)."""
        n = 0
        for cid in candidate_ids:
            c = self.candidates.get(cid)
            if c and not c.processed:
                c.processed = True
                n += 1
        return n

    def export_document(self, document_id: str, format: str = "markdown") -> Optional[str]:
        """
        Экспортировать документ в нужном формате.

        Args:
            document_id: ID документа
            format: markdown, html, json

        Returns:
            Контент документа в нужном формате
        """
        doc = self.documents.get(document_id)
        if not doc:
            return None

        if format == "markdown":
            return doc.content_markdown
        elif format == "html":
            return doc.content_html
        elif format == "json":
            return json.dumps({
                "document_id": doc.document_id,
                "title": doc.title,
                "type": doc.document_type,
                "version": doc.version,
                "created_at": doc.created_at.isoformat(),
                "updated_at": doc.updated_at.isoformat(),
                "summary": doc.summary,
                "content": doc.content_markdown,
                "sections": doc.sections,
                "source_meetings": doc.source_meetings,
                "keywords": doc.keywords,
                "word_count": doc.word_count
            }, ensure_ascii=False, indent=2)

        return doc.content_markdown

    # ==========================================
    # Persistence (сохранение/загрузка)
    # ==========================================

    def _auto_save(self):
        """Автоматическое сохранение после изменений."""
        try:
            self.save_to_file(self._storage_file)
        except Exception as e:
            logger.error(f"Auto-save failed: {e}")

    def delete_document(self, document_id: str) -> bool:
        """Удалить документ."""
        if document_id in self.documents:
            del self.documents[document_id]
            self._auto_save()
            logger.info(f"🗑️ Deleted document: {document_id}")
            return True
        return False

    def save_to_file(self, filepath: str):
        """Сохранить все документы в файл."""
        data = {
            "documents": [
                {
                    "document_id": d.document_id,
                    "title": d.title,
                    "document_type": d.document_type,
                    "version": d.version,
                    "created_at": d.created_at.isoformat(),
                    "updated_at": d.updated_at.isoformat(),
                    "summary": d.summary,
                    "content_markdown": d.content_markdown,
                    "content_html": d.content_html,
                    "topic": d.topic,
                    "keywords": d.keywords,
                    "source_meetings": d.source_meetings,
                    "authors": d.authors,
                    "sections": d.sections,
                    "table_of_contents": d.table_of_contents,
                    "status": d.status,
                    "confidence": d.confidence,
                    "word_count": d.word_count,
                    "user_id": d.user_id  # Привязка к пользователю
                }
                for d in self.documents.values()
            ],
            "candidates": [
                {
                    "candidate_id": c.candidate_id,
                    "meeting_id": c.meeting_id,
                    "meeting_title": c.meeting_title,
                    "content_type": c.classification.content_type.value,
                    "topic": c.classification.topic,
                    "confidence": c.classification.confidence,
                    "created_at": c.created_at.isoformat(),
                    "processed": c.processed
                }
                for c in self.candidates.values()
            ]
        }

        # Merge-before-write: другой воркер мог создать документы, которых нет
        # в памяти этого процесса. Без слияния _auto_save затирал бы их на диске
        # (потеря данных). Подмешиваем в data чужие document_id перед записью.
        try:
            import os
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                known = {d["document_id"] for d in data["documents"]}
                for d in on_disk.get("documents", []):
                    if d.get("document_id") and d["document_id"] not in known:
                        data["documents"].append(d)
                known_c = {c["candidate_id"] for c in data["candidates"]}
                for c in on_disk.get("candidates", []):
                    if c.get("candidate_id") and c["candidate_id"] not in known_c:
                        data["candidates"].append(c)
        except Exception as e:
            logger.warning(f"merge-before-write skipped: {e}")

        # Атомарная запись: tmp + replace (не рвём файл при конкурентной записи)
        import os
        tmp = f"{filepath}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)
        try:
            self._storage_mtime = os.path.getmtime(filepath)
        except OSError:
            pass

        logger.info(f"💾 Saved {len(data['documents'])} documents to {filepath}")

    def load_from_file(self, filepath: str):
        """Загрузить документы (и кандидатов) из файла."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Перезагрузка: чистим и наполняем заново (иначе reload дублирует)
            self.documents.clear()

            # Загружаем документы
            for d in data.get("documents", []):
                doc = GeneratedDocument(
                    document_id=d["document_id"],
                    title=d["title"],
                    document_type=d["document_type"],
                    version=d["version"],
                    created_at=datetime.fromisoformat(d["created_at"]),
                    updated_at=datetime.fromisoformat(d["updated_at"]),
                    summary=d["summary"],
                    content_markdown=d["content_markdown"],
                    content_html=d["content_html"],
                    topic=d["topic"],
                    keywords=d.get("keywords", []),
                    source_meetings=d.get("source_meetings", []),
                    authors=d.get("authors", []),
                    sections=d.get("sections", []),
                    table_of_contents=d.get("table_of_contents", []),
                    status=d.get("status", "draft"),
                    confidence=d.get("confidence", 0.0),
                    word_count=d.get("word_count", 0),
                    user_id=d.get("user_id", "")  # Привязка к пользователю
                )
                self.documents[doc.document_id] = doc

            logger.info(f"📂 Loaded {len(self.documents)} documents from {filepath}")

        except FileNotFoundError:
            logger.debug(f"No existing documents file: {filepath}")
        except Exception as e:
            logger.error(f"Failed to load documents: {e}")

