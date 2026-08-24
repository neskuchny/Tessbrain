# -*- coding: utf-8 -*-
"""
Temporal Tracker - версионирование сущностей и audit trail.

Обеспечивает:
- Версионирование данных сущностей
- История изменений (change log)
- Audit trail для compliance
- Temporal queries (запросы на конкретную дату)
- Rollback capability

Адаптировано из tess_old/module_07_temporal_tracking/temporal_tracker.py
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _normalize_ts(value: Any) -> Optional[str]:
    """Дату события → ISO-строка UTC, сравнимая с колонкой timestamp.

    Принимает datetime (naive считаем UTC) и строку ISO. Мусор → None,
    чтобы вызывающий откатился на «сейчас», а не записал версию с датой,
    по которой её потом не найти.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        logger.debug("temporal: не разобрал occurred_at=%r, беру время записи", value)
        return None


def _as_of_bound(date: Any) -> str:
    """Граница отсечения для запроса «как было на дату».

    Чистая дата без времени («2026-03-15») в бизнес-смысле означает «на конец
    того дня»: спрашивая «как было 15 марта», человек имеет в виду итог дня,
    а не полночь до начала. Иначе всё, что решили в этот день, из ответа
    выпадает — и срез выглядит пустым без объяснения.
    """
    raw = str(date).strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw}T23:59:59.999999+00:00"
    return _normalize_ts(date) or raw


class TemporalTracker:
    """
    Temporal Tracking для версионирования данных.

    Возможности:
    - Версионирование данных
    - История изменений
    - Audit trail
    - Temporal queries
    - Rollback capability

    Пример использования:
    ```python
    tracker = TemporalTracker(db_path="data/temporal/user_123.db")
    await tracker.initialize()

    # Создание версии при изменении сущности
    version = await tracker.create_version(
        entity_id="person_ivan_petrov",
        entity_type="person",
        data={"name": "Иван Петров", "role": "CTO"},
        changed_by="system",
        change_type="UPDATE"
    )

    # Получение истории
    history = await tracker.get_version_history("person_ivan_petrov")

    # Получение версии на дату
    version_at_date = await tracker.get_version_at_date(
        entity_id="person_ivan_petrov",
        date="2025-06-10"
    )
    ```
    """

    def __init__(
        self,
        db_path: str = "data/temporal/temporal_tracking.db",
        user_id: Optional[str] = None
    ):
        """
        Args:
            db_path: Путь к SQLite базе данных
            user_id: ID пользователя для изоляции данных
        """
        self.db_path = db_path
        self.user_id = user_id
        self.conn: Optional[sqlite3.Connection] = None
        self._initialized = False

        # Создаём директорию если не существует
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> bool:
        """Инициализация базы данных"""
        try:
            # check_same_thread=False для использования в async контексте
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

            # Конкурентность (важно с тех пор, как temporal стал ОБЩИМ на уровне
            # org — несколько встреч компании могут индексироваться параллельно):
            #  - WAL: читатели не блокируют писателя и наоборот;
            #  - busy_timeout: писатель ЖДЁТ до 5с вместо "database is locked";
            #  - synchronous=NORMAL: безопасно с WAL, заметно быстрее на записи.
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except Exception as _pragma_err:  # noqa: BLE001
                logger.warning(f"TemporalTracker PRAGMA setup skipped: {_pragma_err}")

            # Создание таблиц
            self._create_tables()

            self._initialized = True
            logger.info(f"✅ TemporalTracker initialized: {self.db_path}")
            return True
        except Exception as e:
            logger.error(f"❌ TemporalTracker initialization failed: {e}")
            return False

    def _create_tables(self):
        """Создание таблиц для версионирования"""
        cursor = self.conn.cursor()

        # Таблица версий
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                version INTEGER NOT NULL,
                data TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                changed_by TEXT,
                change_type TEXT,
                user_id TEXT,
                UNIQUE(entity_id, version)
            )
        """)

        # Таблица изменений (детальный лог полей)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS change_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                timestamp TEXT NOT NULL,
                changed_by TEXT,
                user_id TEXT
            )
        """)

        # Таблица audit trail
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                user_id TEXT,
                timestamp TEXT NOT NULL,
                changes TEXT,
                metadata TEXT,
                source TEXT
            )
        """)

        # Таблица timeline events (для построения таймлайнов)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT,
                event_time TEXT NOT NULL,
                meeting_id TEXT,
                user_id TEXT,
                description TEXT
            )
        """)

        # Индексы для быстрого поиска
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_entity ON versions(entity_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_timestamp ON versions(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_type ON versions(entity_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_user ON versions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_change_logs_entity ON change_logs(entity_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_trail(entity_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_trail(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timeline_entity ON timeline_events(entity_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timeline_time ON timeline_events(event_time)")

        self.conn.commit()

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
        """
        Создание новой версии entity.

        Args:
            entity_id: ID entity
            entity_type: Тип entity (task, project, person, etc.)
            data: Данные entity
            changed_by: Кто изменил
            change_type: Тип изменения (CREATE, UPDATE, DELETE)
            source: Источник изменения (meeting_id, manual, etc.)
            occurred_at: КОГДА факт стал верен (дата встречи), а не когда мы
                о нём узнали. Без него запрос «как было на 15 марта» ломается
                на любой досинхронизации: пачка старых встреч, загруженная
                сегодня, получила бы сегодняшний штамп, и вся история
                схлопнулась бы в один день. None → время записи (прежнее
                поведение, обратная совместимость).

        Returns:
            Созданная версия
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        # Номер версии — порядок ЗАПИСИ (аудит: чем мы пополнялись и когда).
        # Хронология живёт в timestamp и может с ним расходиться при
        # досинхронизации старых встреч — это нормально и намеренно.
        cursor.execute(
            "SELECT MAX(version) as max_version FROM versions WHERE entity_id = ?",
            (entity_id,)
        )
        row = cursor.fetchone()
        current_version = row['max_version'] if row['max_version'] else 0
        new_version = current_version + 1

        # Создаём новую версию. timestamp = когда факт стал верен (дата
        # встречи), если её передали; иначе — время записи.
        timestamp = _normalize_ts(occurred_at) or datetime.now(timezone.utc).isoformat()

        # Предыдущие данные для диффа берём по ХРОНОЛОГИИ, а не по номеру
        # версии: при загрузке старой встречи задним числом сравнивать надо
        # с тем, что было верно ДО неё, иначе change_log покажет
        # несуществующий «откат назад».
        old_data = {}
        if current_version > 0:
            cursor.execute(
                """
                SELECT data FROM versions
                WHERE entity_id = ? AND timestamp <= ?
                ORDER BY timestamp DESC, version DESC
                LIMIT 1
                """,
                (entity_id, timestamp),
            )
            row = cursor.fetchone()
            if row:
                old_data = json.loads(row['data'])

        cursor.execute("""
            INSERT INTO versions (entity_id, entity_type, version, data, timestamp, changed_by, change_type, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (entity_id, entity_type, new_version, json.dumps(data, ensure_ascii=False), timestamp, changed_by, change_type, self.user_id))

        # Логируем изменения полей
        changes = []
        for field, new_value in data.items():
            old_value = old_data.get(field)
            if old_value != new_value:
                cursor.execute("""
                    INSERT INTO change_logs (entity_id, version, field, old_value, new_value, timestamp, changed_by, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (entity_id, new_version, field, json.dumps(old_value, ensure_ascii=False), json.dumps(new_value, ensure_ascii=False), timestamp, changed_by, self.user_id))

                changes.append({
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value
                })

        # Audit trail
        cursor.execute("""
            INSERT INTO audit_trail (action, entity_type, entity_id, user_id, timestamp, changes, metadata, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (change_type, entity_type, entity_id, self.user_id, timestamp, json.dumps(changes, ensure_ascii=False), json.dumps({}), source))

        self.conn.commit()

        version = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "version": new_version,
            "data": data,
            "timestamp": timestamp,
            "changed_by": changed_by,
            "change_type": change_type,
            "changes": changes,
            "changes_count": len(changes)
        }

        logger.debug(f"📝 Created version: entity_id={entity_id}, version={new_version}, changes={len(changes)}")

        return version

    async def get_current_version(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Получить текущую (последнюю) версию entity"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT * FROM versions
            WHERE entity_id = ?
            ORDER BY version DESC
            LIMIT 1
        """, (entity_id,))

        row = cursor.fetchone()
        if not row:
            return None

        return {
            "entity_id": row['entity_id'],
            "entity_type": row['entity_type'],
            "version": row['version'],
            "data": json.loads(row['data']),
            "timestamp": row['timestamp'],
            "changed_by": row['changed_by'],
            "change_type": row['change_type']
        }

    async def get_version_history(
        self,
        entity_id: str,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Получить историю версий entity"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        query = "SELECT * FROM versions WHERE entity_id = ? ORDER BY version DESC"
        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, (entity_id,))

        history = []
        for row in cursor.fetchall():
            history.append({
                "entity_id": row['entity_id'],
                "entity_type": row['entity_type'],
                "version": row['version'],
                "data": json.loads(row['data']),
                "timestamp": row['timestamp'],
                "changed_by": row['changed_by'],
                "change_type": row['change_type']
            })

        return history

    async def list_entities(
        self,
        entity_type: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Список версионируемых сущностей с агрегатом по эволюции.

        Для панели «Эволюция объекта»: показать, у кого есть история изменений
        (versions>1 = объект реально трансформировался). Возвращает entity_id,
        entity_type, число версий, первую/последнюю дату и текущее имя (из data)."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()
        q = """
            SELECT entity_id, entity_type,
                   COUNT(*)      AS versions,
                   MIN(timestamp) AS first_seen,
                   MAX(timestamp) AS last_seen,
                   MAX(version)   AS latest_version
            FROM versions
        """
        params: tuple = ()
        if entity_type:
            q += " WHERE entity_type = ?"
            params = (entity_type,)
        q += " GROUP BY entity_id, entity_type ORDER BY versions DESC, last_seen DESC LIMIT ?"
        params = params + (int(limit),)
        cursor.execute(q, params)

        out: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            # текущее имя объекта — из последней версии (best-effort)
            name = row["entity_id"]
            data_tenant = None
            try:
                c2 = self.conn.cursor()
                c2.execute(
                    "SELECT data FROM versions WHERE entity_id = ? ORDER BY version DESC LIMIT 1",
                    (row["entity_id"],),
                )
                r2 = c2.fetchone()
                if r2:
                    d = json.loads(r2["data"])
                    name = d.get("name") or d.get("title") or row["entity_id"]
                    # штамп тенанта из последней версии — для анти-протечки
                    # на API-слое (чужие записи, попавшие в стор до фикса
                    # кросс-tenant резолва, отфильтровываются при чтении)
                    data_tenant = d.get("tenant_id")
            except Exception:
                pass
            out.append({
                "entity_id": row["entity_id"],
                "entity_type": row["entity_type"],
                "name": name,
                "versions": row["versions"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "latest_version": row["latest_version"],
                "tenant_id": data_tenant,
            })
        return out

    async def get_version_at_date(
        self,
        entity_id: str,
        date: str
    ) -> Optional[Dict[str, Any]]:
        """Получить версию entity на определённую дату"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        # Сортировка по timestamp, а не по version: номер версии — это
        # порядок записи, и после досинхронизации старых встреч он больше не
        # совпадает с хронологией. «Как было на дату» обязано отвечать по
        # хронологии.
        cursor.execute("""
            SELECT * FROM versions
            WHERE entity_id = ? AND timestamp <= ?
            ORDER BY timestamp DESC, version DESC
            LIMIT 1
        """, (entity_id, _as_of_bound(date)))

        row = cursor.fetchone()
        if not row:
            return None

        return {
            "entity_id": row['entity_id'],
            "entity_type": row['entity_type'],
            "version": row['version'],
            "data": json.loads(row['data']),
            "timestamp": row['timestamp'],
            "changed_by": row['changed_by'],
            "change_type": row['change_type']
        }

    async def get_changes_in_range(
        self,
        entity_id: str,
        start_date: str,
        end_date: str
    ) -> List[Dict[str, Any]]:
        """Получить изменения за период"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT * FROM change_logs
            WHERE entity_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
        """, (entity_id, start_date, end_date))

        changes = []
        for row in cursor.fetchall():
            changes.append({
                "entity_id": row['entity_id'],
                "version": row['version'],
                "field": row['field'],
                "old_value": json.loads(row['old_value']) if row['old_value'] else None,
                "new_value": json.loads(row['new_value']) if row['new_value'] else None,
                "timestamp": row['timestamp'],
                "changed_by": row['changed_by']
            })

        return changes

    async def get_audit_trail(
        self,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Получить audit trail"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        query = "SELECT * FROM audit_trail WHERE 1=1"
        params = []

        if entity_id:
            query += " AND entity_id = ?"
            params.append(entity_id)

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)

        query += f" ORDER BY timestamp DESC LIMIT {limit}"

        cursor.execute(query, params)

        audit = []
        for row in cursor.fetchall():
            audit.append({
                "id": row['id'],
                "action": row['action'],
                "entity_type": row['entity_type'],
                "entity_id": row['entity_id'],
                "user_id": row['user_id'],
                "timestamp": row['timestamp'],
                "changes": json.loads(row['changes']) if row['changes'] else [],
                "metadata": json.loads(row['metadata']) if row['metadata'] else {},
                "source": row['source']
            })

        return audit

    async def add_timeline_event(
        self,
        entity_id: str,
        entity_type: str,
        event_type: str,
        event_time: datetime,
        event_data: Optional[Dict[str, Any]] = None,
        meeting_id: Optional[str] = None,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Добавить событие в timeline сущности.

        Args:
            entity_id: ID сущности
            entity_type: Тип сущности
            event_type: Тип события (created, updated, mentioned, assigned, completed, etc.)
            event_time: Время события
            event_data: Дополнительные данные события
            meeting_id: ID встречи (если событие из встречи)
            description: Описание события

        Returns:
            Созданное событие
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO timeline_events (entity_id, entity_type, event_type, event_data, event_time, meeting_id, user_id, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entity_id,
            entity_type,
            event_type,
            json.dumps(event_data or {}, ensure_ascii=False),
            event_time.isoformat(),
            meeting_id,
            self.user_id,
            description
        ))

        event_id = cursor.lastrowid
        self.conn.commit()

        event = {
            "id": event_id,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "event_type": event_type,
            "event_data": event_data or {},
            "event_time": event_time.isoformat(),
            "meeting_id": meeting_id,
            "description": description
        }

        logger.debug(f"📅 Timeline event added: {entity_id} - {event_type}")

        return event

    async def get_entity_timeline(
        self,
        entity_id: str,
        limit: int = 50,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить timeline сущности.

        Args:
            entity_id: ID сущности
            limit: Максимальное количество событий
            start_date: Начало периода (ISO format)
            end_date: Конец периода (ISO format)

        Returns:
            Список событий timeline
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        query = "SELECT * FROM timeline_events WHERE entity_id = ?"
        params = [entity_id]

        if start_date:
            query += " AND event_time >= ?"
            params.append(start_date)

        if end_date:
            query += " AND event_time <= ?"
            params.append(end_date)

        query += f" ORDER BY event_time DESC LIMIT {limit}"

        cursor.execute(query, params)

        events = []
        for row in cursor.fetchall():
            events.append({
                "id": row['id'],
                "entity_id": row['entity_id'],
                "entity_type": row['entity_type'],
                "event_type": row['event_type'],
                "event_data": json.loads(row['event_data']) if row['event_data'] else {},
                "event_time": row['event_time'],
                "meeting_id": row['meeting_id'],
                "description": row['description']
            })

        return events

    async def get_project_timeline(
        self,
        project_id: str,
        include_related: bool = True,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить timeline проекта (включая связанные сущности).

        Args:
            project_id: ID проекта
            include_related: Включать связанные сущности (задачи, решения)
            limit: Максимальное количество событий

        Returns:
            Список событий timeline проекта
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        if include_related:
            # Ищем события проекта и связанных сущностей
            cursor.execute("""
                SELECT * FROM timeline_events
                WHERE entity_id = ?
                   OR entity_id LIKE ?
                   OR entity_id LIKE ?
                ORDER BY event_time DESC
                LIMIT ?
            """, (project_id, f"task_{project_id}%", f"decision_{project_id}%", limit))
        else:
            cursor.execute("""
                SELECT * FROM timeline_events
                WHERE entity_id = ?
                ORDER BY event_time DESC
                LIMIT ?
            """, (project_id, limit))

        events = []
        for row in cursor.fetchall():
            events.append({
                "id": row['id'],
                "entity_id": row['entity_id'],
                "entity_type": row['entity_type'],
                "event_type": row['event_type'],
                "event_data": json.loads(row['event_data']) if row['event_data'] else {},
                "event_time": row['event_time'],
                "meeting_id": row['meeting_id'],
                "description": row['description']
            })

        return events

    async def get_person_timeline(
        self,
        person_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить timeline человека (участия во встречах, задачи, решения).

        Args:
            person_id: ID человека
            limit: Максимальное количество событий

        Returns:
            Список событий timeline человека
        """
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT * FROM timeline_events
            WHERE entity_id = ?
            ORDER BY event_time DESC
            LIMIT ?
        """, (person_id, limit))

        events = []
        for row in cursor.fetchall():
            events.append({
                "id": row['id'],
                "entity_id": row['entity_id'],
                "entity_type": row['entity_type'],
                "event_type": row['event_type'],
                "event_data": json.loads(row['event_data']) if row['event_data'] else {},
                "event_time": row['event_time'],
                "meeting_id": row['meeting_id'],
                "description": row['description']
            })

        return events

    async def get_stats(self) -> Dict[str, Any]:
        """Статистика temporal tracking"""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as count FROM versions")
        total_versions = cursor.fetchone()['count']

        cursor.execute("SELECT COUNT(DISTINCT entity_id) as count FROM versions")
        total_entities = cursor.fetchone()['count']

        cursor.execute("SELECT COUNT(*) as count FROM change_logs")
        total_changes = cursor.fetchone()['count']

        cursor.execute("SELECT COUNT(*) as count FROM audit_trail")
        total_audit_records = cursor.fetchone()['count']

        cursor.execute("SELECT COUNT(*) as count FROM timeline_events")
        total_timeline_events = cursor.fetchone()['count']

        # Статистика по типам сущностей
        cursor.execute("""
            SELECT entity_type, COUNT(DISTINCT entity_id) as count
            FROM versions
            GROUP BY entity_type
        """)
        entities_by_type = {row['entity_type']: row['count'] for row in cursor.fetchall()}

        return {
            "total_versions": total_versions,
            "total_entities": total_entities,
            "total_changes": total_changes,
            "total_audit_records": total_audit_records,
            "total_timeline_events": total_timeline_events,
            "avg_versions_per_entity": round(total_versions / total_entities, 2) if total_entities > 0 else 0,
            "entities_by_type": entities_by_type,
            "db_path": self.db_path
        }

    async def close(self):
        """Закрытие соединения"""
        if self.conn:
            self.conn.close()
            self.conn = None
            self._initialized = False
            logger.info("TemporalTracker connection closed")


# Singleton instances per user
_trackers: Dict[str, TemporalTracker] = {}


def get_temporal_tracker(user_id: str) -> TemporalTracker:
    """
    Получить TemporalTracker для пользователя.

    Args:
        user_id: ID пользователя

    Returns:
        TemporalTracker instance
    """
    global _trackers

    # Tenant-scope: в организации temporal — ОБЩИЙ (по org_id), у solo — по
    # user_id. Согласовано с графом/вектором (везде tenant = get_org_for_user
    # or user_id), иначе эволюция не сливалась бы в общий мозг компании.
    # Solo (нет org): tenant == user_id → путь и данные БАЙТ-В-БАЙТ прежние.
    tenant = user_id
    try:
        from backend.core.ingest.membership import get_org_for_user
        tenant = get_org_for_user(user_id) or user_id
    except Exception:
        tenant = user_id

    if tenant not in _trackers:
        # Бэкенд хранилища: Postgres (мульти-реплика) ИЛИ SQLite (дефолт,
        # single-node). Переключается env TESSENT_TEMPORAL_BACKEND=postgres
        # ПОСЛЕ прогона миграции 270 + переноса данных. Дефолт — SQLite
        # (поведение байт-в-байт прежнее).
        if os.getenv("TESSENT_TEMPORAL_BACKEND", "sqlite").strip().lower() == "postgres":
            from backend.core.temporal.pg_temporal_tracker import (
                PostgresTemporalTracker,
            )
            _trackers[tenant] = PostgresTemporalTracker(tenant_id=tenant)
        else:
            db_path = f"data/temporal/{tenant}.db"
            _trackers[tenant] = TemporalTracker(db_path=db_path, user_id=tenant)

    return _trackers[tenant]

