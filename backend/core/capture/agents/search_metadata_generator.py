# -*- coding: utf-8 -*-
"""
Search Metadata Generator - Генерация метаданных для поиска

Создаёт LLM-описания для всех данных в графе:
1. Описания встреч (GraphDescriptor)
2. Описания узлов (NodeDescriptor)
3. Описания связей (EdgeDescriptor)
4. Резюме чанков (ChunkDescriptor)

Адаптировано из tess_old/module_18_search_metadata/
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class NodeMetadata:
    """Метаданные узла"""
    node_id: str
    label: str
    summary: str
    keywords: List[str]
    searchable_text: str
    importance_score: float
    related_topics: List[str]


class SearchMetadataGenerator:
    """
    Генератор метаданных для улучшения поиска.

    Возможности:
    - Генерация описаний встреч
    - Создание резюме узлов
    - Извлечение ключевых слов
    - Создание searchable text
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "search_metadata_generator"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"🔍 {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def generate_metadata(
        self,
        meeting_id: str,
        transcript: str,
        extracted_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Генерирует метаданные для встречи и всех извлечённых данных.

        Args:
            meeting_id: ID встречи
            transcript: Транскрипт встречи
            extracted_data: Извлечённые данные (tasks, decisions, etc.)

        Returns:
            Метаданные для поиска
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"🔍 Generating search metadata for meeting {meeting_id}")

        result = {
            "meeting_metadata": {},
            "node_metadata": [],
            "keywords": [],
            "topics": [],
            "searchable_chunks": [],
            "stats": {}
        }

        try:
            # 1. Генерируем метаданные встречи
            result["meeting_metadata"] = await self._generate_meeting_metadata(
                meeting_id, transcript, extracted_data
            )

            # 2. Генерируем метаданные для узлов
            result["node_metadata"] = await self._generate_node_metadata(
                extracted_data
            )

            # 3. Извлекаем ключевые слова
            result["keywords"] = await self._extract_keywords(
                transcript, extracted_data
            )

            # 4. Определяем темы
            result["topics"] = await self._identify_topics(
                transcript, extracted_data
            )

            # 5. Создаём searchable chunks
            result["searchable_chunks"] = await self._create_searchable_chunks(
                transcript, extracted_data, result["keywords"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "nodes_processed": len(result["node_metadata"]),
                "keywords_extracted": len(result["keywords"]),
                "topics_identified": len(result["topics"]),
                "chunks_created": len(result["searchable_chunks"])
            }

            logger.info(f"✅ Search metadata generated in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Search metadata error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _generate_meeting_metadata(
        self,
        meeting_id: str,
        transcript: str,
        extracted_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Генерирует метаданные встречи"""
        if not self.llm:
            return self._create_basic_meeting_metadata(meeting_id, extracted_data)

        # Собираем статистику
        tasks_count = len(extracted_data.get("tasks", []))
        decisions_count = len(extracted_data.get("decisions", []))
        participants_count = len(extracted_data.get("participants", []))

        prompt = f"""
Создай ПОИСКОВЫЕ МЕТАДАННЫЕ для встречи.

ТРАНСКРИПТ (фрагмент):
{transcript[:3000]}

СТАТИСТИКА:
- Участников: {participants_count}
- Задач: {tasks_count}
- Решений: {decisions_count}

Создай метаданные для эффективного поиска:

Формат (JSON):
{{
    "meeting_id": "{meeting_id}",
    "title": "краткое название встречи (5-10 слов)",
    "summary": "резюме встречи (2-3 предложения)",
    "main_topics": ["основные темы обсуждения"],
    "key_outcomes": ["ключевые результаты"],
    "action_items_summary": "краткое описание задач",
    "decisions_summary": "краткое описание решений",
    "participants_summary": "кто участвовал и их роли",
    "mood": "general/positive/negative/neutral/urgent",
    "meeting_type": "status/planning/brainstorm/decision/review/other",
    "keywords": ["ключевые слова для поиска"],
    "related_projects": ["связанные проекты"],
    "searchable_text": "полный текст для полнотекстового поиска (включи все важные термины, имена, проекты)",
    "importance_score": 0.0-1.0
}}
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, dict):
                    parsed["meeting_id"] = meeting_id
                    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
                    return parsed
        except Exception as e:
            logger.error(f"Error generating meeting metadata: {e}")

        return self._create_basic_meeting_metadata(meeting_id, extracted_data)

    def _create_basic_meeting_metadata(
        self,
        meeting_id: str,
        extracted_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Создаёт базовые метаданные без LLM"""
        tasks = extracted_data.get("tasks", [])
        decisions = extracted_data.get("decisions", [])
        participants = extracted_data.get("participants", [])

        # Извлекаем имена
        participant_names = []
        for p in participants:
            if isinstance(p, dict):
                name = p.get("name") or p.get("participant_name")
                if name:
                    participant_names.append(name)

        # Извлекаем названия задач
        task_names = []
        for t in tasks:
            if isinstance(t, dict):
                name = t.get("task") or t.get("title") or t.get("description")
                if name:
                    task_names.append(name)

        return {
            "meeting_id": meeting_id,
            "title": f"Meeting {meeting_id}",
            "summary": f"Meeting with {len(participants)} participants, {len(tasks)} tasks, {len(decisions)} decisions",
            "main_topics": [],
            "key_outcomes": task_names[:5],
            "participants_summary": ", ".join(participant_names[:10]),
            "keywords": participant_names[:10] + task_names[:5],
            "searchable_text": " ".join(participant_names + task_names),
            "importance_score": 0.5,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    async def _generate_node_metadata(
        self,
        extracted_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Генерирует метаданные для узлов"""
        metadata = []

        # Обрабатываем участников
        for participant in extracted_data.get("participants", []):
            if isinstance(participant, dict):
                name = participant.get("name") or participant.get("participant_name")
                role = participant.get("role") or participant.get("functional_role") or ""

                metadata.append({
                    "node_type": "Person",
                    "name": name,
                    "summary": f"{name} - {role}" if role else name,
                    "keywords": [name, role] if role else [name],
                    "searchable_text": f"{name} {role}".strip(),
                    "importance_score": 0.7
                })

        # Обрабатываем задачи
        for task in extracted_data.get("tasks", []):
            if isinstance(task, dict):
                task_name = task.get("task") or task.get("title") or task.get("description") or ""
                assignee = task.get("assignee") or task.get("responsible") or ""
                deadline = task.get("deadline") or ""

                metadata.append({
                    "node_type": "Task",
                    "name": task_name,
                    "summary": f"Task: {task_name}" + (f" (assigned to {assignee})" if assignee else ""),
                    "keywords": [task_name, assignee, "task", "задача"],
                    "searchable_text": f"{task_name} {assignee} {deadline}".strip(),
                    "importance_score": 0.8
                })

        # Обрабатываем решения
        for decision in extracted_data.get("decisions", []):
            if isinstance(decision, dict):
                decision_text = decision.get("decision") or decision.get("description") or ""
                maker = decision.get("decision_maker") or decision.get("made_by") or ""

                metadata.append({
                    "node_type": "Decision",
                    "name": decision_text[:100],
                    "summary": f"Decision: {decision_text[:100]}",
                    "keywords": [decision_text[:50], maker, "decision", "решение"],
                    "searchable_text": f"{decision_text} {maker}".strip(),
                    "importance_score": 0.9
                })

        # Обрабатываем сущности
        for entity in extracted_data.get("entities", []):
            if isinstance(entity, dict):
                entity_name = entity.get("name") or entity.get("entity") or ""
                entity_type = entity.get("type") or entity.get("entity_type") or ""

                metadata.append({
                    "node_type": "Entity",
                    "name": entity_name,
                    "summary": f"{entity_type}: {entity_name}" if entity_type else entity_name,
                    "keywords": [entity_name, entity_type],
                    "searchable_text": f"{entity_name} {entity_type}".strip(),
                    "importance_score": 0.6
                })

        return metadata

    async def _extract_keywords(
        self,
        transcript: str,
        extracted_data: Dict[str, Any]
    ) -> List[str]:
        """Извлекает ключевые слова"""
        keywords = set()

        # Добавляем имена участников
        for p in extracted_data.get("participants", []):
            if isinstance(p, dict):
                name = p.get("name") or p.get("participant_name")
                if name:
                    keywords.add(name)

        # Добавляем названия проектов
        for project in extracted_data.get("projects", []):
            if isinstance(project, dict):
                name = project.get("name") or project.get("project_name")
                if name:
                    keywords.add(name)

        # Добавляем сущности
        for entity in extracted_data.get("entities", []):
            if isinstance(entity, dict):
                name = entity.get("name") or entity.get("entity")
                if name:
                    keywords.add(name)

        # Используем LLM для извлечения дополнительных ключевых слов
        if self.llm:
            prompt = f"""
Извлеки КЛЮЧЕВЫЕ СЛОВА для поиска из транскрипта.

ТРАНСКРИПТ (фрагмент):
{transcript[:2000]}

Извлеки:
1. Имена людей
2. Названия проектов и продуктов
3. Технологии и инструменты
4. Компании и организации
5. Важные термины и понятия
6. Даты и сроки

Формат ответа (JSON массив строк):
["keyword1", "keyword2", "keyword3", ...]

Дай 20-30 ключевых слов.
"""

            try:
                response = await self.llm.generate(prompt)
                if response:
                    parsed = self._parse_json_response(response)
                    if isinstance(parsed, list):
                        for kw in parsed:
                            if isinstance(kw, str):
                                keywords.add(kw)
            except Exception as e:
                logger.error(f"Error extracting keywords: {e}")

        return list(keywords)

    async def _identify_topics(
        self,
        transcript: str,
        extracted_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Определяет темы"""
        topics = []

        if not self.llm:
            return topics

        prompt = f"""
Определи ОСНОВНЫЕ ТЕМЫ обсуждения на встрече.

ТРАНСКРИПТ (фрагмент):
{transcript[:2500]}

Для каждой темы укажи:

Формат (JSON массив):
[
    {{
        "topic": "название темы",
        "description": "краткое описание",
        "relevance_score": 0.0-1.0,
        "keywords": ["связанные ключевые слова"],
        "participants_involved": ["участники, обсуждавшие тему"],
        "outcomes": ["результаты обсуждения темы"],
        "follow_up_needed": true/false
    }}
]

Выдели 3-7 основных тем.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error identifying topics: {e}")

        return topics

    async def _create_searchable_chunks(
        self,
        transcript: str,
        extracted_data: Dict[str, Any],
        keywords: List[str]
    ) -> List[Dict[str, Any]]:
        """Создаёт searchable chunks"""
        chunks = []

        # Разбиваем транскрипт на чанки
        chunk_size = 500
        words = transcript.split()

        for i in range(0, len(words), chunk_size):
            chunk_words = words[i:i + chunk_size]
            chunk_text = " ".join(chunk_words)

            # Находим ключевые слова в чанке
            chunk_keywords = []
            chunk_lower = chunk_text.lower()
            for kw in keywords:
                if kw.lower() in chunk_lower:
                    chunk_keywords.append(kw)

            chunks.append({
                "chunk_id": f"chunk_{i // chunk_size}",
                "text": chunk_text,
                "start_position": i,
                "end_position": min(i + chunk_size, len(words)),
                "keywords_found": chunk_keywords,
                "keyword_density": len(chunk_keywords) / len(keywords) if keywords else 0,
                "importance_score": min(1.0, len(chunk_keywords) * 0.1)
            })

        # Сортируем по важности
        chunks.sort(key=lambda x: x.get("importance_score", 0), reverse=True)

        return chunks

    def _parse_json_response(self, response: str) -> Any:
        """Парсит JSON из ответа LLM"""
        if not response:
            return None

        response = response.strip()
        if response.startswith('```json'):
            response = response[7:]
        if response.startswith('```'):
            response = response[3:]
        if response.endswith('```'):
            response = response[:-3]

        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'[\[\{].*[\]\}]', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return None

    async def update_graph_with_metadata(
        self,
        metadata: Dict[str, Any],
        meeting_id: str
    ):
        """Обновляет граф метаданными"""
        if not self.graph:
            return

        try:
            # Обновляем узел встречи
            meeting_metadata = metadata.get("meeting_metadata", {})
            if meeting_metadata:
                all_nodes = await self.graph.get_all_nodes_async()
                meeting_nodes = [n for n in all_nodes
                               if n.get("id") == meeting_id or
                               n.get("meeting_id") == meeting_id]

                for node in meeting_nodes:
                    # Добавляем метаданные к свойствам узла
                    node["search_summary"] = meeting_metadata.get("summary", "")
                    node["search_keywords"] = meeting_metadata.get("keywords", [])
                    node["searchable_text"] = meeting_metadata.get("searchable_text", "")

            # Обновляем другие узлы
            for node_meta in metadata.get("node_metadata", []):
                if isinstance(node_meta, dict):
                    # Находим соответствующий узел
                    node_name = node_meta.get("name", "")
                    node_type = node_meta.get("node_type", "")

                    all_nodes = await self.graph.get_all_nodes_async()
                    matching_nodes = [n for n in all_nodes
                                     if n.get("_label") == node_type and
                                     node_name.lower() in (n.get("name") or "").lower()]

                    for node in matching_nodes:
                        node["search_summary"] = node_meta.get("summary", "")
                        node["search_keywords"] = node_meta.get("keywords", [])
                        node["searchable_text"] = node_meta.get("searchable_text", "")

            # Сохраняем граф
            self.graph.save_graph()

            logger.info(f"💾 Updated graph with search metadata for meeting {meeting_id}")

        except Exception as e:
            logger.error(f"Error updating graph with metadata: {e}")


