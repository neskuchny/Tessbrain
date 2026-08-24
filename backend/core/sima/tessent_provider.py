"""
SIMA TessentProvider — интеграция данных Tessbrain в SIMA.
Загрузка транскриптов встреч из Supabase + документов + данных из Knowledge Graph (Neo4j).
"""
import json

import structlog

from backend.db import set_user_context
from backend.db.supabase_client import get_supabase_client

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════
# Встречи (Meetings)
# ═══════════════════════════════════════════

async def fetch_meetings_list(user_id: str, project_filter: str | None = None) -> dict:
    """Получить список встреч из Supabase с группировкой по папкам/проектам.

    Возвращает:
        {"meetings": [...], "folders": [...], "projects": [...]}
    """
    try:
        set_user_context(user_id=user_id)
        supabase = get_supabase_client()

        # Берём все поля, чтобы достоверно проверить наличие транскрипта
        meetings = await supabase._request(
            "GET",
            "/rest/v1/meetings",
            params={
                "user_id": f"eq.{user_id}",
                "status": "eq.active",
                "select": "*",
                "order": "created_at.desc",
                "limit": "200",
            },
        )

        if not meetings:
            return {"meetings": [], "folders": [], "projects": []}

        # Логируем колонки первой встречи для отладки
        logger.info("Meeting columns", columns=list(meetings[0].keys()))

        # Собираем уникальные project_id и folder_id
        project_ids = set()
        folder_ids = set()
        for m in meetings:
            pid = m.get("project_id")
            fid = m.get("folder_id")
            if pid:
                project_ids.add(pid)
            if fid:
                folder_ids.add(fid)

        # Загружаем имена проектов
        projects_map: dict[str, str] = {}
        if project_ids:
            try:
                projects = await supabase._request(
                    "GET",
                    "/rest/v1/projects",
                    params={
                        "select": "project_id,name",
                        "project_id": f"in.({','.join(project_ids)})",
                    },
                )
                for p in (projects or []):
                    projects_map[p.get("project_id")] = p.get("name") or "Без названия"
            except Exception as e:
                logger.warning("Failed to load project names", error=str(e))

        # Загружаем имена папок
        folders_map: dict[str, str] = {}
        if folder_ids:
            try:
                folders = await supabase._request(
                    "GET",
                    "/rest/v1/folders",
                    params={
                        "select": "id,name",
                        "id": f"in.({','.join(folder_ids)})",
                    },
                )
                for f in (folders or []):
                    folders_map[f.get("id")] = f.get("name") or "Без названия"
            except Exception as e:
                logger.warning("Failed to load folder names", error=str(e))

        # Нормализуем встречи
        result_meetings = []
        for m in meetings:
            transcription_status = m.get("transcription_status", "") or ""
            has_transcript = bool(
                m.get("transcription_text")
                or m.get("transcription")
                or m.get("transcript")
            ) or transcription_status.lower() in (
                "completed", "done", "success", "ready", "transcribed",
            )

            summary_data = m.get("analysis_summary")
            summary_text = None
            if isinstance(summary_data, dict):
                summary_text = summary_data.get("summary")
            elif isinstance(summary_data, str):
                summary_text = summary_data

            pid = m.get("project_id")
            fid = m.get("folder_id")

            result_meetings.append({
                "id": m.get("id"),
                "title": m.get("title") or "Без названия",
                "date": m.get("created_at"),
                "status": m.get("status"),
                "transcriptionStatus": transcription_status,
                "hasTranscript": has_transcript,
                "summary": summary_text,
                "projectId": pid,
                "projectName": projects_map.get(pid) if pid else None,
                "folderId": fid,
                "folderName": folders_map.get(fid) if fid else None,
            })

        # Списки проектов и папок для фильтрации
        projects_list = [
            {"id": pid, "name": name}
            for pid, name in sorted(projects_map.items(), key=lambda x: x[1])
        ]
        folders_list = [
            {"id": fid, "name": name}
            for fid, name in sorted(folders_map.items(), key=lambda x: x[1])
        ]

        return {
            "meetings": result_meetings,
            "folders": folders_list,
            "projects": projects_list,
        }
    except Exception as e:
        logger.error("Failed to fetch meetings list", error=str(e))
        return {"meetings": [], "folders": [], "projects": []}


async def fetch_meeting_transcript(user_id: str, meeting_id: str) -> dict | None:
    """Получить транскрипт конкретной встречи из Supabase."""
    try:
        set_user_context(user_id=user_id)
        supabase = get_supabase_client()

        meeting = await supabase.get_meeting_details(
            meeting_id, include_transcript=True, user_id=user_id)
        if not meeting:
            return None

        content_parts = []

        # Резюме
        summary = meeting.get("analysis_summary")
        if isinstance(summary, dict):
            if summary.get("summary"):
                content_parts.append(f"## Резюме встречи\n{summary['summary']}")
        elif isinstance(summary, str) and summary:
            content_parts.append(f"## Резюме встречи\n{summary}")

        # Ключевые моменты
        key_points = meeting.get("key_points")
        if not key_points and isinstance(summary, dict):
            key_points = summary.get("key_points")
        if key_points:
            if isinstance(key_points, str):
                try:
                    key_points = json.loads(key_points)
                except json.JSONDecodeError:
                    key_points = [key_points]
            if key_points:
                content_parts.append("## Ключевые моменты\n" + "\n".join(f"- {p}" for p in key_points))

        # Задачи
        action_items = meeting.get("action_items")
        if not action_items and isinstance(summary, dict):
            action_items = summary.get("action_items")
        if action_items:
            if isinstance(action_items, str):
                try:
                    action_items = json.loads(action_items)
                except json.JSONDecodeError:
                    action_items = [action_items]
            if action_items:
                content_parts.append("## Задачи\n" + "\n".join(f"- {i}" for i in action_items))

        # Транскрипт
        transcript = (
            meeting.get("transcription_text") or
            meeting.get("transcription") or
            meeting.get("transcript") or
            ""
        )
        if transcript:
            content_parts.append(f"## Транскрипт\n{transcript}")

        if not content_parts:
            return None

        title = meeting.get("title") or "Без названия"
        date = meeting.get("meeting_date") or meeting.get("created_at") or ""
        if date and "T" in str(date):
            date = str(date).split("T")[0]

        return {
            "name": f"Встреча: {title} ({date})",
            "content": "\n\n".join(content_parts),
            "sourceType": "tessent_meetings",
            "mimeType": "text/markdown",
            "metadata": {
                "meeting_id": meeting.get("id"),
                "title": title,
                "date": date,
            },
        }
    except Exception as e:
        logger.error("Failed to fetch meeting transcript", error=str(e), meeting_id=meeting_id)
        return None


# ═══════════════════════════════════════════
# Документы (Documents)
# ═══════════════════════════════════════════

async def fetch_documents_list(user_id: str) -> list[dict]:
    """Получить список доступных документов из Supabase."""
    try:
        supabase = get_supabase_client()
        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,title,doc_type,file_name,content_preview,status,created_at,updated_at",
                "order": "created_at.desc",
                "limit": "100",
            },
        )
        result = []
        for d in (documents or []):
            result.append({
                "id": d.get("id"),
                "title": d.get("title") or d.get("file_name") or "Без названия",
                "docType": d.get("doc_type"),
                "fileName": d.get("file_name"),
                "preview": d.get("content_preview", "")[:200],
                "status": d.get("status"),
                "createdAt": d.get("created_at"),
            })
        return result
    except Exception as e:
        logger.error("Failed to fetch documents list", error=str(e))
        return []


async def fetch_document_content(user_id: str, document_id: str) -> dict | None:
    """Получить полный контент документа из Supabase."""
    try:
        supabase = get_supabase_client()
        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{user_id}",
                "select": "id,title,content,doc_type,file_name,metadata",
            },
        )

        if not documents:
            return None

        doc = documents[0]
        content = doc.get("content", "")
        if not content:
            return None

        title = doc.get("title") or doc.get("file_name") or "Без названия"
        doc_type = doc.get("doc_type") or "text"

        return {
            "name": f"Документ: {title}",
            "content": content,
            "sourceType": "tessent_documents",
            "mimeType": f"text/{doc_type}" if doc_type in ("markdown", "plain", "csv") else "text/plain",
            "metadata": {
                "document_id": doc.get("id"),
                "title": title,
                "doc_type": doc_type,
                "file_name": doc.get("file_name"),
            },
        }
    except Exception as e:
        logger.error("Failed to fetch document content", error=str(e), document_id=document_id)
        return None


# ═══════════════════════════════════════════
# Knowledge Graph (Neo4j)
# ═══════════════════════════════════════════

async def fetch_knowledge_graph_data(
    user_id: str,
    keywords: list[str] | None = None,
    entity_types: list[str] | None = None,
    limit: int = 50,
) -> dict | None:
    """Получить данные из Knowledge Graph (Neo4j) для SIMA."""
    try:
        from backend.db.neo4j_client import get_neo4j_client

        neo4j = await get_neo4j_client()
        if not neo4j:
            logger.warning("Neo4j client not available")
            return None

        # Ищем сущности по ключевым словам
        entities = []
        relationships = []

        if keywords:
            # Поиск сущностей, содержащих ключевые слова
            keyword_pattern = "|".join(keywords)
            query = """
                MATCH (e:Entity)
                WHERE e.user_id = $user_id
                AND (e.name =~ $pattern OR e.description =~ $pattern)
                RETURN e.id as id, e.name as name, e.type as type,
                       e.description as description, labels(e) as labels
                LIMIT $limit
            """
            result = await neo4j.execute_query(
                query,
                {"user_id": user_id, "pattern": f"(?i).*({keyword_pattern}).*", "limit": limit}
            )
            entities = [dict(record) for record in result] if result else []

            # Получаем связи между найденными сущностями
            if entities:
                entity_ids = [e["id"] for e in entities if e.get("id")]
                rel_query = """
                    MATCH (a:Entity)-[r]->(b:Entity)
                    WHERE a.id IN $ids AND b.id IN $ids
                    RETURN a.name as from_name, type(r) as relation, b.name as to_name,
                           r.description as description
                    LIMIT 100
                """
                rel_result = await neo4j.execute_query(rel_query, {"ids": entity_ids})
                relationships = [dict(r) for r in rel_result] if rel_result else []

        elif entity_types:
            query = """
                MATCH (e:Entity)
                WHERE e.user_id = $user_id AND e.type IN $types
                RETURN e.id as id, e.name as name, e.type as type,
                       e.description as description
                LIMIT $limit
            """
            result = await neo4j.execute_query(
                query,
                {"user_id": user_id, "types": entity_types, "limit": limit}
            )
            entities = [dict(record) for record in result] if result else []

        if not entities:
            return None

        # Формируем контент
        content_parts = ["## Данные из Knowledge Graph Tessbrain\n"]
        content_parts.append(f"Найдено сущностей: {len(entities)}, связей: {len(relationships)}\n")

        content_parts.append("### Сущности\n")
        for e in entities:
            content_parts.append(f"- **{e.get('name')}** ({e.get('type', 'unknown')}): {e.get('description') or 'нет описания'}")

        if relationships:
            content_parts.append("\n### Связи\n")
            for r in relationships:
                content_parts.append(f"- {r.get('from_name')} --[{r.get('relation')}]--> {r.get('to_name')}: {r.get('description') or ''}")

        return {
            "name": f"Knowledge Graph: {', '.join(keywords or entity_types or ['все'])}",
            "content": "\n".join(content_parts),
            "sourceType": "tessent_knowledge",
            "mimeType": "text/markdown",
            "metadata": {
                "entities_count": len(entities),
                "relationships_count": len(relationships),
                "keywords": keywords,
                "entity_types": entity_types,
            },
        }
    except Exception as e:
        logger.error("Failed to fetch knowledge graph data", error=str(e))
        return None
