# -*- coding: utf-8 -*-
"""
Access Gateway - Точка входа для внешнего доступа.

Координирует аутентификацию, авторизацию, агрегацию данных,
фильтрацию и аудит для запросов от подрядчиков.
"""
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .audit_logger import get_audit_logger
from .data_aggregator import get_data_aggregator
from .security_filter import ContractorType, get_security_filter

logger = logging.getLogger(__name__)


@dataclass
class Contractor:
    """Информация о подрядчике."""
    id: str
    name: str
    type: ContractorType
    api_key_hash: str
    allowed_categories: Set[str] = field(default_factory=set)
    allowed_meeting_ids: List[str] = field(default_factory=list)
    # Аудит (S3-EXTERNAL): API-ключ привязан к конкретным владельцам данных.
    # Запрос с api_key + чужой user_id отклоняется. Пусто → совместимость со
    # старым конфигом (но владелец обязан выставить при регистрации новых).
    allowed_user_ids: List[str] = field(default_factory=list)
    max_requests_per_hour: int = 100
    active: bool = True
    created_at: str = ""
    expires_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["allowed_categories"] = list(self.allowed_categories)
        return d

    def is_authorized_for(self, user_id: Optional[str]) -> bool:
        """API-ключ может работать только с данными владельца(ев).

        Пустой список = legacy-ключ (до S3-фикса) — пропускаем (бэк-совмест),
        чтобы не сломать уже выданные ключи; новые ключи всегда привязаны."""
        if not self.allowed_user_ids:
            return True
        return bool(user_id) and user_id in self.allowed_user_ids


@dataclass
class ExternalAccessResponse:
    """Ответ на внешний запрос."""
    request_id: str
    contractor_id: str
    contractor_type: str
    data: Dict[str, Any]
    categories_accessed: List[str]
    items_returned: int
    items_filtered: int
    audit_log_id: str
    processing_time_ms: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AccessGateway:
    """
    Шлюз внешнего доступа.

    Функции:
    1. Аутентификация подрядчиков по API ключам
    2. Авторизация доступа к категориям данных
    3. Координация агрегации и фильтрации
    4. Rate limiting
    5. Аудит всех запросов
    """

    def __init__(
        self,
        contractors_config_path: str = "config/contractor_profiles.json",
        graph_builder=None,
        snapshot_generator=None
    ):
        """
        Args:
            contractors_config_path: Путь к конфигурации подрядчиков
            graph_builder: GraphBuilder для доступа к данным
            snapshot_generator: EnhancedSnapshotGenerator
        """
        self.config_path = Path(contractors_config_path)
        self.contractors: Dict[str, Contractor] = {}

        # Компоненты
        self.security_filter = get_security_filter()
        self.data_aggregator = get_data_aggregator(graph_builder, snapshot_generator)
        self.audit_logger = get_audit_logger()

        # Rate limiting
        self._request_counts: Dict[str, List[float]] = {}

        # Загружаем конфигурацию
        self._load_contractors()

    def _load_contractors(self):
        """Загрузить конфигурацию подрядчиков."""
        if not self.config_path.exists():
            # Создаём пример конфигурации
            self._create_default_config()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for contractor_data in data.get("contractors", []):
                contractor = Contractor(
                    id=contractor_data["id"],
                    name=contractor_data["name"],
                    type=ContractorType(contractor_data["type"]),
                    api_key_hash=contractor_data["api_key_hash"],
                    allowed_categories=set(contractor_data.get("allowed_categories", [])),
                    allowed_meeting_ids=contractor_data.get("allowed_meeting_ids", []),
                    allowed_user_ids=contractor_data.get("allowed_user_ids", []),
                    max_requests_per_hour=contractor_data.get("max_requests_per_hour", 100),
                    active=contractor_data.get("active", True),
                    created_at=contractor_data.get("created_at", ""),
                    expires_at=contractor_data.get("expires_at"),
                )
                self.contractors[contractor.id] = contractor

            logger.info(f"Loaded {len(self.contractors)} contractors")

        except Exception as e:
            logger.error(f"Error loading contractors config: {e}")

    def _create_default_config(self):
        """Создать конфигурацию по умолчанию."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        default_config = {
            "contractors": [
                {
                    "id": "demo_investor",
                    "name": "Demo Investor",
                    "type": "investor",
                    "api_key_hash": self._hash_api_key("demo_investor_key_123"),
                    "allowed_categories": [
                        "company_overview", "project_progress", "team_overview",
                        "risks_overview", "achievements", "metrics"
                    ],
                    "allowed_meeting_ids": [],
                    "max_requests_per_hour": 50,
                    "active": True,
                    "created_at": datetime.now(timezone.utc).isoformat()
                },
                {
                    "id": "demo_marketing",
                    "name": "Demo Marketing Agency",
                    "type": "marketing",
                    "api_key_hash": self._hash_api_key("demo_marketing_key_456"),
                    "allowed_categories": [
                        "company_overview", "product_description",
                        "team_overview", "achievements"
                    ],
                    "allowed_meeting_ids": [],
                    "max_requests_per_hour": 200,
                    "active": True,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
            ]
        }

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)

        # Загружаем созданную конфигурацию
        self._load_contractors()

    def _hash_api_key(self, api_key: str) -> str:
        """Хэшировать API ключ."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    async def authenticate(self, api_key: str) -> Optional[Contractor]:
        """
        Аутентификация подрядчика.

        Args:
            api_key: API ключ

        Returns:
            Contractor или None если не найден
        """
        key_hash = self._hash_api_key(api_key)

        for contractor in self.contractors.values():
            if contractor.api_key_hash == key_hash:
                if not contractor.active:
                    logger.warning(f"Contractor {contractor.id} is inactive")
                    return None

                if contractor.expires_at:
                    if contractor.expires_at < datetime.now(timezone.utc).isoformat():
                        logger.warning(f"Contractor {contractor.id} access expired")
                        return None

                return contractor

        return None

    def _check_rate_limit(self, contractor_id: str, max_per_hour: int) -> bool:
        """
        Проверка rate limit.

        Returns:
            True если лимит не превышен
        """
        now = time.time()
        hour_ago = now - 3600

        if contractor_id not in self._request_counts:
            self._request_counts[contractor_id] = []

        # Удаляем старые записи
        self._request_counts[contractor_id] = [
            t for t in self._request_counts[contractor_id]
            if t > hour_ago
        ]

        # Проверяем лимит
        if len(self._request_counts[contractor_id]) >= max_per_hour:
            return False

        # Добавляем текущий запрос
        self._request_counts[contractor_id].append(now)
        return True

    async def process_request(
        self,
        api_key: str,
        query: str,
        categories: Optional[List[str]] = None,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> ExternalAccessResponse:
        """
        Обработать запрос от подрядчика.

        Args:
            api_key: API ключ подрядчика
            query: Поисковый запрос
            categories: Запрошенные категории (опционально)
            user_id: ID пользователя-владельца данных
            ip_address: IP адрес клиента
            user_agent: User-Agent клиента

        Returns:
            ExternalAccessResponse
        """
        import uuid
        start_time = time.time()
        request_id = f"req_{uuid.uuid4().hex[:12]}"

        # 1. Аутентификация
        contractor = await self.authenticate(api_key)
        if not contractor:
            return ExternalAccessResponse(
                request_id=request_id,
                contractor_id="unknown",
                contractor_type="unknown",
                data={},
                categories_accessed=[],
                items_returned=0,
                items_filtered=0,
                audit_log_id="",
                processing_time_ms=int((time.time() - start_time) * 1000),
                error="Authentication failed"
            )

        # 2. Rate limiting
        if not self._check_rate_limit(contractor.id, contractor.max_requests_per_hour):
            return ExternalAccessResponse(
                request_id=request_id,
                contractor_id=contractor.id,
                contractor_type=contractor.type.value,
                data={},
                categories_accessed=[],
                items_returned=0,
                items_filtered=0,
                audit_log_id="",
                processing_time_ms=int((time.time() - start_time) * 1000),
                error="Rate limit exceeded"
            )

        # 3. Определение категорий
        requested_categories = set(categories) if categories else contractor.allowed_categories
        actual_categories = requested_categories & contractor.allowed_categories

        if not actual_categories:
            actual_categories = contractor.allowed_categories

        # 4. Агрегация данных
        aggregated = await self.data_aggregator.aggregate(
            query=query,
            categories=actual_categories,
            meeting_ids=contractor.allowed_meeting_ids or None,
            user_id=user_id
        )

        # 5. Фильтрация конфиденциальных данных
        filtered_data = self.security_filter.filter_data(
            data=aggregated.data,
            contractor_type=contractor.type
        )

        # Статистика фильтрации
        filter_stats = self.security_filter.get_filter_stats(
            aggregated.data, filtered_data
        )

        # 6. Аудит
        response_json = json.dumps(filtered_data, ensure_ascii=False)
        audit_entry = await self.audit_logger.log(
            contractor_id=contractor.id,
            contractor_type=contractor.type.value,
            query=query,
            categories_requested=list(requested_categories),
            categories_accessed=list(actual_categories),
            data_items_returned=filter_stats["filtered_items"],
            data_items_filtered=filter_stats["removed_items"],
            response_size_bytes=len(response_json.encode("utf-8")),
            ip_address=ip_address,
            user_agent=user_agent,
            duration_ms=int((time.time() - start_time) * 1000)
        )

        # 7. Формирование ответа
        return ExternalAccessResponse(
            request_id=request_id,
            contractor_id=contractor.id,
            contractor_type=contractor.type.value,
            data=filtered_data,
            categories_accessed=list(actual_categories),
            items_returned=filter_stats["filtered_items"],
            items_filtered=filter_stats["removed_items"],
            audit_log_id=audit_entry.id,
            processing_time_ms=int((time.time() - start_time) * 1000)
        )

    # ------------------------------------------------------------------
    # Вопрос своими словами (а не выгрузка категорий)
    # ------------------------------------------------------------------

    @staticmethod
    def ask_enabled() -> bool:
        """Ответы мозга подрядчику — за явным флагом, по умолчанию ВЫКЛ.

        Выгрузка данных (process_request) и связный ответ — разные риски: во
        втором случае содержимое уходит ещё и в модель. Пока владелец это не
        включил, эндпоинт честно отвечает «выключено», а не тихо работает."""
        import os
        return os.getenv("ENABLE_EXTERNAL_ASK", "").strip().lower() in (
            "1", "on", "true", "yes")

    async def process_question(
        self,
        api_key: str,
        question: str,
        categories: Optional[List[str]] = None,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ExternalAccessResponse:
        """Ответить подрядчику своими словами — СТРОГО по его же данным.

        Никакого нового пути к данным здесь нет: сначала обычный
        process_request (аутентификация, лимит, разрешённые категории,
        маскирование, аудит), и только его результат попадает в модель. Что
        фильтр вырезал — модель не видит и увидеть не может. Ответ дополнительно
        прогоняется через ту же маскировку и пишется в аудит отдельной записью.
        """
        resp = await self.process_request(
            api_key=api_key, query=question, categories=categories,
            user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        if resp.error:
            return resp

        contractor = self.contractors.get(resp.contractor_id)
        ctype = contractor.type if contractor else ContractorType.CONSULTANT

        if not self.ask_enabled():
            resp.data = dict(resp.data or {})
            resp.data["answer"] = (
                "Ответы своими словами выключены владельцем данных. "
                "Данные по запросу отданы как есть — см. остальные поля.")
            resp.data["answer_status"] = "disabled"
            return resp

        payload = json.dumps(resp.data or {}, ensure_ascii=False, indent=2)
        prompt = (
            "Ты отвечаешь внешнему подрядчику компании. Ниже — ЕДИНСТВЕННЫЕ "
            "данные, которые ему разрешено видеть; всё остальное намеренно "
            "скрыто владельцем.\n\n"
            "ПРАВИЛА:\n"
            "1. Отвечай только по этим данным. Ничего не додумывай и не "
            "восстанавливай скрытое по косвенным признакам.\n"
            "2. Если ответа в данных нет — так и скажи: «в доступных мне "
            "данных этого нет». Это нормальный ответ.\n"
            "3. Пометки вида [СУММА СКРЫТА] — это намеренные скрытия. Не "
            "пытайся их раскрыть и не строй на них выводов.\n\n"
            f"ВОПРОС: {question}\n\nДАННЫЕ:\n{payload}")

        answer = ""
        try:
            from backend.core.llm.workload_policy import generate_for_workload
            answer = await generate_for_workload(user_id or "", "chat", prompt) or ""
        except Exception as e:
            logger.warning("external ask: модель недоступна: %s", e)

        if not answer.strip():
            resp.data = dict(resp.data or {})
            resp.data["answer_status"] = "unavailable"
            resp.data["answer"] = ("Не удалось составить ответ. Данные по "
                                   "запросу отданы как есть.")
            return resp

        # Пересказ прогоняем через ту же маскировку — модель видела только
        # отфильтрованное, но подстраховка дешёвая, а цена ошибки высокая.
        answer = self.security_filter.filter_text(answer, ctype)

        try:
            await self.audit_logger.log(
                contractor_id=resp.contractor_id,
                contractor_type=resp.contractor_type,
                query=f"[ask] {question}",
                categories_requested=list(categories or []),
                categories_accessed=list(resp.categories_accessed),
                data_items_returned=1,
                data_items_filtered=0,
                response_size_bytes=len(answer.encode("utf-8")),
                ip_address=ip_address,
                user_agent=user_agent,
                duration_ms=0,
            )
        except Exception:
            logger.warning("external ask: аудит ответа не записан", exc_info=True)

        resp.data = dict(resp.data or {})
        resp.data["answer"] = answer
        resp.data["answer_status"] = "ok"
        return resp

    def register_contractor(
        self,
        id: str,
        name: str,
        contractor_type: str,
        api_key: str,
        allowed_categories: List[str],
        allowed_meeting_ids: Optional[List[str]] = None,
        allowed_user_ids: Optional[List[str]] = None,
        max_requests_per_hour: int = 100,
        expires_at: Optional[str] = None
    ) -> Contractor:
        """
        Зарегистрировать нового подрядчика.

        allowed_user_ids привязывает API-ключ к владельцу(ам) данных:
        запрос с api_key + чужой user_id отклоняется (S3-EXTERNAL).
        """
        contractor = Contractor(
            id=id,
            name=name,
            type=ContractorType(contractor_type),
            api_key_hash=self._hash_api_key(api_key),
            allowed_categories=set(allowed_categories),
            allowed_meeting_ids=allowed_meeting_ids or [],
            allowed_user_ids=allowed_user_ids or [],
            max_requests_per_hour=max_requests_per_hour,
            active=True,
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at
        )

        self.contractors[contractor.id] = contractor
        self._save_contractors()

        logger.info(f"Registered contractor: {contractor.id} ({contractor.type.value})")
        return contractor

    def deactivate_contractor(self, contractor_id: str) -> bool:
        """Деактивировать подрядчика."""
        if contractor_id in self.contractors:
            self.contractors[contractor_id].active = False
            self._save_contractors()
            return True
        return False

    def _save_contractors(self):
        """Сохранить конфигурацию подрядчиков."""
        try:
            data = {
                "contractors": [c.to_dict() for c in self.contractors.values()]
            }
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving contractors: {e}")

    def get_available_categories(self, contractor_type: str) -> List[str]:
        """Получить доступные категории для типа подрядчика."""
        try:
            ct = ContractorType(contractor_type)
            return list(self.security_filter.ALLOWED_CATEGORIES.get(ct, set()))
        except ValueError:
            return []

    def get_audit_stats(self, contractor_id: Optional[str] = None) -> Dict[str, Any]:
        """Получить статистику аудита."""
        return self.audit_logger.get_stats(contractor_id)


# Singleton
_access_gateway: Optional[AccessGateway] = None


def get_access_gateway(
    contractors_config_path: str = "config/contractor_profiles.json",
    graph_builder=None,
    snapshot_generator=None
) -> AccessGateway:
    """Получить экземпляр AccessGateway."""
    global _access_gateway
    if _access_gateway is None:
        _access_gateway = AccessGateway(
            contractors_config_path,
            graph_builder,
            snapshot_generator
        )
    return _access_gateway


