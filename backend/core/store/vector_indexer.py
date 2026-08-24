# -*- coding: utf-8 -*-
"""
Vector Indexer - индексация сущностей в Qdrant.

Создаёт эмбеддинги для:
- Встреч (summary + контекст)
- Решений (текст + обоснование)
- Задач (описание + контекст)
- Участников (профиль)
- Сущностей (описание)
- KPI (название + значения)

Поддерживает два режима:
1. Qdrant (персистентный) - для продакшена
2. In-memory (numpy) - для разработки без Docker
"""
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Проверяем доступность Qdrant
try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from qdrant_client.http.exceptions import UnexpectedResponse
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    logger.info("qdrant-client не установлен - используется in-memory fallback")

# Проверяем доступность sentence-transformers.
# ВАЖНО: except Exception, не только ImportError — на Windows torch может
# упасть на инициализации DLL (OSError WinError 1114: shm.dll) и раньше это
# убивало ВЕСЬ backend на старте, хотя эмбеддинги — деградируемая подсистема
# (поиск продолжает работать на BM25+графе). Лечение torch — см. warning ниже.
try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    SentenceTransformer = None  # аннотации в __init__ вычисляются в рантайме
    EMBEDDINGS_AVAILABLE = False
    logger.warning("sentence-transformers не установлен - pip install sentence-transformers")
except Exception as _e:  # OSError/RuntimeError из torch (DLL, CUDA, память)
    SentenceTransformer = None
    EMBEDDINGS_AVAILABLE = False
    logger.error(
        f"⚠️ sentence-transformers/torch сломан ({type(_e).__name__}: {_e}) — "
        "эмбеддинги ОТКЛЮЧЕНЫ, поиск работает на BM25+графе. Лечение (Windows): "
        "1) перезагрузка; 2) pip install --force-reinstall torch "
        "--index-url https://download.pytorch.org/whl/cpu ; "
        "3) VC++ Redistributable 2015-2022 x64.")

# Глобальный кеш для модели эмбеддингов (загружается один раз)
_EMBEDDING_MODEL_CACHE: Dict[str, "SentenceTransformer"] = {}


class VectorIndexer:
    """
    Индексатор сущностей в векторную базу данных.

    Поддерживает Qdrant и in-memory режимы.

    Пример использования:
    ```python
    indexer = VectorIndexer(use_qdrant=False)  # in-memory для разработки
    await indexer.connect()

    # Индексировать результаты встречи
    stats = await indexer.index_meeting_results(
        meeting_id="meeting_123",
        results=orchestrator_results
    )

    # Поиск
    results = await indexer.search("какие решения по проекту X?")
    ```
    """

    # Коллекции для разных типов сущностей
    COLLECTIONS = {
        "meetings": "tessent_meetings",
        "decisions": "tessent_decisions",
        "tasks": "tessent_tasks",
        "participants": "tessent_participants",
        "entities": "tessent_entities",
        "kpis": "tessent_kpis",
        "chunks": "tessent_chunks",
        "documents": "tessent_documents",  # Документы из папок
        # ═══ Step 3: Новые коллекции ═══
        "snapshots": "tessent_snapshots",  # Снэпшоты сущностей
        "reports": "tessent_reports",      # Периодические отчёты
        # ═══ Step 4: Semantic chunking ═══
        "propositions": "tessent_propositions",  # Атомарные утверждения
        "topics": "tessent_topics",              # Темы встреч
    }

    # Размерность эмбеддингов (зависит от модели)
    EMBEDDING_DIM = 768  # intfloat/multilingual-e5-base (default)

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        use_qdrant: Optional[bool] = None,
        embedding_model: Optional[str] = None,
        storage_path: Optional[str] = None,
        namespace: Optional[str] = None,
    ):
        """
        Args:
            host: Хост Qdrant
            port: Порт Qdrant
            use_qdrant: Принудительно использовать Qdrant (None = авто)
            embedding_model: Название модели для эмбеддингов
            storage_path: Путь для сохранения in-memory индекса
        """
        self.host = host or os.environ.get("QDRANT_HOST", "localhost")
        self.port = port or int(os.environ.get("QDRANT_PORT", "6333"))
        # Прод-Qdrant требует API-ключ; пусто == без авторизации (dev).
        self.api_key = os.environ.get("QDRANT_API_KEY") or None
        # qdrant-client при api_key по умолчанию включает https=True → к
        # self-hosted HTTP-Qdrant это даёт SSL WRONG_VERSION_NUMBER. Держим
        # False, если QDRANT_HTTPS не выставлен явно (Qdrant Cloud).
        self.https = os.environ.get("QDRANT_HTTPS", "false").lower() == "true"
        # Таймаут клиента Qdrant (сек). Баланс между двумя крайностями:
        #  - слишком мало (5с) → busy-but-alive Qdrant под нагрузкой ресинка
        #    бросается зря;
        #  - слишком много (15с×3) → МЁРТВЫЙ Qdrant держит чат 45с до отката.
        # Живой Qdrant отвечает на ping <2с даже под нагрузкой, поэтому 6с +
        # 1 ретрай (worst-case ~13с) ловит транзиент, но быстро сдаётся, если
        # Qdrant действительно лёг. Настраивается через env.
        try:
            self.qdrant_timeout = float(os.environ.get("QDRANT_TIMEOUT", "6"))
        except (TypeError, ValueError):
            self.qdrant_timeout = 6.0

        # Определяем бэкенд.
        #
        # Раньше здесь стояло `env in (...) or QDRANT_AVAILABLE`, и это
        # означало, что USE_QDRANT=false не выключал ничего: пакет
        # qdrant-client стоит в requirements, значит QDRANT_AVAILABLE
        # всегда True, значит правая часть «или» всегда побеждала.
        # Переменная существовала и не работала — а на неё опирается
        # развёртывание без внешних сервисов.
        #
        # Теперь: явное значение переменной решает в обе стороны, и лишь
        # при незаданной переменной бэкенд выбирается по наличию пакета.
        if use_qdrant is None:
            env_use_qdrant = os.environ.get("USE_QDRANT", "").strip().lower()
            if env_use_qdrant in ("true", "1", "yes", "on"):
                self.use_qdrant = True
            elif env_use_qdrant in ("false", "0", "no", "off"):
                self.use_qdrant = False
            else:
                self.use_qdrant = QDRANT_AVAILABLE
        else:
            self.use_qdrant = use_qdrant

        # Модель для эмбеддингов (sentence-transformers)
        # intfloat/multilingual-e5-base — лучше для русского языка (768 dim)
        # sentence-transformers/all-MiniLM-L6-v2 — быстрее, но хуже для русского (384 dim)
        from backend.config import settings
        self.configured_embedding_dim = settings.vector_embedding_dimension
        self.embedding_model_name = embedding_model or os.environ.get(
            "LOCAL_EMBEDDING_MODEL",
            settings.vector_embedding_model
        )

        # Namespace (например user_id) для изоляции коллекций/файлов
        self.namespace = namespace

        # Хранилище (для in-memory режима)
        self.storage_path = storage_path or os.environ.get(
            "VECTOR_STORAGE_PATH",
            "data/vector_index.json"
        )

        # Клиенты
        self.qdrant: Optional[QdrantClient] = None
        self.embedding_model: Optional[SentenceTransformer] = None
        self.embedding_disabled: bool = False
        self.embedding_disabled_reason: Optional[str] = None
        self._embedding_disabled_logged: bool = False

        # In-memory индекс (fallback)
        self.memory_index: Dict[str, List[Dict[str, Any]]] = {}

        self.connected = False
        self.backend = "qdrant" if self.use_qdrant else "memory"

        # Статистика
        self.stats = {
            "vectors_created": 0,
            "vectors_updated": 0,
            "errors": 0
        }

        # Instance-level collections mapping (can be namespaced)
        self.collections = dict(self.COLLECTIONS)
        if self.namespace:
            # Изоляция на уровне имени коллекций в Qdrant/Memory
            safe_ns = self.namespace.replace("-", "_")
            self.collections = {k: f"{safe_ns}__{v}" for k, v in self.COLLECTIONS.items()}

    async def connect(self) -> bool:
        """Подключиться к хранилищу"""
        # Инициализируем модель эмбеддингов.
        # В офлайн/ограниченных окружениях модель может быть недоступна (HF DNS/403 и т.п.).
        # В этом случае НЕ валим весь пайплайн — просто отключаем векторные операции.
        await self._init_embedding_model()
        if self.embedding_disabled:
            # Qdrant требует размерность эмбеддингов для коллекций, поэтому безопаснее уйти в memory.
            self.use_qdrant = False

        if self.use_qdrant:
            return await self._connect_qdrant()
        else:
            return await self._connect_memory()

    async def _init_embedding_model(self) -> bool:
        """Инициализировать модель эмбеддингов (с глобальным кешем)"""
        global _EMBEDDING_MODEL_CACHE

        if not EMBEDDINGS_AVAILABLE:
            self.embedding_disabled = True
            self.embedding_disabled_reason = ("sentence-transformers недоступен "
                                              "(не установлен или torch сломан — см. лог старта)")
            if not self._embedding_disabled_logged:
                logger.warning(f"⚠️ VectorIndexer: эмбеддинги отключены ({self.embedding_disabled_reason})")
                self._embedding_disabled_logged = True
            return False

        try:
            # Используем глобальный кеш — модель загружается ОДИН раз
            if self.embedding_model_name in _EMBEDDING_MODEL_CACHE:
                self.embedding_model = _EMBEDDING_MODEL_CACHE[self.embedding_model_name]
                self.EMBEDDING_DIM = self.embedding_model.get_sentence_embedding_dimension()
                if self.configured_embedding_dim and self.configured_embedding_dim != self.EMBEDDING_DIM:
                    logger.warning(
                        "⚠️ VectorIndexer dimension mismatch: config vs model",
                        extra={
                            "configured_dim": self.configured_embedding_dim,
                            "model_dim": self.EMBEDDING_DIM,
                            "model_name": self.embedding_model_name,
                        }
                    )
                logger.info(f"✅ Модель эмбеддингов из кеша: {self.embedding_model_name} (dim={self.EMBEDDING_DIM})")
                return True

            logger.info(f"📦 Загрузка модели эмбеддингов: {self.embedding_model_name}...")
            self.embedding_model = SentenceTransformer(self.embedding_model_name)

            # Обновляем размерность
            self.EMBEDDING_DIM = self.embedding_model.get_sentence_embedding_dimension()
            if self.configured_embedding_dim and self.configured_embedding_dim != self.EMBEDDING_DIM:
                logger.warning(
                    "⚠️ VectorIndexer dimension mismatch: config vs model",
                    extra={
                        "configured_dim": self.configured_embedding_dim,
                        "model_dim": self.EMBEDDING_DIM,
                        "model_name": self.embedding_model_name,
                    }
                )

            # Сохраняем в глобальный кеш
            _EMBEDDING_MODEL_CACHE[self.embedding_model_name] = self.embedding_model

            logger.info(f"✅ Модель загружена: {self.embedding_model_name} (dim={self.EMBEDDING_DIM})")
            return True

        except Exception as e:
            self.embedding_model = None
            self.embedding_disabled = True
            self.embedding_disabled_reason = str(e)
            if not self._embedding_disabled_logged:
                logger.warning(f"⚠️ VectorIndexer: эмбеддинги отключены (не удалось загрузить модель): {e}")
                self._embedding_disabled_logged = True
            return False

    async def _connect_qdrant(self) -> bool:
        """Подключиться к Qdrant"""
        if not QDRANT_AVAILABLE:
            logger.warning("⚠️ Qdrant недоступен, переключаюсь на in-memory")
            self.use_qdrant = False
            return await self._connect_memory()

        # Ретрай пинга перед fallback: одиночный таймаут (Qdrant занят ресинком,
        # Docker/WSL2 моргнул) больше не сбрасывает чат на устаревший снапшот.
        # 2 попытки (1 ретрай) — мёртвый Qdrant отдаёт управление за ~13с, а не
        # за 45с, и чат не висит 4 минуты.
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self.qdrant = QdrantClient(
                    host=self.host,
                    port=self.port,
                    api_key=self.api_key,
                    https=self.https,
                    timeout=self.qdrant_timeout,
                )

                # Проверяем подключение
                self.qdrant.get_collections()

                # Создаём коллекции
                await self._ensure_collections()

                self.connected = True
                self.backend = "qdrant"
                logger.info(f"✅ VectorIndexer connected to Qdrant: {self.host}:{self.port}")

                return True

            except Exception as e:
                last_err = e
                if attempt < 1:
                    logger.warning(
                        f"⚠️ Qdrant ping не прошёл ({e}) — повтор 2/2 "
                        f"(timeout={self.qdrant_timeout}s)")
                    await asyncio.sleep(0.4)

        logger.warning(
            f"⚠️ Qdrant НЕДОСТУПЕН ({last_err}) — похоже, контейнер завис/лёг "
            f"(перезапусти: docker restart tessent_qdrant). Переключаюсь на "
            f"in-memory снапшот — ответы будут НЕПОЛНЫМИ, реальные данные в Qdrant.")
        self.use_qdrant = False
        return await self._connect_memory()

    async def _connect_memory(self) -> bool:
        """Инициализировать in-memory индекс"""
        try:
            # Загружаем существующий индекс
            storage_path = Path(self.storage_path)
            if storage_path.exists():
                self._load_index_from_file(storage_path)
                logger.info(f"✅ Loaded existing index from {storage_path}")
            else:
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                # Инициализируем пустые коллекции
                for collection_name in self.collections.values():
                    self.memory_index[collection_name] = []
                logger.info("✅ Created new in-memory index")

            self.connected = True
            self.backend = "memory"
            logger.info("✅ VectorIndexer connected (in-memory)")

            # Выводим статистику
            total_vectors = sum(len(v) for v in self.memory_index.values())
            logger.info(f"   📊 Total vectors: {total_vectors}")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize in-memory index: {e}")
            return False

    async def _ensure_collections(self):
        """Создать коллекции в Qdrant если не существуют"""
        if not self.qdrant:
            return

        existing = {c.name for c in self.qdrant.get_collections().collections}

        for collection_name in self.collections.values():
            if collection_name not in existing:
                self.qdrant.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=self.EMBEDDING_DIM,
                        distance=models.Distance.COSINE
                    )
                )
                logger.info(f"✅ Created collection: {collection_name}")

    async def close(self):
        """Закрыть подключение и сохранить индекс"""
        if not self.use_qdrant:
            self._save_index_to_file()
        elif self.qdrant:
            self.qdrant.close()

        self.connected = False
        logger.info("VectorIndexer closed")

    def _load_index_from_file(self, path: Path):
        """Загрузить индекс из файла"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.memory_index = data.get("collections", {})

            # Конвертируем векторы обратно в numpy
            for collection in self.memory_index.values():
                for item in collection:
                    if "vector" in item and isinstance(item["vector"], list):
                        item["vector"] = np.array(item["vector"])

        except Exception as e:
            logger.warning(f"⚠️ Could not load index: {e}")
            self.memory_index = {name: [] for name in self.COLLECTIONS.values()}

    def _save_index_to_file(self):
        """Сохранить индекс в файл"""
        try:
            path = Path(self.storage_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            # Конвертируем numpy в списки для JSON
            serializable_index = {}
            for collection_name, items in self.memory_index.items():
                serializable_index[collection_name] = []
                for item in items:
                    item_copy = item.copy()
                    if "vector" in item_copy and isinstance(item_copy["vector"], np.ndarray):
                        item_copy["vector"] = item_copy["vector"].tolist()
                    serializable_index[collection_name].append(item_copy)

            data = {
                "collections": serializable_index,
                "metadata": {
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "total_vectors": sum(len(v) for v in self.memory_index.values()),
                    "embedding_model": self.embedding_model_name
                }
            }

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info(f"✅ Index saved to {path}")

        except Exception as e:
            logger.error(f"❌ Failed to save index: {e}")

    def _e5_prefixes_enabled(self) -> bool:
        """Режим префиксов e5 («query: …» / «passage: …»).

        Модели intfloat/e5 ОБУЧЕНЫ с этими префиксами — без них recall
        заметно ниже. НО включение меняет пространство эмбеддингов:
        старые векторы (без префиксов) станут асимметричны запросам.
        Поэтому флаг по умолчанию ВЫКЛЮЧЕН; включать вместе с полной
        переиндексацией (sync force / reload), не на живом индексе.
        env: VECTOR_E5_PREFIXES=on"""
        import os as _os
        if _os.getenv("VECTOR_E5_PREFIXES", "").lower() not in (
                "on", "true", "1"):
            return False
        return "e5" in (self.embedding_model_name or "").lower()

    def _get_embedding(self, text: str, *,
                       kind: str = "passage") -> np.ndarray:
        """Получить эмбеддинг для текста.

        kind: "passage" (индексация) | "query" (поисковый запрос) —
        различие используется ТОЛЬКО в e5-режиме префиксов (см. выше)."""
        if self.embedding_disabled or not self.embedding_model:
            raise RuntimeError("Embedding model not initialized")

        # Ограничиваем длину текста
        max_length = 512
        if len(text) > max_length * 4:  # примерно 4 символа на токен
            text = text[:max_length * 4]

        if self._e5_prefixes_enabled():
            prefix = "query: " if kind == "query" else "passage: "
            text = prefix + text

        # Кэш эмбеддинга по ФИНАЛЬНОМУ тексту. Безопасно: encode() —
        # детерминированная чистая функция (тот же текст+модель → тот же
        # вектор), в отличие от answer-кэша это НЕ протухает. Устраняет
        # повторный encode одного подзапроса на ретраях reflection
        # (search_queries[:3] неизменны между попытками — до 3× экономии на
        # standard/deep). Bounded LRU, ленивая инициализация (работает и
        # для инстансов без __init__).
        cache = getattr(self, "_embed_cache", None)
        if cache is None:
            from collections import OrderedDict
            cache = self._embed_cache = OrderedDict()
        cached = cache.get(text)
        if cached is not None:
            cache.move_to_end(text)     # LRU touch
            return cached
        vec = self.embedding_model.encode(text, convert_to_numpy=True)
        cache[text] = vec
        if len(cache) > 1024:
            cache.popitem(last=False)   # выселяем самый старый
        return vec

    def _generate_id(self, prefix: str, content: str) -> str:
        """Генерировать уникальный ID на основе контента"""
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        return f"{prefix}_{content_hash}"

    async def index_meeting_results(
        self,
        meeting_id: str,
        results: Dict[str, Any],
        meeting_metadata: Optional[Dict[str, Any]] = None,
        access_group: str = "public"
    ) -> Dict[str, Any]:
        """
        Индексировать результаты обработки встречи.

        Args:
            meeting_id: ID встречи
            results: Результаты от CaptureOrchestrator
            meeting_metadata: Дополнительные метаданные
            access_group: Группа доступа (RBAC)

        Returns:
            Статистика индексации
        """
        if not self.connected:
            return {"error": "Not connected", "vectors_created": 0}

        self.stats = {"vectors_created": 0, "vectors_updated": 0, "errors": 0}

        try:
            # 1. Индексируем встречу
            await self._index_meeting(meeting_id, results, meeting_metadata, access_group)

            # 2. Индексируем решения
            await self._index_decisions(results.get("decisions", []), meeting_id, access_group)

            # 3. Индексируем задачи
            await self._index_tasks(results.get("tasks", []), meeting_id, access_group)

            # 4. Индексируем участников
            await self._index_participants(results.get("participants", []), meeting_id, access_group)

            # 5. Индексируем сущности
            await self._index_entities(results.get("entities", []), meeting_id, access_group)

            # 6. Индексируем KPI
            await self._index_kpis(results.get("kpis", []), meeting_id, access_group)

            # 7. Доменные события встречи в EventLog (MEMORY_DESIGN_PRINCIPLES §6 №4).
            # За флагом OFF → прежнее поведение. Best-effort: не роняем индексацию.
            try:
                from backend.core.config.feature_flags import get_feature_flags
                uid = getattr(self, "namespace", None)
                if uid and get_feature_flags().enable_event_log_ingestion:
                    from backend.core.memory.event_ingestion import ingest_meeting_events
                    ingest_meeting_events(results, meeting_id=meeting_id, user_id=uid,
                                          meeting_metadata=meeting_metadata)
            except Exception as e:
                logger.debug(f"event-log ingestion skipped (non-critical): {e}")

            # Сохраняем индекс
            if not self.use_qdrant:
                self._save_index_to_file()

            logger.info(
                f"✅ Meeting {meeting_id} indexed: "
                f"{self.stats['vectors_created']} vectors created"
            )

            return {
                "meeting_id": meeting_id,
                **self.stats
            }

        except Exception as e:
            logger.error(f"❌ Error indexing meeting: {e}")
            self.stats["errors"] += 1
            return {"error": str(e), **self.stats}

    async def _index_meeting(
        self,
        meeting_id: str,
        results: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
        access_group: str
    ):
        """Индексировать встречу"""
        summary = results.get("summary", {})

        # Формируем текст для эмбеддинга
        text_parts = [
            f"Встреча: {metadata.get('title', 'Без названия')}" if metadata else "Встреча",
            f"Дата: {metadata.get('date', '')}" if metadata else "",
            f"Участников: {summary.get('participants_count', 0)}",
            f"Решений: {summary.get('decisions_count', 0)}",
            f"Задач: {summary.get('tasks_count', 0)}",
        ]
        text = " | ".join(filter(None, text_parts))

        payload = {
            "meeting_id": meeting_id,
            "title": metadata.get("title", "") if metadata else "",
            "date": metadata.get("date", "") if metadata else "",
            "project_id": metadata.get("project_id", "") if metadata else "",
            "participants_count": summary.get("participants_count", 0),
            "decisions_count": summary.get("decisions_count", 0),
            "tasks_count": summary.get("tasks_count", 0),
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "type": "meeting",
            "access_group": access_group  # NEW
        }

        await self._upsert_vector(
            collection=self.collections["meetings"],
            id=meeting_id,
            text=text,
            payload=payload
        )

    async def _index_decisions(self, decisions: List[Dict[str, Any]], meeting_id: str, access_group: str):
        """Индексировать решения"""
        for d in decisions:
            decision_id = d.get("decision_id", "")
            if not decision_id:
                continue

            # Текст для эмбеддинга
            text_parts = [
                d.get("decision_summary", ""),
                d.get("details", ""),
                d.get("reasoning", ""),
                f"Категория: {d.get('decision_category', '')}",
            ]
            text = " ".join(filter(None, text_parts))

            payload = {
                "decision_id": decision_id,
                "meeting_id": meeting_id,
                "summary": d.get("decision_summary", ""),
                "category": d.get("decision_category", ""),
                "urgency": d.get("business_impact", {}).get("urgency", "medium"),
                "risk_level": d.get("business_impact", {}).get("risk_level", "low"),
                "responsible": d.get("responsible", {}).get("primary", ""),
                "confidence": d.get("confidence", 0.8),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "type": "decision",
                "access_group": access_group # NEW
            }

            await self._upsert_vector(
                collection=self.collections["decisions"],
                id=decision_id,
                text=text,
                payload=payload
            )

    async def _index_tasks(self, tasks: List[Dict[str, Any]], meeting_id: str, access_group: str):
        """Индексировать задачи"""
        for t in tasks:
            task_id = t.get("task_id", "")
            if not task_id:
                continue

            assignee = t.get("assignee", {})
            assignee_name = assignee.get("name", "") if isinstance(assignee, dict) else str(assignee)

            deadline = t.get("deadline", {})
            deadline_date = deadline.get("date", "") if isinstance(deadline, dict) else str(deadline)

            # Текст для эмбеддинга
            text_parts = [
                t.get("title", ""),
                t.get("description", ""),
                f"Исполнитель: {assignee_name}",
                f"Приоритет: {t.get('priority', 'medium')}",
                f"Срок: {deadline_date}",
            ]
            text = " ".join(filter(None, text_parts))

            payload = {
                "task_id": task_id,
                "meeting_id": meeting_id,
                "title": t.get("title", ""),
                "description": t.get("description", ""),
                "task_type": t.get("task_type", "action"),
                "priority": t.get("priority", "medium"),
                "status": t.get("status", "new"),
                "assignee": assignee_name,
                "deadline": deadline_date,
                "confidence": t.get("confidence", 0.8),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "type": "task",
                "access_group": access_group # NEW
            }

            await self._upsert_vector(
                collection=self.collections["tasks"],
                id=task_id,
                text=text,
                payload=payload
            )

    async def _index_participants(self, participants: List[Dict[str, Any]], meeting_id: str, access_group: str):
        """Индексировать участников"""
        for p in participants:
            name = p.get("name", "")
            if not name:
                continue

            participant_id = p.get("participant_id", self._generate_id("person", name))

            # Текст для эмбеддинга
            text_parts = [
                name,
                f"Роль: {p.get('role', '')}",
                f"Отдел: {p.get('department', '')}",
                f"Активность: {p.get('activity_level', 'medium')}",
            ]
            text = " ".join(filter(None, text_parts))

            payload = {
                "participant_id": participant_id,
                "name": name,
                "normalized_name": p.get("normalized_name", name),
                "role": p.get("role", ""),
                "department": p.get("department", ""),
                "activity_level": p.get("activity_level", "medium"),
                "sentiment": p.get("sentiment", "neutral"),
                "meeting_ids": [meeting_id],  # Список встреч
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "type": "participant",
                "access_group": access_group # NEW
            }

            await self._upsert_vector(
                collection=self.collections["participants"],
                id=participant_id,
                text=text,
                payload=payload
            )

    async def _index_entities(self, entities: List[Dict[str, Any]], meeting_id: str, access_group: str):
        """Индексировать сущности"""
        for e in entities:
            entity_id = e.get("entity_id", "")
            name = e.get("name", "")

            if not entity_id or not name:
                continue

            # Текст для эмбеддинга
            text_parts = [
                name,
                e.get("description", ""),
                f"Тип: {e.get('entity_type', '')}",
                f"Важность: {e.get('importance', 'medium')}",
            ]
            text = " ".join(filter(None, text_parts))

            payload = {
                "entity_id": entity_id,
                "name": name,
                "entity_type": e.get("entity_type", "idea"),
                "description": e.get("description", ""),
                "importance": e.get("importance", "medium"),
                "confidence": e.get("confidence", 0.8),
                "meeting_ids": [meeting_id],
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "type": "entity",
                "access_group": access_group # NEW
            }

            await self._upsert_vector(
                collection=self.collections["entities"],
                id=entity_id,
                text=text,
                payload=payload
            )

        # Кросс-источниковые упоминания (MEMORY_DESIGN_PRINCIPLES §6 №5).
        # За флагом OFF → индексация прежняя. Best-effort: не роняем индексацию.
        # namespace == user_id (см. конструктор / chat route). Один batch-save.
        try:
            from backend.core.config.feature_flags import get_feature_flags
            uid = getattr(self, "namespace", None)
            if uid and get_feature_flags().enable_cross_source_mentions:
                from backend.core.memory.mention_ingestion import ingest_entity_mentions
                ingest_entity_mentions(entities, meeting_id=meeting_id, user_id=uid,
                                       source_type="meeting")
        except Exception as e:
            logger.debug(f"cross-source mentions ingestion skipped (non-critical): {e}")

    async def _index_kpis(self, kpis: List[Dict[str, Any]], meeting_id: str, access_group: str):
        """Индексировать KPI"""
        for k in kpis:
            kpi_id = k.get("kpi_id", "")
            name = k.get("name", "")

            if not kpi_id or not name:
                continue

            value = k.get("value", {})
            current_value = value.get("current") if isinstance(value, dict) else value
            unit = value.get("unit", "") if isinstance(value, dict) else ""

            trend = k.get("trend", {})
            direction = trend.get("direction", "stable") if isinstance(trend, dict) else ""

            # Текст для эмбеддинга
            text_parts = [
                name,
                f"Значение: {current_value} {unit}",
                f"Тренд: {direction}",
                f"Категория: {k.get('category', '')}",
            ]
            text = " ".join(filter(None, text_parts))

            payload = {
                "kpi_id": kpi_id,
                "name": name,
                "category": k.get("category", "operational"),
                "current_value": current_value,
                "unit": unit,
                "trend_direction": direction,
                "confidence": k.get("confidence", 0.8),
                "meeting_id": meeting_id,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "type": "kpi",
                "access_group": access_group # NEW
            }

            await self._upsert_vector(
                collection=self.collections["kpis"],
                id=kpi_id,
                text=text,
                payload=payload
            )

    async def add_document(self, text: str, metadata: Dict[str, Any], access_group: str = "public"):
        """
        Добавить произвольный документ.

        Args:
            text: Содержимое документа
            metadata: Метаданные (source, filename, title)
            access_group: Группа доступа
        """
        doc_id = metadata.get("id") or self._generate_id("doc", text)

        payload = metadata.copy()
        payload["access_group"] = access_group
        payload["type"] = "document"
        payload["indexed_at"] = datetime.now(timezone.utc).isoformat()

        await self._upsert_vector(
            collection=self.collections["documents"],
            id=doc_id,
            text=text,
            payload=payload
        )

    async def upsert_vector_public(
        self,
        collection: str,
        vector_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Публичный метод для вставки/обновления одного вектора.
        Используется для индексации отдельных сущностей.

        Args:
            collection: Название коллекции
            vector_id: ID вектора
            text: Текст для эмбеддинга
            metadata: Метаданные
        """
        await self._upsert_vector(
            collection=collection,
            id=vector_id,
            text=text,
            payload=metadata or {}
        )

    def _string_to_uuid(self, string_id: str) -> str:
        """
        Конвертирует строковый ID в UUID для Qdrant.
        Qdrant принимает только unsigned integer или UUID.
        Используем UUID5 с namespace для детерминированной генерации.
        """
        # Если уже валидный UUID, возвращаем как есть
        try:
            uuid.UUID(string_id)
            return string_id
        except (ValueError, AttributeError):
            pass

        # Генерируем UUID5 из строки (детерминированный)
        namespace = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')  # UUID namespace DNS
        return str(uuid.uuid5(namespace, string_id))

    async def _upsert_vector(
        self,
        collection: str,
        id: str,
        text: str,
        payload: Dict[str, Any]
    ):
        """Добавить или обновить вектор (внутренний метод)"""
        try:
            if self.embedding_disabled or not self.embedding_model:
                # Мягко пропускаем: векторная часть недоступна в текущем окружении
                return
            # Генерируем эмбеддинг
            vector = self._get_embedding(text)

            # ВАЖНО: Сохраняем текст в payload для использования при поиске
            payload_with_text = payload.copy()
            payload_with_text["text"] = text
            # Сохраняем оригинальный ID в payload для обратного маппинга
            payload_with_text["original_id"] = id

            if self.use_qdrant:
                # Конвертируем строковый ID в UUID для Qdrant
                qdrant_id = self._string_to_uuid(id)
                self.qdrant.upsert(
                    collection_name=collection,
                    points=[
                        models.PointStruct(
                            id=qdrant_id,
                            vector=vector.tolist(),
                            payload=payload_with_text
                        )
                    ]
                )
            else:
                # In-memory
                if collection not in self.memory_index:
                    self.memory_index[collection] = []

                # Ищем существующий
                existing_idx = None
                for i, item in enumerate(self.memory_index[collection]):
                    if item.get("id") == id:
                        existing_idx = i
                        break

                item = {
                    "id": id,
                    "vector": vector,
                    "payload": payload_with_text
                }

                if existing_idx is not None:
                    self.memory_index[collection][existing_idx] = item
                    self.stats["vectors_updated"] += 1
                else:
                    self.memory_index[collection].append(item)
                    self.stats["vectors_created"] += 1

            if self.use_qdrant:
                self.stats["vectors_created"] += 1

        except Exception as e:
            # Не спамим логами, если эмбеддинги отключены
            if "Embedding model not initialized" in str(e):
                if not self._embedding_disabled_logged:
                    logger.warning("⚠️ VectorIndexer: попытка upsert при отключённых эмбеддингах (пропускаю)")
                    self._embedding_disabled_logged = True
                return
            logger.error(f"❌ Error upserting vector: {e}")
            self.stats["errors"] += 1

    async def search(
        self,
        query: str,
        collections: Optional[List[str]] = None,
        limit: int = 10,
        score_threshold: float = 0.5,
        filter_conditions: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Семантический поиск по индексу.

        Args:
            query: Текст запроса
            collections: Список коллекций для поиска (None = все)
            limit: Максимум результатов на коллекцию
            score_threshold: Минимальный порог схожести
            filter_conditions: Фильтры по payload

        Returns:
            Список результатов с score и payload
        """
        if not self.connected:
            return []

        if self.embedding_disabled or not self.embedding_model:
            # Поиск невозможен без эмбеддингов — честно возвращаем пусто
            return []

        # Генерируем эмбеддинг запроса
        query_vector = self._get_embedding(query, kind="query")

        # Определяем коллекции (используем self.collections с namespace, не COLLECTIONS)
        search_collections = collections or list(self.collections.values())

        # Добавляем коллекции документов (без namespace) — они создаются document_indexer
        document_collections = ["tessent_documents", "tessent_chunks"]
        for doc_coll in document_collections:
            if doc_coll not in search_collections:
                # Проверяем что коллекция существует в memory_index
                if not self.use_qdrant and doc_coll in self.memory_index:
                    search_collections.append(doc_coll)
                elif self.use_qdrant:
                    # Для Qdrant проверяем существование
                    try:
                        existing = {c.name for c in self.qdrant.get_collections().collections}
                        if doc_coll in existing:
                            search_collections.append(doc_coll)
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

        logger.debug(f"🔍 VectorIndexer.search: query='{query[:50]}...', collections={search_collections}, threshold={score_threshold}")

        results = []

        for collection in search_collections:
            if self.use_qdrant:
                collection_results = await self._search_qdrant(
                    collection, query_vector, limit, score_threshold, filter_conditions
                )
            else:
                collection_results = self._search_memory(
                    collection, query_vector, limit, score_threshold, filter_conditions
                )

            results.extend(collection_results)

        # Сортируем по score
        results.sort(key=lambda x: x["score"], reverse=True)

        return results[:limit]

    async def _search_qdrant(
        self,
        collection: str,
        query_vector: np.ndarray,
        limit: int,
        score_threshold: float,
        filter_conditions: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Поиск в Qdrant"""
        try:
            # Формируем фильтр: скаляры → must/MatchValue (как раньше);
            # списки → MatchAny, несколько списочных ключей — should-OR
            # (контракт чата «эти встречи ИЛИ эти документы»)
            qdrant_filter = None
            if filter_conditions:
                must, should = [], []
                for key, value in filter_conditions.items():
                    if isinstance(value, (list, tuple, set)):
                        should.append(models.FieldCondition(
                            key=key,
                            match=models.MatchAny(any=list(value))))
                    else:
                        must.append(models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value)))
                if len(should) == 1 and not must:
                    qdrant_filter = models.Filter(must=should)
                elif should or must:
                    qdrant_filter = models.Filter(
                        must=must or None, should=should or None)

            # Use 'query' method (new API) instead of deprecated 'search'
            results = self.qdrant.query_points(
                collection_name=collection,
                query=query_vector.tolist(),
                limit=limit,
                score_threshold=score_threshold,
                query_filter=qdrant_filter
            )

            return [
                {
                    "id": str(r.id),
                    "score": r.score,
                    "collection": collection,
                    **r.payload
                }
                for r in results.points
            ]

        except Exception as e:
            logger.error(f"❌ Qdrant search error: {e}")
            return []

    def _search_memory(
        self,
        collection: str,
        query_vector: np.ndarray,
        limit: int,
        score_threshold: float,
        filter_conditions: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Поиск в in-memory индексе"""
        if collection not in self.memory_index:
            logger.debug(f"⚠️ Collection '{collection}' not in memory_index")
            return []

        collection_size = len(self.memory_index[collection])
        logger.debug(f"🔍 Searching in-memory collection '{collection}': {collection_size} items")

        results = []

        for item in self.memory_index[collection]:
            # Применяем фильтры (список = membership, см. filter_match)
            if filter_conditions:
                from backend.core.search.filter_match import (
                    metadata_matches_filters)
                if not metadata_matches_filters(item.get("payload", {}),
                                                filter_conditions):
                    continue

            # Вычисляем косинусное сходство
            item_vector = item.get("vector")
            if item_vector is None:
                continue

            if isinstance(item_vector, list):
                item_vector = np.array(item_vector)

            score = float(np.dot(query_vector, item_vector) / (
                np.linalg.norm(query_vector) * np.linalg.norm(item_vector)
            ))

            if score >= score_threshold:
                results.append({
                    "id": item["id"],
                    "score": score,
                    "collection": collection,
                    **item.get("payload", {})
                })

        # Сортируем и ограничиваем
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        stats = self.stats.copy()
        stats["backend"] = self.backend
        stats["connected"] = self.connected

        if not self.use_qdrant:
            stats["collections"] = {
                name: len(items)
                for name, items in self.memory_index.items()
            }
            stats["total_vectors"] = sum(len(v) for v in self.memory_index.values())

        return stats

    def save_index(self):
        """Принудительно сохранить индекс"""
        if not self.use_qdrant:
            self._save_index_to_file()


# Singleton
_global_indexer: Optional[VectorIndexer] = None


async def get_vector_indexer() -> VectorIndexer:
    """Получить глобальный экземпляр VectorIndexer"""
    global _global_indexer
    if _global_indexer is None:
        _global_indexer = VectorIndexer()
        await _global_indexer.connect()
    return _global_indexer


def reset_global_indexer():
    """Сбросить глобальный indexer"""
    global _global_indexer
    _global_indexer = None

