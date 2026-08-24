"""
TESSENT BRAIN - SIMA Database Client
Асинхронный клиент для работы с таблицами SIMA (Система Интерактивного Мышления и Анализа)
через PostgreSQL + asyncpg
"""
import uuid
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import text

from backend.db.postgres import PostgresClient, get_postgres

logger = structlog.get_logger(__name__)


def _snake_to_camel(s: str) -> str:
    """Конвертация snake_case → camelCase."""
    parts = s.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _to_camel_dict(d: dict) -> dict:
    """Конвертировать все ключи словаря из snake_case в camelCase."""
    return {_snake_to_camel(k): v for k, v in d.items()}


def _blocks_to_camel(blocks: list[dict]) -> list[dict]:
    """Конвертировать список блоков в camelCase."""
    return [_to_camel_dict(b) for b in blocks]


def _connections_to_camel(connections: list[dict]) -> list[dict]:
    """Конвертировать список связей в camelCase."""
    return [_to_camel_dict(c) for c in connections]


class SimaDBClient:
    """Клиент для работы с SIMA таблицами в PostgreSQL"""

    def __init__(self, pg: PostgresClient):
        self.pg = pg

    # ==========================================
    # Projects
    # ==========================================

    async def list_projects(self, user_id: str) -> list[dict]:
        """Получить список проектов пользователя"""
        async with self.pg.session() as session:
            result = await session.execute(
                text("""
                    SELECT p.*,
                        (SELECT COUNT(*) FROM sima_blocks WHERE project_id = p.id) as blocks_count,
                        (SELECT COUNT(*) FROM sima_connections WHERE project_id = p.id) as connections_count
                    FROM sima_projects p
                    WHERE p.user_id = :user_id
                    ORDER BY p.updated_at DESC
                """),
                {"user_id": user_id}
            )
            return [_to_camel_dict(dict(row._mapping)) for row in result.fetchall()]

    async def get_project(self, project_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Получить проект с блоками, связями и другими данными"""
        async with self.pg.session() as session:
            # Проект
            if user_id:
                result = await session.execute(
                    text("SELECT * FROM sima_projects WHERE id = :id AND user_id = :user_id"),
                    {"id": project_id, "user_id": user_id}
                )
            else:
                result = await session.execute(
                    text("SELECT * FROM sima_projects WHERE id = :id"),
                    {"id": project_id}
                )
            project = result.fetchone()
            if not project:
                return None
            project_data = dict(project._mapping)

            # Блоки
            result = await session.execute(
                text("SELECT * FROM sima_blocks WHERE project_id = :pid ORDER BY created_at"),
                {"pid": project_id}
            )
            project_data["blocks"] = _blocks_to_camel([dict(r._mapping) for r in result.fetchall()])

            # Связи
            result = await session.execute(
                text("SELECT * FROM sima_connections WHERE project_id = :pid ORDER BY created_at"),
                {"pid": project_id}
            )
            project_data["connections"] = _connections_to_camel([dict(r._mapping) for r in result.fetchall()])

            # Решения
            result = await session.execute(
                text("SELECT * FROM sima_decisions WHERE project_id = :pid ORDER BY created_at"),
                {"pid": project_id}
            )
            project_data["decisions"] = [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

            # Конвертируем ключи самого проекта
            project_data = _to_camel_dict(project_data)

            return project_data

    async def _tenant_for(self, session, *, project_id: Optional[str] = None,
                          user_id: Optional[str] = None) -> Optional[str]:
        """tenant_id для INSERT в sima_projects/blocks/connections/messages —
        у этих четырёх таблиц колонка tenant_id NOT NULL (миграция 020b), а
        код исторически её не заполнял → INSERT падал NotNullViolation.

        Приоритет (convention миграции 220 «tenant_id == user_id» для
        tenant-less деплоя):
        1) tenant из контекста запроса (multi-tenant, JWT/заголовок);
        2) tenant родительского проекта (для дочерних таблиц);
        3) user_id (tenant-less: группа из одного пользователя).
        """
        from backend.core.observability.tenant_context import get_current_tenant
        tid = get_current_tenant()
        if tid:
            return str(tid)
        if project_id:
            row = (await session.execute(
                text("SELECT tenant_id FROM sima_projects WHERE id = :pid"),
                {"pid": project_id})).fetchone()
            if row and row[0]:
                return str(row[0])
        return str(user_id) if user_id else None

    async def create_project(self, user_id: str, data: dict) -> dict:
        """Создать проект"""
        project_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            tenant_id = await self._tenant_for(session, user_id=user_id)
            _kind = str(data.get("taskKind") or "product")
            if _kind not in ("product", "book", "idea"):
                _kind = "product"
            await session.execute(
                text("""
                    INSERT INTO sima_projects (id, user_id, tenant_id, name, description, goal, target_audience, mvp_description, ideal_product, task_kind)
                    VALUES (:id, :user_id, :tenant_id, :name, :description, :goal, :target_audience, :mvp_description, :ideal_product, :task_kind)
                """),
                {
                    "id": project_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "name": data.get("name", "Новый проект"),
                    "description": data.get("description"),
                    "goal": data.get("goal"),
                    "target_audience": data.get("targetAudience"),
                    "mvp_description": data.get("mvpDescription"),
                    "ideal_product": data.get("idealProduct"),
                    "task_kind": _kind,
                }
            )
        return await self.get_project(project_id, user_id)

    async def update_project(self, project_id: str, user_id: str, data: dict) -> Optional[dict]:
        """Обновить проект"""
        # Формируем SET часть запроса только для переданных полей
        field_map = {
            "name": "name",
            "description": "description",
            "goal": "goal",
            "targetAudience": "target_audience",
            "mvpDescription": "mvp_description",
            "idealProduct": "ideal_product",
            "narrativeProblem": "narrative_problem",
            "narrativeSolution": "narrative_solution",
            "narrativeHowItWorks": "narrative_how_it_works",
            "narrativeProductMap": "narrative_product_map",
            "narrativeDecisions": "narrative_decisions",
            "generatedTZ": "generated_tz",
        }

        set_parts = []
        params = {"id": project_id, "user_id": user_id}
        for js_key, db_col in field_map.items():
            if js_key in data:
                set_parts.append(f"{db_col} = :{db_col}")
                params[db_col] = data[js_key]

        if not set_parts:
            return await self.get_project(project_id, user_id)

        async with self.pg.session() as session:
            await session.execute(
                text(f"UPDATE sima_projects SET {', '.join(set_parts)} WHERE id = :id AND user_id = :user_id"),
                params
            )
        return await self.get_project(project_id, user_id)

    async def delete_project(self, project_id: str, user_id: str) -> bool:
        """Удалить проект (каскадно удаляет всё связанное)"""
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_projects WHERE id = :id AND user_id = :user_id"),
                {"id": project_id, "user_id": user_id}
            )
            return result.rowcount > 0

    # ==========================================
    # Blocks
    # ==========================================

    async def create_block(self, data: dict) -> dict:
        """Создать блок"""
        block_id = data.get("id", str(uuid.uuid4()))
        async with self.pg.session() as session:
            tenant_id = await self._tenant_for(session, project_id=data["projectId"])
            await session.execute(
                text("""
                    INSERT INTO sima_blocks (
                        id, project_id, tenant_id, parent_block_id, name, label, type, layer, color,
                        position_x, position_y, description, goal
                    ) VALUES (
                        :id, :project_id, :tenant_id, :parent_block_id, :name, :label, :type, :layer, :color,
                        :position_x, :position_y, :description, :goal
                    )
                """),
                {
                    "id": block_id,
                    "project_id": data["projectId"],
                    "tenant_id": tenant_id,
                    "parent_block_id": data.get("parentBlockId"),
                    "name": data.get("name", "Новый блок"),
                    "label": data.get("label", "?"),
                    "type": data.get("type", "feature"),
                    "layer": data.get("layer", "mvp"),
                    "color": data.get("color", "#6366F1"),
                    "position_x": data.get("positionX", 0),
                    "position_y": data.get("positionY", 0),
                    "description": data.get("description"),
                    "goal": data.get("goal"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_blocks WHERE id = :id"),
                {"id": block_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def update_block(self, block_id: str, data: dict) -> Optional[dict]:
        """Обновить блок — принимает любые поля"""
        field_map = {
            "name": "name", "label": "label", "type": "type", "layer": "layer",
            "color": "color", "positionX": "position_x", "positionY": "position_y",
            "parentBlockId": "parent_block_id",
            "status": "status", "isMvp": "is_mvp",
            # Зачем
            "businessValue": "business_value", "userBenefit": "user_benefit",
            "problemSolved": "problem_solved", "impactIfRemoved": "impact_if_removed",
            "metrics": "metrics",
            # Что
            "description": "description", "goal": "goal",
            "sourceTake": "source_take",
            "inputData": "input_data", "outputData": "output_data",
            "acceptanceCriteria": "acceptance_criteria", "userFlow": "user_flow",
            "edgeCases": "edge_cases",
            # Как
            "implementation": "implementation", "blockTechStack": "block_tech_stack",
            "apiEndpoints": "api_endpoints", "dataModel": "data_model",
            "filesToCreate": "files_to_create", "technicalDeps": "technical_deps",
            "estimatedComplexity": "estimated_complexity",
            # Контекст
            "analogues": "analogues", "alternatives": "alternatives",
            "risks": "risks", "futureEvolution": "future_evolution",
            "problems": "problems",
            # Нарратив
            "blockNarrative": "block_narrative",
        }

        set_parts = []
        params = {"id": block_id}
        for js_key, db_col in field_map.items():
            if js_key in data and data[js_key] is not None:
                set_parts.append(f"{db_col} = :{db_col}")
                params[db_col] = data[js_key]

        if not set_parts:
            return None

        async with self.pg.session() as session:
            await session.execute(
                text(f"UPDATE sima_blocks SET {', '.join(set_parts)} WHERE id = :id"),
                params
            )
            result = await session.execute(
                text("SELECT * FROM sima_blocks WHERE id = :id"),
                {"id": block_id}
            )
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    async def delete_block(self, block_id: str) -> bool:
        """Удалить блок"""
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_blocks WHERE id = :id"),
                {"id": block_id}
            )
            return result.rowcount > 0

    # ==========================================
    # Connections
    # ==========================================

    async def create_connection(self, data: dict) -> dict:
        """Создать связь"""
        conn_id = data.get("id", str(uuid.uuid4()))
        async with self.pg.session() as session:
            tenant_id = await self._tenant_for(session, project_id=data["projectId"])
            await session.execute(
                text("""
                    INSERT INTO sima_connections (
                        id, project_id, tenant_id, parent_block_id, from_block_id, to_block_id,
                        type, label, description, data_description, data_format,
                        color, style, animated, direction
                    ) VALUES (
                        :id, :project_id, :tenant_id, :parent_block_id, :from_block_id, :to_block_id,
                        :type, :label, :description, :data_description, :data_format,
                        :color, :style, :animated, :direction
                    )
                """),
                {
                    "id": conn_id,
                    "project_id": data["projectId"],
                    "tenant_id": tenant_id,
                    "parent_block_id": data.get("parentBlockId"),
                    "from_block_id": data["fromBlockId"],
                    "to_block_id": data["toBlockId"],
                    "type": data.get("type", "data_flow"),
                    "label": data.get("label"),
                    "description": data.get("description"),
                    "data_description": data.get("dataDescription"),
                    "data_format": data.get("dataFormat"),
                    "color": data.get("color", "#6366F1"),
                    "style": data.get("style", "solid"),
                    "animated": data.get("animated", False),
                    "direction": (data.get("direction")
                                  if data.get("direction") in ("one_way", "two_way", "to_task")
                                  else "one_way"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_connections WHERE id = :id"),
                {"id": conn_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def update_connection(self, conn_id: str, data: dict) -> Optional[dict]:
        """Обновить связь"""
        field_map = {
            "type": "type", "label": "label", "description": "description",
            "dataDescription": "data_description", "dataFormat": "data_format",
            "dataExample": "data_example", "dataVolume": "data_volume",
            "condition": "condition", "errorHandling": "error_handling",
            "businessMeaning": "business_meaning",
            "color": "color", "style": "style", "animated": "animated",
            "direction": "direction",
        }

        set_parts = []
        params = {"id": conn_id}
        for js_key, db_col in field_map.items():
            if js_key in data:
                set_parts.append(f"{db_col} = :{db_col}")
                params[db_col] = data[js_key]

        if not set_parts:
            return None

        async with self.pg.session() as session:
            await session.execute(
                text(f"UPDATE sima_connections SET {', '.join(set_parts)} WHERE id = :id"),
                params
            )
            result = await session.execute(
                text("SELECT * FROM sima_connections WHERE id = :id"),
                {"id": conn_id}
            )
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    async def delete_connection(self, conn_id: str) -> bool:
        """Удалить связь"""
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_connections WHERE id = :id"),
                {"id": conn_id}
            )
            return result.rowcount > 0

    # ==========================================
    # Messages
    # ==========================================

    async def get_messages(self, project_id: str, session_id: Optional[str] = None) -> list[dict]:
        """Получить сообщения проекта/сессии"""
        if session_id:
            query = "SELECT * FROM sima_messages WHERE project_id = :pid AND session_id = :sid ORDER BY created_at"
            params = {"pid": project_id, "sid": session_id}
        else:
            query = "SELECT * FROM sima_messages WHERE project_id = :pid ORDER BY created_at"
            params = {"pid": project_id}

        async with self.pg.session() as session:
            result = await session.execute(text(query), params)
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def create_message(self, data: dict) -> dict:
        """Создать сообщение"""
        msg_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            tenant_id = await self._tenant_for(session, project_id=data["projectId"])
            await session.execute(
                text("""
                    INSERT INTO sima_messages (id, project_id, tenant_id, session_id, role, content, actions)
                    VALUES (:id, :project_id, :tenant_id, :session_id, :role, :content, :actions)
                """),
                {
                    "id": msg_id,
                    "project_id": data["projectId"],
                    "tenant_id": tenant_id,
                    "session_id": data.get("sessionId"),
                    "role": data["role"],
                    "content": data["content"],
                    "actions": data.get("actions"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_messages WHERE id = :id"),
                {"id": msg_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    # ==========================================
    # Sessions
    # ==========================================

    async def get_sessions(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("SELECT * FROM sima_sessions WHERE project_id = :pid ORDER BY updated_at DESC"),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def create_session(self, data: dict) -> dict:
        session_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_sessions (id, project_id, name)
                    VALUES (:id, :project_id, :name)
                """),
                {"id": session_id, "project_id": data["projectId"], "name": data.get("name")}
            )
            result = await session.execute(
                text("SELECT * FROM sima_sessions WHERE id = :id"), {"id": session_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def delete_session(self, session_id: str) -> dict:
        async with self.pg.session() as session:
            await session.execute(
                text("DELETE FROM sima_sessions WHERE id = :id"), {"id": session_id}
            )
            return {"deleted": session_id}

    # ==========================================
    # Decisions
    # ==========================================

    async def list_decisions(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("SELECT * FROM sima_decisions WHERE project_id = :pid ORDER BY created_at DESC"),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def delete_decision(self, decision_id: str) -> dict:
        async with self.pg.session() as session:
            await session.execute(
                text("DELETE FROM sima_decisions WHERE id = :id"), {"id": decision_id}
            )
            return {"deleted": decision_id}

    async def create_decision(self, data: dict) -> dict:
        dec_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_decisions (id, project_id, title, description, reasoning, alternatives, session_id)
                    VALUES (:id, :project_id, :title, :description, :reasoning, :alternatives, :session_id)
                """),
                {
                    "id": dec_id,
                    "project_id": data["projectId"],
                    "title": data["title"],
                    "description": data["description"],
                    "reasoning": data.get("reasoning"),
                    "alternatives": data.get("alternatives"),
                    "session_id": data.get("sessionId"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_decisions WHERE id = :id"), {"id": dec_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    # ==========================================
    # Personas
    # ==========================================

    async def get_personas(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("SELECT * FROM sima_personas WHERE project_id = :pid ORDER BY created_at"),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def create_persona(self, data: dict) -> dict:
        persona_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_personas (
                        id, project_id, name, role, tech_level, domain_knowledge,
                        goal, time_budget, motivation, patience_level,
                        traits, fears, previous_experience, constraints, archetype
                    ) VALUES (
                        :id, :project_id, :name, :role, :tech_level, :domain_knowledge,
                        :goal, :time_budget, :motivation, :patience_level,
                        CAST(:traits AS jsonb), CAST(:fears AS jsonb), CAST(:previous_experience AS jsonb), CAST(:constraints AS jsonb), :archetype
                    )
                """),
                {
                    "id": persona_id,
                    "project_id": data["projectId"],
                    "name": data["name"],
                    "role": data["role"],
                    "tech_level": data.get("techLevel", "basic"),
                    "domain_knowledge": data.get("domainKnowledge", "basic"),
                    "goal": data["goal"],
                    "time_budget": data.get("timeBudget", "неограничено"),
                    "motivation": data.get("motivation", "medium"),
                    "patience_level": data.get("patienceLevel", "medium"),
                    "traits": str(data.get("traits", "[]")),
                    "fears": str(data.get("fears", "[]")),
                    "previous_experience": str(data.get("previousExperience", "[]")),
                    "constraints": str(data.get("constraints", "[]")),
                    "archetype": data.get("archetype", "custom"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_personas WHERE id = :id"), {"id": persona_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def delete_persona(self, persona_id: str) -> bool:
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_personas WHERE id = :id"), {"id": persona_id}
            )
            return result.rowcount > 0

    # ==========================================
    # Simulation Reports
    # ==========================================

    async def create_simulation_report(self, data: dict) -> dict:
        report_id = str(uuid.uuid4())
        import json
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_simulation_reports (
                        id, project_id, persona_id, scope, summary, steps,
                        friction_points, recommendations, analysis_time
                    ) VALUES (
                        :id, :project_id, :persona_id, :scope,
                        CAST(:summary AS jsonb), CAST(:steps AS jsonb), CAST(:friction_points AS jsonb), CAST(:recommendations AS jsonb),
                        :analysis_time
                    )
                """),
                {
                    "id": report_id,
                    "project_id": data["projectId"],
                    "persona_id": data["personaId"],
                    "scope": data["scope"],
                    "summary": json.dumps(data["summary"]),
                    "steps": json.dumps(data["steps"]),
                    "friction_points": json.dumps(data.get("frictionPoints", [])),
                    "recommendations": json.dumps(data.get("recommendations", [])),
                    "analysis_time": data.get("analysisTime", 0),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_simulation_reports WHERE id = :id"), {"id": report_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def get_last_simulation_report(self, project_id: str) -> Optional[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("""
                    SELECT * FROM sima_simulation_reports
                    WHERE project_id = :pid ORDER BY created_at DESC LIMIT 1
                """),
                {"pid": project_id}
            )
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    # ==========================================
    # Strategist Reports
    # ==========================================

    async def create_strategist_report(self, data: dict) -> dict:
        report_id = str(uuid.uuid4())
        import json
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_strategist_reports (
                        id, project_id, scope, scope_id, verdict, top_actions,
                        sections, raw_analysis, analysis_time
                    ) VALUES (
                        :id, :project_id, :scope, :scope_id,
                        CAST(:verdict AS jsonb), CAST(:top_actions AS jsonb), CAST(:sections AS jsonb),
                        :raw_analysis, :analysis_time
                    )
                """),
                {
                    "id": report_id,
                    "project_id": data["projectId"],
                    "scope": data["scope"],
                    "scope_id": data.get("scopeId"),
                    "verdict": json.dumps(data["verdict"]),
                    "top_actions": json.dumps(data["topActions"]),
                    "sections": json.dumps(data["sections"]),
                    "raw_analysis": data.get("rawAnalysis"),
                    "analysis_time": data.get("analysisTime", 0),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_strategist_reports WHERE id = :id"), {"id": report_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def get_last_strategist_report(self, project_id: str) -> Optional[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("""
                    SELECT * FROM sima_strategist_reports
                    WHERE project_id = :pid ORDER BY created_at DESC LIMIT 1
                """),
                {"pid": project_id}
            )
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    # ==========================================
    # Snapshots
    # ==========================================

    async def create_snapshot(self, data: dict) -> dict:
        snap_id = str(uuid.uuid4())
        import json

        class _UUIDEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, uuid.UUID):
                    return str(obj)
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return super().default(obj)

        def _safe_dumps(obj):
            return json.dumps(obj, cls=_UUIDEncoder, ensure_ascii=False)

        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_project_snapshots (id, project_id, label, type, blocks_data, connections_data, project_data)
                    VALUES (:id, :project_id, :label, :type, CAST(:blocks_data AS jsonb), CAST(:connections_data AS jsonb), CAST(:project_data AS jsonb))
                """),
                {
                    "id": snap_id,
                    "project_id": data["projectId"],
                    "label": data.get("label"),
                    "type": data.get("type", "auto"),
                    "blocks_data": _safe_dumps(data.get("blocksData", [])),
                    "connections_data": _safe_dumps(data.get("connectionsData", [])),
                    "project_data": _safe_dumps(data.get("projectData")) if data.get("projectData") else None,
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_project_snapshots WHERE id = :id"), {"id": snap_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def list_snapshots(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("SELECT id, project_id, label, type, created_at FROM sima_project_snapshots WHERE project_id = :pid ORDER BY created_at DESC"),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    # ==========================================
    # Data Sources & Insights
    # ==========================================

    async def create_data_source(self, data: dict) -> dict:
        ds_id = str(uuid.uuid4())
        import json
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_data_sources (id, project_id, name, source_type, mime_type, content, raw_metadata, status)
                    VALUES (:id, :project_id, :name, :source_type, :mime_type, :content, CAST(:raw_metadata AS jsonb), :status)
                """),
                {
                    "id": ds_id,
                    "project_id": data["projectId"],
                    "name": data["name"],
                    "source_type": data["sourceType"],
                    "mime_type": data.get("mimeType"),
                    "content": data["content"],
                    "raw_metadata": json.dumps(data.get("rawMetadata")) if data.get("rawMetadata") else None,
                    "status": data.get("status", "uploaded"),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_data_sources WHERE id = :id"), {"id": ds_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def get_data_sources(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("""
                    SELECT ds.*,
                        (SELECT COUNT(*) FROM sima_data_insights WHERE data_source_id = ds.id) as insights_count
                    FROM sima_data_sources ds
                    WHERE ds.project_id = :pid ORDER BY ds.created_at DESC
                """),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def update_data_source_status(self, ds_id: str, status: str) -> None:
        async with self.pg.session() as session:
            await session.execute(
                text("UPDATE sima_data_sources SET status = :status WHERE id = :id"),
                {"id": ds_id, "status": status}
            )

    async def delete_data_source(self, ds_id: str) -> bool:
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_data_sources WHERE id = :id"), {"id": ds_id}
            )
            return result.rowcount > 0

    async def create_data_insight(self, data: dict) -> dict:
        insight_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_data_insights (
                        id, data_source_id, project_id, category, title, content,
                        evidence, confidence, priority, linked_block_id, applied
                    ) VALUES (
                        :id, :data_source_id, :project_id, :category, :title, :content,
                        :evidence, :confidence, :priority, :linked_block_id, :applied
                    )
                """),
                {
                    "id": insight_id,
                    "data_source_id": data["dataSourceId"],
                    "project_id": data["projectId"],
                    "category": data["category"],
                    "title": data["title"],
                    "content": data["content"],
                    "evidence": data.get("evidence"),
                    "confidence": data.get("confidence", 0.7),
                    "priority": data.get("priority", "medium"),
                    "linked_block_id": data.get("linkedBlockId"),
                    "applied": data.get("applied", False),
                }
            )
            result = await session.execute(
                text("SELECT * FROM sima_data_insights WHERE id = :id"), {"id": insight_id}
            )
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def get_data_insights(self, project_id: str) -> list[dict]:
        async with self.pg.session() as session:
            result = await session.execute(
                text("SELECT * FROM sima_data_insights WHERE project_id = :pid ORDER BY created_at"),
                {"pid": project_id}
            )
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def link_insight_to_block(self, insight_id: str, block_id: str) -> None:
        async with self.pg.session() as session:
            await session.execute(
                text("UPDATE sima_data_insights SET linked_block_id = :block_id, applied = TRUE WHERE id = :id"),
                {"id": insight_id, "block_id": block_id}
            )

    async def unlink_insight(self, insight_id: str) -> None:
        async with self.pg.session() as session:
            await session.execute(
                text("UPDATE sima_data_insights SET linked_block_id = NULL, applied = FALSE WHERE id = :id"),
                {"id": insight_id}
            )


    # ==========================================
    # Artifacts — переиспользование между проектами (Sima Remix: галерея)
    # ==========================================

    async def create_artifact(self, user_id: str, data: dict) -> dict:
        """Сохранить артефакт (block|subschema|map|tz|wiki) в библиотеку
        пользователя. payload — содержимое (поля блока / блоки+связи /
        markdown)."""
        import json
        a_id = str(uuid.uuid4())
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO sima_artifacts
                        (id, user_id, artifact_type, title, description,
                         tags, payload, source_project_id, maturity)
                    VALUES (:id, :user_id, :type, :title, :description,
                            CAST(:tags AS jsonb), CAST(:payload AS jsonb),
                            :source_project_id, :maturity)
                """),
                {"id": a_id, "user_id": user_id,
                 "type": data.get("artifactType", "block"),
                 "title": data.get("title", "Артефакт"),
                 "description": data.get("description"),
                 "tags": json.dumps(data.get("tags") or [], ensure_ascii=False),
                 "payload": json.dumps(data.get("payload") or {},
                                       ensure_ascii=False),
                 "source_project_id": data.get("sourceProjectId"),
                 "maturity": (data.get("maturity")
                              if data.get("maturity") in ("draft", "verified", "canon")
                              else "draft")})
            result = await session.execute(
                text("SELECT * FROM sima_artifacts WHERE id = :id"),
                {"id": a_id})
            return _to_camel_dict(dict(result.fetchone()._mapping))

    async def update_artifact_maturity(self, user_id: str, artifact_id: str,
                                       maturity: str) -> dict | None:
        """Сменить маркер зрелости артефакта (только владельцем). Возвращает
        обновлённый артефакт или None если не найден/чужой."""
        if maturity not in ("draft", "verified", "canon"):
            return None
        async with self.pg.session() as session:
            await session.execute(
                text("UPDATE sima_artifacts SET maturity = :m, updated_at = NOW() "
                     "WHERE id = :id AND user_id = :uid"),
                {"m": maturity, "id": artifact_id, "uid": user_id})
            result = await session.execute(
                text("SELECT * FROM sima_artifacts WHERE id = :id AND user_id = :uid"),
                {"id": artifact_id, "uid": user_id})
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    async def list_artifacts(self, user_id: str,
                             artifact_type: str | None = None,
                             query: str | None = None) -> list[dict]:
        """Галерея артефактов пользователя: фильтр по типу и поиск по
        названию/описанию/тегам. usedIn → счётчик переиспользования."""
        sql = ("SELECT * FROM sima_artifacts WHERE user_id = :uid")
        params: dict = {"uid": user_id}
        if artifact_type:
            sql += " AND artifact_type = :atype"
            params["atype"] = artifact_type
        if query:
            sql += (" AND (title ILIKE :q OR description ILIKE :q"
                    " OR tags::text ILIKE :q)")
            params["q"] = f"%{query}%"
        sql += " ORDER BY updated_at DESC"
        async with self.pg.session() as session:
            result = await session.execute(text(sql), params)
            return [_to_camel_dict(dict(r._mapping)) for r in result.fetchall()]

    async def get_artifact(self, artifact_id: str,
                           user_id: str | None = None) -> dict | None:
        """user_id=None — без owner-фильтра (grant-fallback: доступ
        не-владельца уже проверен resource_permissions на уровне роута)."""
        q = "SELECT * FROM sima_artifacts WHERE id = :id"
        params: dict = {"id": artifact_id}
        if user_id:
            q += " AND user_id = :uid"
            params["uid"] = user_id
        async with self.pg.session() as session:
            result = await session.execute(text(q), params)
            row = result.fetchone()
            return _to_camel_dict(dict(row._mapping)) if row else None

    async def mark_artifact_used(self, artifact_id: str, user_id: str,
                                 project_id: str) -> None:
        """Отметить использование артефакта в проекте (used_in —
        уникальный список project_id)."""
        async with self.pg.session() as session:
            await session.execute(
                text("""
                    UPDATE sima_artifacts
                    SET used_in = (
                            SELECT to_jsonb(array_agg(DISTINCT v))
                            FROM jsonb_array_elements_text(
                                used_in || to_jsonb(CAST(:pid AS text))) AS t(v)),
                        updated_at = NOW()
                    WHERE id = :id AND user_id = :uid
                """),
                {"id": artifact_id, "uid": user_id, "pid": project_id})

    async def delete_artifact(self, artifact_id: str, user_id: str) -> bool:
        async with self.pg.session() as session:
            result = await session.execute(
                text("DELETE FROM sima_artifacts WHERE id = :id"
                     " AND user_id = :uid"),
                {"id": artifact_id, "uid": user_id})
            return result.rowcount > 0


# ==========================================
# Singleton
# ==========================================

_sima_client: SimaDBClient | None = None


async def get_sima_db() -> SimaDBClient:
    """Получить SIMA DB клиент"""
    global _sima_client
    if _sima_client is None:
        pg = await get_postgres()
        _sima_client = SimaDBClient(pg)
    return _sima_client
