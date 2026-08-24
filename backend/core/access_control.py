"""
TESSENT BRAIN - Access Control System
=====================================

Система управления доступами:
- RBAC (Role-Based Access Control)
- Разделение по организациям (multi-tenancy)
- Разделение по проектам
- Фильтрация данных по уровню доступа

Архитектура доступов:
┌─────────────────────────────────────────────────────────────┐
│                     Organization                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    Projects                          │   │
│  │  ┌───────────────────────────────────────────────┐  │   │
│  │  │              Entities (Graph)                  │  │   │
│  │  │  - Все сущности связаны в единый граф         │  │   │
│  │  │  - Доступ фильтруется по project_ids          │  │   │
│  │  │  - Связи между проектами видны по ролям       │  │   │
│  │  └───────────────────────────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘

Роли:
- admin: Полный доступ к организации
- manager: Доступ к своим проектам + чтение связей
- employee: Доступ только к своим проектам
- viewer: Только чтение
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class Role(str, Enum):
    """Роли пользователей"""
    ADMIN = "admin"          # Полный доступ
    MANAGER = "manager"      # Управление проектами
    EMPLOYEE = "employee"    # Работа в проектах
    VIEWER = "viewer"        # Только просмотр

    @property
    def level(self) -> int:
        """Уровень доступа (больше = больше прав)"""
        levels = {
            Role.VIEWER: 1,
            Role.EMPLOYEE: 2,
            Role.MANAGER: 3,
            Role.ADMIN: 4,
        }
        return levels.get(self, 0)


class Permission(str, Enum):
    """Разрешения на действия"""
    # Встречи
    MEETING_READ = "meeting:read"
    MEETING_CREATE = "meeting:create"
    MEETING_UPDATE = "meeting:update"
    MEETING_DELETE = "meeting:delete"

    # Сущности
    ENTITY_READ = "entity:read"
    ENTITY_CREATE = "entity:create"
    ENTITY_UPDATE = "entity:update"
    ENTITY_DELETE = "entity:delete"

    # Граф
    GRAPH_READ = "graph:read"
    GRAPH_TRAVERSE = "graph:traverse"
    GRAPH_CROSS_PROJECT = "graph:cross_project"  # Видеть связи между проектами

    # Снэпшоты
    SNAPSHOT_READ = "snapshot:read"
    SNAPSHOT_GENERATE = "snapshot:generate"

    # Аномалии
    ANOMALY_READ = "anomaly:read"
    ANOMALY_RESOLVE = "anomaly:resolve"

    # Администрирование
    USER_MANAGE = "user:manage"
    PROJECT_MANAGE = "project:manage"
    ORG_MANAGE = "org:manage"


# Маппинг ролей на разрешения
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.VIEWER: {
        Permission.MEETING_READ,
        Permission.ENTITY_READ,
        Permission.GRAPH_READ,
        Permission.SNAPSHOT_READ,
        Permission.ANOMALY_READ,
    },
    Role.EMPLOYEE: {
        Permission.MEETING_READ,
        Permission.MEETING_CREATE,
        Permission.ENTITY_READ,
        Permission.ENTITY_CREATE,
        Permission.GRAPH_READ,
        Permission.GRAPH_TRAVERSE,
        Permission.SNAPSHOT_READ,
        Permission.ANOMALY_READ,
    },
    Role.MANAGER: {
        Permission.MEETING_READ,
        Permission.MEETING_CREATE,
        Permission.MEETING_UPDATE,
        Permission.ENTITY_READ,
        Permission.ENTITY_CREATE,
        Permission.ENTITY_UPDATE,
        Permission.GRAPH_READ,
        Permission.GRAPH_TRAVERSE,
        Permission.GRAPH_CROSS_PROJECT,
        Permission.SNAPSHOT_READ,
        Permission.SNAPSHOT_GENERATE,
        Permission.ANOMALY_READ,
        Permission.ANOMALY_RESOLVE,
        Permission.PROJECT_MANAGE,
    },
    Role.ADMIN: set(Permission),  # Все разрешения
}


@dataclass
class AccessContext:
    """
    Контекст доступа пользователя

    Содержит информацию о том, что может видеть и делать пользователь.
    Используется для фильтрации данных при запросах.
    """
    user_id: str
    organization_id: str
    role: Role

    # Проекты, к которым есть доступ
    project_ids: list[str] = field(default_factory=list)

    # Дополнительные разрешения (сверх роли)
    extra_permissions: set[Permission] = field(default_factory=set)

    # Supabase user_id (для синхронизации)
    supabase_user_id: str | None = None

    # P5 Mandatory access: команды (из общей Supabase) и clearance.
    # Пусто/None → MAC опирается только на роль.
    team_ids: set[str] = field(default_factory=set)
    clearance: int | None = None

    def mac_membership(self):
        """TeamMembership для mandatory-фильтра (P5). Ленивый импорт."""
        from backend.core.access.mandatory import (
            ROLE_CLEARANCE,
            TeamMembership,
        )

        base = ROLE_CLEARANCE.get(self.role.value, 2)
        clr = max(base, self.clearance) if self.clearance else base
        return TeamMembership(
            user_id=self.user_id,
            team_ids=frozenset(self.team_ids),
            clearance=max(1, min(clr, 5)),
            source="role+teams" if self.team_ids else "role",
        )

    def has_permission(self, permission: Permission) -> bool:
        """Проверить наличие разрешения"""
        role_perms = ROLE_PERMISSIONS.get(self.role, set())
        return permission in role_perms or permission in self.extra_permissions

    def can_access_project(self, project_id: str) -> bool:
        """Проверить доступ к проекту"""
        if self.role == Role.ADMIN:
            return True
        return project_id in self.project_ids

    def can_see_cross_project_links(self) -> bool:
        """Может ли видеть связи между проектами"""
        return self.has_permission(Permission.GRAPH_CROSS_PROJECT)

    def get_graph_filter(self) -> dict[str, Any]:
        """
        Получить фильтр для запросов к графу

        Returns:
            Cypher-совместимый фильтр
        """
        if self.role == Role.ADMIN:
            # Админ видит всё в организации
            return {
                "organization_id": self.organization_id,
            }

        # Остальные видят только свои проекты
        return {
            "organization_id": self.organization_id,
            "project_ids": self.project_ids,
        }

    def get_vector_filter(self) -> dict[str, Any]:
        """
        Получить фильтр для векторного поиска

        Returns:
            Qdrant-совместимый фильтр
        """
        if self.role == Role.ADMIN:
            return {
                "organization_id": self.organization_id,
            }

        return {
            "organization_id": self.organization_id,
            "project_id": {"$in": self.project_ids},
        }


class AccessControlService:
    """
    Сервис управления доступами

    Обеспечивает:
    - Создание контекста доступа
    - Проверку разрешений
    - Фильтрацию данных
    """

    def __init__(self, postgres_client=None):
        self.postgres = postgres_client

    async def get_access_context(
        self,
        user_id: str,
        organization_id: str | None = None,
    ) -> AccessContext:
        """
        Получить контекст доступа пользователя

        Args:
            user_id: ID пользователя
            organization_id: ID организации (опционально)
        """
        # TODO: Загрузить из PostgreSQL
        # Пока возвращаем дефолтный контекст

        logger.debug(
            "Creating access context",
            user_id=user_id,
            organization_id=organization_id,
        )

        ctx = AccessContext(
            user_id=user_id,
            organization_id=organization_id or "default",
            role=Role.EMPLOYEE,
            project_ids=[],
        )

        # P5: best-effort подтянуть команды из общей Supabase, если
        # MAC включён. Никогда не роняет get_access_context.
        try:
            from backend.core.access.mandatory import (
                load_team_membership,
                mode_from_env,
            )

            mode = mode_from_env()
            if mode.value != "off":
                m = await load_team_membership(
                    user_id, ctx.role.value, mode=mode
                )
                ctx.team_ids = set(m.team_ids)
                ctx.clearance = m.clearance
        except Exception as exc:
            logger.warning("MAC membership load skipped", error=str(exc))

        return ctx

    async def check_permission(
        self,
        context: AccessContext,
        permission: Permission,
        resource_id: str | None = None,
    ) -> bool:
        """
        Проверить разрешение

        Args:
            context: Контекст доступа
            permission: Требуемое разрешение
            resource_id: ID ресурса (для проверки владения)
        """
        if not context.has_permission(permission):
            logger.warning(
                "Permission denied",
                user_id=context.user_id,
                permission=permission,
            )
            return False

        return True

    def filter_entities(
        self,
        entities: list[dict[str, Any]],
        context: AccessContext,
    ) -> list[dict[str, Any]]:
        """
        Отфильтровать сущности по доступу

        Args:
            entities: Список сущностей
            context: Контекст доступа
        """
        if context.role == Role.ADMIN:
            return entities

        filtered = []
        for entity in entities:
            project_id = entity.get("project_id")

            # Если нет project_id - общая сущность, видна всем
            if not project_id:
                filtered.append(entity)
                continue

            # Проверяем доступ к проекту
            if context.can_access_project(project_id):
                filtered.append(entity)

        # P5: mandatory-слой (access_level/access_groups) поверх
        # project-фильтра. env-gated, default OFF → поведение не
        # меняется, пока MANDATORY_ACCESS_MODE не включён явно.
        try:
            from backend.core.access.mandatory import (
                filter_nodes,
                mode_from_env,
            )

            mode = mode_from_env()
            if mode.value != "off":
                filtered, hidden = filter_nodes(
                    filtered, context.mac_membership(), mode
                )
                if hidden:
                    logger.info(
                        "MAC filter applied",
                        user_id=context.user_id,
                        hidden=hidden,
                        mode=mode.value,
                    )
        except Exception as exc:
            logger.warning("MAC filter skipped (fail-safe)", error=str(exc))

        return filtered

    def filter_relationships(
        self,
        relationships: list[dict[str, Any]],
        context: AccessContext,
        all_nodes: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Отфильтровать связи по доступу

        Если пользователь не может видеть cross-project связи,
        скрываем связи между сущностями из разных проектов.
        """
        if context.role == Role.ADMIN:
            return relationships

        if context.can_see_cross_project_links():
            return relationships

        filtered = []
        for rel in relationships:
            start_node = all_nodes.get(rel.get("start"))
            end_node = all_nodes.get(rel.get("end"))

            if not start_node or not end_node:
                continue

            start_project = start_node.get("project_id")
            end_project = end_node.get("project_id")

            # Если оба узла из одного проекта - показываем
            if start_project == end_project:
                filtered.append(rel)
                continue

            # Если один из узлов без проекта (общий) - показываем
            if not start_project or not end_project:
                filtered.append(rel)

        return filtered

    def redact_sensitive_fields(
        self,
        entity: dict[str, Any],
        context: AccessContext,
    ) -> dict[str, Any]:
        """
        Скрыть чувствительные поля в зависимости от роли

        Например:
        - Зарплаты видны только admin/manager
        - Личные данные скрыты для viewer
        """
        if context.role == Role.ADMIN:
            return entity

        # Список чувствительных полей по ролям
        sensitive_fields = {
            Role.VIEWER: ["salary", "personal_email", "phone", "address"],
            Role.EMPLOYEE: ["salary"],
            Role.MANAGER: [],
        }

        fields_to_hide = sensitive_fields.get(context.role, [])

        result = entity.copy()
        for field in fields_to_hide:
            if field in result.get("properties", {}):
                result["properties"][field] = "[REDACTED]"

        return result


# Singleton
_access_control: AccessControlService | None = None


def get_access_control() -> AccessControlService:
    """Получить сервис контроля доступа"""
    global _access_control
    if _access_control is None:
        _access_control = AccessControlService()
    return _access_control



