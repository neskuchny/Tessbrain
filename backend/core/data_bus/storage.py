# -*- coding: utf-8 -*-
"""
Data Bus Storage — Хранение данных шины (Supabase + JSON fallback).

Таблицы в Supabase:
- data_bus_consumers — потребители
- data_bus_policies — политики доступа
- data_bus_channels — каналы доставки
- data_bus_audit — аудит-лог
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import (
    ConsumerType,
    DataBusAuditEntry,
    DataBusConsumer,
    SharingPolicy,
)

logger = logging.getLogger(__name__)


class DataBusStorage:
    """
    Хранение данных шины данных.

    Основное хранилище: Supabase (PostgreSQL через REST API).
    Fallback: JSON файлы (для разработки/тестирования).
    """

    STORAGE_DIR = Path("data/data_bus")

    def __init__(self):
        """Инициализация."""
        self._supabase = None
        self._use_supabase: Optional[bool] = None

        # In-memory кеш для быстрого доступа
        self._consumers_cache: Dict[str, DataBusConsumer] = {}
        self._policies_cache: Dict[str, SharingPolicy] = {}
        self._key_hash_to_consumer: Dict[str, str] = {}  # hash → consumer_id

        # Аудит-лог (in-memory буфер)
        self._audit_buffer: List[DataBusAuditEntry] = []

    async def _get_supabase(self):
        """Получить Supabase клиент."""
        if self._use_supabase is None:
            try:
                from backend.db.supabase_client import SupabaseClient
                client = SupabaseClient()
                if client.url and client.key:
                    self._supabase = client
                    self._use_supabase = True
                    logger.info("DataBusStorage: Using Supabase backend")
                else:
                    self._use_supabase = False
                    logger.info("DataBusStorage: Supabase not configured, using JSON fallback")
            except Exception as e:
                logger.warning(f"DataBusStorage: Supabase init failed ({e}), using JSON fallback")
                self._use_supabase = False

        return self._supabase if self._use_supabase else None

    # ══════════════════════════════════════════════
    # Consumers
    # ══════════════════════════════════════════════

    async def save_consumer(self, consumer: DataBusConsumer):
        """Сохранить потребителя."""
        supabase = await self._get_supabase()

        if supabase:
            await self._save_consumer_supabase(consumer)

        # Всегда сохраняем в JSON как backup
        await self._save_consumer_json(consumer)

        # Обновляем кеш
        self._consumers_cache[consumer.id] = consumer
        self._key_hash_to_consumer[consumer.api_key_hash] = consumer.id

    async def get_consumer(self, consumer_id: str) -> Optional[DataBusConsumer]:
        """Получить потребителя по ID."""
        # Сначала из кеша
        if consumer_id in self._consumers_cache:
            return self._consumers_cache[consumer_id]

        supabase = await self._get_supabase()
        if supabase:
            result = await self._get_consumer_supabase(consumer_id)
            if result:
                return result
        # Fallback на JSON
        return await self._get_consumer_json(consumer_id)

    async def find_consumer_by_key_hash(self, key_hash: str) -> Optional[DataBusConsumer]:
        """Найти потребителя по хешу API ключа."""
        # Кеш
        if key_hash in self._key_hash_to_consumer:
            consumer_id = self._key_hash_to_consumer[key_hash]
            return await self.get_consumer(consumer_id)

        supabase = await self._get_supabase()
        if supabase:
            result = await self._find_consumer_by_hash_supabase(key_hash)
            if result:
                return result
        # Fallback на JSON (всегда, если Supabase не нашёл)
        return await self._find_consumer_by_hash_json(key_hash)

    async def list_consumers(self, tenant_id: str) -> List[DataBusConsumer]:
        """Список потребителей тенанта."""
        supabase = await self._get_supabase()
        if supabase:
            result = await self._list_consumers_supabase(tenant_id)
            if result:
                return result
        # Fallback на JSON
        return await self._list_consumers_json(tenant_id)

    async def update_consumer_last_access(self, consumer_id: str):
        """Обновить last_access_at."""
        now = datetime.now(timezone.utc).isoformat()

        if consumer_id in self._consumers_cache:
            self._consumers_cache[consumer_id].last_access_at = now

        supabase = await self._get_supabase()
        if supabase:
            try:
                await supabase._request(
                    "PATCH",
                    f"/rest/v1/data_bus_consumers?id=eq.{consumer_id}",
                    json={"last_access_at": now},
                )
            except Exception as e:
                logger.debug(f"Could not update last_access in Supabase: {e}")

    async def deactivate_consumer(self, consumer_id: str) -> bool:
        """Деактивировать потребителя."""
        if consumer_id in self._consumers_cache:
            self._consumers_cache[consumer_id].is_active = False

        supabase = await self._get_supabase()
        if supabase:
            try:
                await supabase._request(
                    "PATCH",
                    f"/rest/v1/data_bus_consumers?id=eq.{consumer_id}",
                    json={"is_active": False},
                )
                return True
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # JSON fallback
        return await self._deactivate_consumer_json(consumer_id)

    async def update_consumer_key(self, consumer_id: str, key_hash: str, key_prefix: str):
        """Обновить API ключ потребителя."""
        if consumer_id in self._consumers_cache:
            old_hash = self._consumers_cache[consumer_id].api_key_hash
            self._key_hash_to_consumer.pop(old_hash, None)

            self._consumers_cache[consumer_id].api_key_hash = key_hash
            self._consumers_cache[consumer_id].api_key_prefix = key_prefix
            self._key_hash_to_consumer[key_hash] = consumer_id

        supabase = await self._get_supabase()
        if supabase:
            try:
                await supabase._request(
                    "PATCH",
                    f"/rest/v1/data_bus_consumers?id=eq.{consumer_id}",
                    json={"api_key_hash": key_hash, "api_key_prefix": key_prefix},
                )
            except Exception as e:
                logger.error(f"Could not update key in Supabase: {e}")

    # ══════════════════════════════════════════════
    # Policies
    # ══════════════════════════════════════════════

    async def save_policy(self, policy: SharingPolicy):
        """Сохранить политику."""
        supabase = await self._get_supabase()
        if supabase:
            await self._save_policy_supabase(policy)

        # Всегда сохраняем в JSON как backup
        await self._save_policy_json(policy)

        self._policies_cache[policy.id] = policy

    async def get_policy(self, policy_id: str) -> Optional[SharingPolicy]:
        """Получить политику по ID."""
        if policy_id in self._policies_cache:
            return self._policies_cache[policy_id]

        supabase = await self._get_supabase()
        if supabase:
            result = await self._get_policy_supabase(policy_id)
            if result:
                return result
        # Fallback на JSON
        return await self._get_policy_json(policy_id)

    # ══════════════════════════════════════════════
    # Audit
    # ══════════════════════════════════════════════

    async def save_audit_entry(self, entry: DataBusAuditEntry):
        """Сохранить запись аудита."""
        self._audit_buffer.append(entry)

        supabase = await self._get_supabase()
        if supabase:
            await self._save_audit_supabase(entry)

        # Всегда сохраняем в JSON как backup
        await self._save_audit_json(entry)

    async def get_audit_entries(
        self,
        tenant_id: str,
        consumer_id: Optional[str] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[DataBusAuditEntry]:
        """Получить записи аудита."""
        supabase = await self._get_supabase()
        if supabase:
            result = await self._get_audit_supabase(tenant_id, consumer_id, limit, since)
            if result:
                return result

        # Fallback: из in-memory буфера
        entries = list(self._audit_buffer)
        if consumer_id:
            entries = [e for e in entries if e.consumer_id == consumer_id]
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    # ══════════════════════════════════════════════
    # Supabase implementations
    # ══════════════════════════════════════════════

    async def _save_consumer_supabase(self, consumer: DataBusConsumer):
        try:
            data = consumer.to_dict()
            data["consumer_type"] = consumer.consumer_type.value if isinstance(consumer.consumer_type, ConsumerType) else consumer.consumer_type
            await self._supabase._request("POST", "/rest/v1/data_bus_consumers", json=data)
        except Exception as e:
            logger.error(f"Supabase save consumer error: {e}")
            await self._save_consumer_json(consumer)

    async def _get_consumer_supabase(self, consumer_id: str) -> Optional[DataBusConsumer]:
        try:
            resp = await self._supabase._request(
                "GET", f"/rest/v1/data_bus_consumers?id=eq.{consumer_id}&limit=1"
            )
            if resp and isinstance(resp, list) and len(resp) > 0:
                c = DataBusConsumer.from_dict(resp[0])
                self._consumers_cache[c.id] = c
                self._key_hash_to_consumer[c.api_key_hash] = c.id
                return c
        except Exception as e:
            logger.error(f"Supabase get consumer error: {e}")
        return None

    async def _find_consumer_by_hash_supabase(self, key_hash: str) -> Optional[DataBusConsumer]:
        try:
            resp = await self._supabase._request(
                "GET", f"/rest/v1/data_bus_consumers?api_key_hash=eq.{key_hash}&is_active=eq.true&limit=1"
            )
            if resp and isinstance(resp, list) and len(resp) > 0:
                c = DataBusConsumer.from_dict(resp[0])
                self._consumers_cache[c.id] = c
                self._key_hash_to_consumer[c.api_key_hash] = c.id
                return c
        except Exception as e:
            logger.error(f"Supabase find consumer error: {e}")
        return None

    async def _list_consumers_supabase(self, tenant_id: str) -> List[DataBusConsumer]:
        try:
            resp = await self._supabase._request(
                "GET", f"/rest/v1/data_bus_consumers?tenant_id=eq.{tenant_id}&order=created_at.desc"
            )
            if resp and isinstance(resp, list):
                return [DataBusConsumer.from_dict(r) for r in resp]
        except Exception as e:
            logger.error(f"Supabase list consumers error: {e}")
        return []

    async def _save_policy_supabase(self, policy: SharingPolicy):
        try:
            data = policy.to_dict()
            await self._supabase._request("POST", "/rest/v1/data_bus_policies", json=data)
        except Exception as e:
            logger.error(f"Supabase save policy error: {e}")
            await self._save_policy_json(policy)

    async def _get_policy_supabase(self, policy_id: str) -> Optional[SharingPolicy]:
        try:
            resp = await self._supabase._request(
                "GET", f"/rest/v1/data_bus_policies?id=eq.{policy_id}&limit=1"
            )
            if resp and isinstance(resp, list) and len(resp) > 0:
                p = SharingPolicy.from_dict(resp[0])
                self._policies_cache[p.id] = p
                return p
        except Exception as e:
            logger.error(f"Supabase get policy error: {e}")
        return None

    async def _save_audit_supabase(self, entry: DataBusAuditEntry):
        try:
            await self._supabase._request(
                "POST", "/rest/v1/data_bus_audit", json=entry.to_dict()
            )
        except Exception as e:
            logger.debug(f"Supabase audit save error: {e}")

    async def _get_audit_supabase(
        self, tenant_id: str, consumer_id: Optional[str], limit: int, since: Optional[str]
    ) -> List[DataBusAuditEntry]:
        try:
            url = f"/rest/v1/data_bus_audit?order=timestamp.desc&limit={limit}"
            if consumer_id:
                url += f"&consumer_id=eq.{consumer_id}"
            if since:
                url += f"&timestamp=gte.{since}"
            resp = await self._supabase._request("GET", url)
            if resp and isinstance(resp, list):
                return [DataBusAuditEntry.from_dict(r) for r in resp]
        except Exception as e:
            logger.error(f"Supabase audit query error: {e}")
        return []

    # ══════════════════════════════════════════════
    # JSON fallback implementations
    # ══════════════════════════════════════════════

    def _ensure_storage_dir(self):
        """Создать директорию хранения."""
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    async def _save_consumer_json(self, consumer: DataBusConsumer):
        self._ensure_storage_dir()
        file_path = self.STORAGE_DIR / "consumers.json"
        data = self._load_json(file_path)

        consumers = data.get("consumers", [])
        # Обновляем или добавляем
        updated = False
        for i, c in enumerate(consumers):
            if c["id"] == consumer.id:
                consumers[i] = consumer.to_dict()
                updated = True
                break
        if not updated:
            consumers.append(consumer.to_dict())

        data["consumers"] = consumers
        self._save_json(file_path, data)

    async def _get_consumer_json(self, consumer_id: str) -> Optional[DataBusConsumer]:
        file_path = self.STORAGE_DIR / "consumers.json"
        data = self._load_json(file_path)

        for c_data in data.get("consumers", []):
            if c_data["id"] == consumer_id:
                c = DataBusConsumer.from_dict(c_data)
                self._consumers_cache[c.id] = c
                self._key_hash_to_consumer[c.api_key_hash] = c.id
                return c
        return None

    async def _find_consumer_by_hash_json(self, key_hash: str) -> Optional[DataBusConsumer]:
        file_path = self.STORAGE_DIR / "consumers.json"
        data = self._load_json(file_path)

        for c_data in data.get("consumers", []):
            if c_data.get("api_key_hash") == key_hash and c_data.get("is_active", True):
                c = DataBusConsumer.from_dict(c_data)
                self._consumers_cache[c.id] = c
                self._key_hash_to_consumer[c.api_key_hash] = c.id
                return c
        return None

    async def _list_consumers_json(self, tenant_id: str) -> List[DataBusConsumer]:
        file_path = self.STORAGE_DIR / "consumers.json"
        data = self._load_json(file_path)

        consumers = []
        for c_data in data.get("consumers", []):
            if c_data.get("tenant_id") == tenant_id:
                consumers.append(DataBusConsumer.from_dict(c_data))
        return consumers

    async def _deactivate_consumer_json(self, consumer_id: str) -> bool:
        file_path = self.STORAGE_DIR / "consumers.json"
        data = self._load_json(file_path)

        for c_data in data.get("consumers", []):
            if c_data["id"] == consumer_id:
                c_data["is_active"] = False
                self._save_json(file_path, data)
                return True
        return False

    async def _save_policy_json(self, policy: SharingPolicy):
        self._ensure_storage_dir()
        file_path = self.STORAGE_DIR / "policies.json"
        data = self._load_json(file_path)

        policies = data.get("policies", [])
        updated = False
        for i, p in enumerate(policies):
            if p["id"] == policy.id:
                policies[i] = policy.to_dict()
                updated = True
                break
        if not updated:
            policies.append(policy.to_dict())

        data["policies"] = policies
        self._save_json(file_path, data)

    async def _get_policy_json(self, policy_id: str) -> Optional[SharingPolicy]:
        file_path = self.STORAGE_DIR / "policies.json"
        data = self._load_json(file_path)

        for p_data in data.get("policies", []):
            if p_data["id"] == policy_id:
                p = SharingPolicy.from_dict(p_data)
                self._policies_cache[p.id] = p
                return p
        return None

    async def _save_audit_json(self, entry: DataBusAuditEntry):
        self._ensure_storage_dir()
        date_str = datetime.now().strftime("%Y%m%d")
        file_path = self.STORAGE_DIR / f"audit_{date_str}.json"
        data = self._load_json(file_path)

        entries = data.get("entries", [])
        entries.append(entry.to_dict())
        data["entries"] = entries

        self._save_json(file_path, data)

    # ──────────────────────── Helpers ────────────────────────

    def _load_json(self, path: Path) -> Dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_json(self, path: Path, data: Dict):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"JSON save error: {e}")
