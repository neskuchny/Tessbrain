# -*- coding: utf-8 -*-
"""PostgresTemporalTracker — Postgres-бэкенд версионирования (мульти-реплика).

Тот же публичный async-интерфейс, что у SQLite TemporalTracker
(create_version / get_version_at_date / get_version_history / list_entities /
add_timeline_event / get_entity_timeline / get_changes_in_range), но данные —
в общих таблицах Postgres (temporal_* из миграции 270) с RLS-изоляцией по
tenant_id. Нужен, когда бэкенд крутится в НЕСКОЛЬКО реплик: per-user SQLite-
файлы не шарятся между нодами.

Выбор бэкенда — в factory get_temporal_tracker (env TESSENT_TEMPORAL_BACKEND).
Дефолт продукта остаётся SQLite; Postgres-путь включают осознанно после
прогона миграции 270 + переноса данных (scripts/migrate_temporal_to_postgres).

tenant передаётся явно (get_temporal_tracker уже резолвит org∪user) и
проставляется в app.tenant_id для RLS на каждой сессии."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PostgresTemporalTracker:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.user_id = tenant_id  # совместимость с полем SQLite-трекера
        self._initialized = False

    async def initialize(self) -> bool:
        try:
            from backend.db.postgres import get_postgres
            client = await get_postgres()
            if not getattr(client, "_initialized", False):
                await client.initialize()
            self._initialized = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"PostgresTemporalTracker init failed: {e}")
            return False

    # --- сессия с проставленным tenant для RLS ---
    def _session(self):
        from backend.db.postgres import get_postgres

        tenant = self.tenant_id

        class _Ctx:
            async def __aenter__(self_):
                self_._client = await get_postgres()
                self_._cm = self_._client.session(apply_tenant=False)
                session = await self_._cm.__aenter__()
                from sqlalchemy import text
                # RLS: явный tenant этого трекера (org∪user)
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": tenant},
                )
                return session

            async def __aexit__(self_, *a):
                return await self_._cm.__aexit__(*a)

        return _Ctx()

    async def create_version(
        self,
        entity_id: str,
        entity_type: str,
        data: Dict[str, Any],
        changed_by: Optional[str] = None,
        change_type: str = "UPDATE",
        source: Optional[str] = None,
        occurred_at: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """occurred_at — когда факт стал верен (дата встречи). См. подробное
        обоснование в SQLite-варианте (`temporal_tracker.create_version`).
        Сигнатуры двух трекеров обязаны совпадать: вызывающий код не знает,
        какой из них подставлен, а исключение там глушится в debug-лог —
        расхождение стоило бы молчаливой потери всего версионирования."""
        from sqlalchemy import text
        from backend.core.temporal.temporal_tracker import _normalize_ts
        ts = _normalize_ts(occurred_at) or datetime.utcnow().isoformat()
        async with self._session() as s:
            # Предшественник — по хронологии, а не по номеру версии: при
            # догрузке архива старых встреч номер версии больше не совпадает
            # с порядком событий.
            row = (await s.execute(text(
                "SELECT version, data FROM temporal_versions "
                "WHERE tenant_id = :t AND entity_id = :e AND ts <= :ts "
                "ORDER BY ts DESC, version DESC LIMIT 1"),
                {"t": self.tenant_id, "e": entity_id, "ts": ts})).first()
            max_row = (await s.execute(text(
                "SELECT version FROM temporal_versions "
                "WHERE tenant_id = :t AND entity_id = :e "
                "ORDER BY version DESC LIMIT 1"),
                {"t": self.tenant_id, "e": entity_id})).first()
            old_data = {}
            # Номер версии — порядок записи (аудит), поэтому считается от
            # максимума, а не от хронологического предшественника.
            new_version = (int(max_row[0]) + 1) if max_row else 1
            if row:
                try:
                    old_data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                except Exception:
                    old_data = {}

            await s.execute(text(
                "INSERT INTO temporal_versions "
                "(tenant_id, entity_id, entity_type, version, data, ts, changed_by, change_type) "
                "VALUES (:t, :e, :et, :v, CAST(:d AS JSONB), :ts, :cb, :ct)"),
                {"t": self.tenant_id, "e": entity_id, "et": entity_type,
                 "v": new_version, "d": json.dumps(data, ensure_ascii=False),
                 "ts": ts, "cb": changed_by, "ct": change_type})

            # per-field change log
            for field in set(list(old_data.keys()) + list(data.keys())):
                ov, nv = old_data.get(field), data.get(field)
                if ov != nv:
                    await s.execute(text(
                        "INSERT INTO temporal_change_logs "
                        "(tenant_id, entity_id, version, field, old_value, new_value, ts, changed_by) "
                        "VALUES (:t, :e, :v, :f, CAST(:ov AS JSONB), CAST(:nv AS JSONB), :ts, :cb)"),
                        {"t": self.tenant_id, "e": entity_id, "v": new_version, "f": field,
                         "ov": json.dumps(ov, ensure_ascii=False),
                         "nv": json.dumps(nv, ensure_ascii=False),
                         "ts": ts, "cb": changed_by})
        return {"entity_id": entity_id, "entity_type": entity_type,
                "version": new_version, "data": data, "timestamp": ts,
                "changed_by": changed_by, "change_type": change_type}

    def _row_to_version(self, r) -> Dict[str, Any]:
        d = r._mapping
        data = d["data"]
        if not isinstance(data, dict):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return {"entity_id": d["entity_id"], "entity_type": d["entity_type"],
                "version": d["version"], "data": data, "timestamp": d["ts"],
                "changed_by": d["changed_by"], "change_type": d["change_type"]}

    async def get_current_version(self, entity_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text
        async with self._session() as s:
            r = (await s.execute(text(
                "SELECT * FROM temporal_versions WHERE tenant_id=:t AND entity_id=:e "
                "ORDER BY version DESC LIMIT 1"),
                {"t": self.tenant_id, "e": entity_id})).first()
            return self._row_to_version(r) if r else None

    async def get_version_history(self, entity_id: str,
                                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
        from sqlalchemy import text
        q = ("SELECT * FROM temporal_versions WHERE tenant_id=:t AND entity_id=:e "
             "ORDER BY version DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        async with self._session() as s:
            rows = (await s.execute(text(q), {"t": self.tenant_id, "e": entity_id})).fetchall()
            return [self._row_to_version(r) for r in rows]

    async def get_version_at_date(self, entity_id: str, date: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text
        from backend.core.temporal.temporal_tracker import _as_of_bound
        async with self._session() as s:
            # Сортировка по ts, а не по version — та же причина, что в
            # SQLite-варианте: номер версии = порядок записи, хронология = ts.
            r = (await s.execute(text(
                "SELECT * FROM temporal_versions WHERE tenant_id=:t AND entity_id=:e "
                "AND ts <= :d ORDER BY ts DESC, version DESC LIMIT 1"),
                {"t": self.tenant_id, "e": entity_id,
                 "d": _as_of_bound(date)})).first()
            return self._row_to_version(r) if r else None

    async def get_changes_in_range(self, entity_id: str, start_date: str,
                                   end_date: str) -> List[Dict[str, Any]]:
        from sqlalchemy import text
        async with self._session() as s:
            rows = (await s.execute(text(
                "SELECT field, old_value, new_value, ts, version FROM temporal_change_logs "
                "WHERE tenant_id=:t AND entity_id=:e AND ts >= :a AND ts <= :b "
                "ORDER BY ts"),
                {"t": self.tenant_id, "e": entity_id, "a": start_date, "b": end_date})).fetchall()
            out = []
            for r in rows:
                m = r._mapping
                out.append({"field": m["field"], "old_value": m["old_value"],
                            "new_value": m["new_value"], "timestamp": m["ts"],
                            "version": m["version"]})
            return out

    async def add_timeline_event(
        self, entity_id: str, entity_type: str, event_type: str,
        event_time: datetime, event_data: Optional[Dict[str, Any]] = None,
        meeting_id: Optional[str] = None, description: Optional[str] = None,
    ) -> Dict[str, Any]:
        from sqlalchemy import text
        et = event_time.isoformat() if isinstance(event_time, datetime) else str(event_time)
        async with self._session() as s:
            await s.execute(text(
                "INSERT INTO temporal_timeline_events "
                "(tenant_id, entity_id, entity_type, event_type, event_data, event_time, meeting_id, description) "
                "VALUES (:t, :e, :et, :ev, CAST(:d AS JSONB), :tm, :m, :desc)"),
                {"t": self.tenant_id, "e": entity_id, "et": entity_type, "ev": event_type,
                 "d": json.dumps(event_data or {}, ensure_ascii=False), "tm": et,
                 "m": meeting_id, "desc": description})
        return {"entity_id": entity_id, "event_type": event_type, "event_time": et}

    async def get_entity_timeline(self, entity_id: str, limit: int = 50,
                                  start_date: Optional[str] = None,
                                  end_date: Optional[str] = None) -> List[Dict[str, Any]]:
        from sqlalchemy import text
        q = ("SELECT * FROM temporal_timeline_events WHERE tenant_id=:t AND entity_id=:e ")
        p: Dict[str, Any] = {"t": self.tenant_id, "e": entity_id}
        if start_date:
            q += "AND event_time >= :a "; p["a"] = start_date
        if end_date:
            q += "AND event_time <= :b "; p["b"] = end_date
        q += f"ORDER BY event_time DESC LIMIT {int(limit)}"
        async with self._session() as s:
            rows = (await s.execute(text(q), p)).fetchall()
            out = []
            for r in rows:
                m = r._mapping
                out.append({"entity_id": m["entity_id"], "entity_type": m["entity_type"],
                            "event_type": m["event_type"], "event_data": m["event_data"],
                            "event_time": m["event_time"], "meeting_id": m["meeting_id"],
                            "description": m["description"]})
            return out

    async def list_entities(self, entity_type: Optional[str] = None,
                            limit: int = 200) -> List[Dict[str, Any]]:
        from sqlalchemy import text
        q = ("SELECT entity_id, entity_type, COUNT(*) AS versions, "
             "MIN(ts) AS first_seen, MAX(ts) AS last_seen, MAX(version) AS latest_version "
             "FROM temporal_versions WHERE tenant_id=:t ")
        p: Dict[str, Any] = {"t": self.tenant_id}
        if entity_type:
            q += "AND entity_type = :et "; p["et"] = entity_type
        q += ("GROUP BY entity_id, entity_type "
              "ORDER BY versions DESC, last_seen DESC LIMIT :lim")
        p["lim"] = int(limit)
        async with self._session() as s:
            rows = (await s.execute(text(q), p)).fetchall()
            out = []
            for r in rows:
                m = r._mapping
                # текущее имя — из последней версии
                name = m["entity_id"]
                nr = (await s.execute(text(
                    "SELECT data FROM temporal_versions WHERE tenant_id=:t AND entity_id=:e "
                    "ORDER BY version DESC LIMIT 1"),
                    {"t": self.tenant_id, "e": m["entity_id"]})).first()
                data_tenant = None
                if nr is not None:
                    d = nr[0] if isinstance(nr[0], dict) else json.loads(nr[0])
                    name = d.get("name") or d.get("title") or m["entity_id"]
                    # штамп тенанта из последней версии — для read-side
                    # анти-протечки на API-слое (см. sqlite-версию)
                    data_tenant = d.get("tenant_id")
                out.append({"entity_id": m["entity_id"], "entity_type": m["entity_type"],
                            "name": name, "versions": m["versions"],
                            "first_seen": m["first_seen"], "last_seen": m["last_seen"],
                            "latest_version": m["latest_version"],
                            "tenant_id": data_tenant})
            return out
