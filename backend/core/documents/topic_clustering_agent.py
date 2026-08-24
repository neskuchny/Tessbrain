# -*- coding: utf-8 -*-
"""
TopicClusteringAgent — группирует встречи по темам для создания объединённых документов.

Например:
- 3 встречи про презентации → 1 документ "Регламент создания презентаций"
- 5 встреч про продажи → 1 документ "Процесс продаж"
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class TopicCluster:
    """Кластер встреч по теме."""
    cluster_id: str
    topic: str
    description: str
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    total_content_length: int = 0
    recommended_document_type: str = "regulation"
    confidence: float = 0.0


class TopicClusteringAgent:
    """
    Агент кластеризации встреч по темам.

    Объединяет связанные встречи для создания более полных документов.
    """

    def __init__(self, llm_router):
        self.llm = llm_router
        logger.info("✅ TopicClusteringAgent initialized")

    async def cluster_meetings(
        self,
        meetings: List[Dict[str, Any]],
        min_cluster_size: int = 1,
        similarity_threshold: float = 0.6
    ) -> List[TopicCluster]:
        """
        Сгруппировать встречи по темам.

        Args:
            meetings: Список встреч с title, transcript/summary
            min_cluster_size: Минимальное количество встреч в кластере
            similarity_threshold: Порог схожести для объединения

        Returns:
            Список TopicCluster
        """
        if not meetings:
            return []

        # Шаг 1: Извлечь темы из каждой встречи
        meeting_topics = await self._extract_topics(meetings)

        # Шаг 2: Сгруппировать по схожим темам через LLM
        clusters = await self._group_by_topics(meetings, meeting_topics)

        # Шаг 3: Отфильтровать маленькие кластеры
        filtered_clusters = [c for c in clusters if len(c.meetings) >= min_cluster_size]

        logger.info(f"📊 Created {len(filtered_clusters)} topic clusters from {len(meetings)} meetings")

        return filtered_clusters

    async def _extract_topics(
        self,
        meetings: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Извлечь темы из каждой встречи."""

        system_prompt = """Ты — TOPIC EXTRACTION AGENT. Извлеки основную тему встречи.

Ответь в формате JSON:
```json
{
  "main_topic": "Основная тема (например: 'Создание презентаций')",
  "sub_topics": ["подтема 1", "подтема 2"],
  "keywords": ["ключевые", "слова"],
  "domain": "marketing|sales|development|design|management|operations|other"
}
```

ВАЖНО: Тема должна быть ОБОБЩЁННОЙ, а не названием конкретного проекта.
Например:
- "Презентация для X5" → тема: "Создание презентаций для клиентов"
- "Дизайн лендинга Acme" → тема: "Дизайн лендингов"
"""

        topics = {}

        for meeting in meetings:
            meeting_id = meeting.get("id", meeting.get("meeting_id", ""))
            title = meeting.get("title", "")
            content = meeting.get("transcription_text") or meeting.get("transcript") or meeting.get("summary", "")

            user_prompt = f"""Название встречи: {title}

Контент (первые 3000 символов):
{content[:3000]}

Извлеки основную тему."""

            try:
                result = await self.llm.generate_json(
                    prompt=user_prompt,
                    system_prompt=system_prompt
                )

                if result:
                    topics[meeting_id] = {
                        "main_topic": result.get("main_topic", title),
                        "sub_topics": result.get("sub_topics", []),
                        "keywords": result.get("keywords", []),
                        "domain": result.get("domain", "other")
                    }
            except Exception as e:
                logger.warning(f"Failed to extract topic for meeting {meeting_id}: {e}")
                topics[meeting_id] = {
                    "main_topic": title,
                    "sub_topics": [],
                    "keywords": [],
                    "domain": "other"
                }

        return topics

    async def _group_by_topics(
        self,
        meetings: List[Dict[str, Any]],
        meeting_topics: Dict[str, Dict[str, Any]]
    ) -> List[TopicCluster]:
        """Сгруппировать встречи по схожим темам."""

        # Подготовить данные для LLM
        meetings_info = []
        for meeting in meetings:
            meeting_id = meeting.get("id", meeting.get("meeting_id", ""))
            topic_info = meeting_topics.get(meeting_id, {})
            meetings_info.append({
                "id": meeting_id,
                "title": meeting.get("title", ""),
                "topic": topic_info.get("main_topic", ""),
                "keywords": topic_info.get("keywords", []),
                "domain": topic_info.get("domain", "other")
            })

        system_prompt = """Ты — TOPIC CLUSTERING AGENT. Сгруппируй встречи по схожим темам.

ПРАВИЛА:
1. Объединяй встречи, которые относятся к ОДНОМУ ПРОЦЕССУ или ОБЛАСТИ
2. Не объединяй разные процессы только потому что они в одном домене
3. Одна встреча может быть только в одном кластере
4. Если встреча уникальна — создай для неё отдельный кластер

Ответь в формате JSON:
```json
{
  "clusters": [
    {
      "cluster_id": "cluster_1",
      "topic": "Название темы кластера",
      "description": "Описание что объединяет эти встречи",
      "meeting_ids": ["id1", "id2"],
      "keywords": ["общие", "ключевые", "слова"],
      "recommended_document_type": "regulation|guideline|checklist|specification"
    }
  ]
}
```
"""

        user_prompt = f"""Сгруппируй эти встречи по темам:

{meetings_info}

Создай кластеры для связанных встреч."""

        try:
            result = await self.llm.generate_json(
                prompt=user_prompt,
                system_prompt=system_prompt
            )

            if not result or "clusters" not in result:
                # Fallback: каждая встреча в своём кластере
                return self._fallback_clustering(meetings, meeting_topics)

            # Преобразовать результат в TopicCluster
            clusters = []
            meeting_map = {m.get("id", m.get("meeting_id", "")): m for m in meetings}

            for cluster_data in result["clusters"]:
                cluster_meetings = [
                    meeting_map[mid]
                    for mid in cluster_data.get("meeting_ids", [])
                    if mid in meeting_map
                ]

                if not cluster_meetings:
                    continue

                total_length = sum(
                    len(m.get("transcript", "") or m.get("summary", ""))
                    for m in cluster_meetings
                )

                cluster = TopicCluster(
                    cluster_id=cluster_data.get("cluster_id", f"cluster_{len(clusters)}"),
                    topic=cluster_data.get("topic", ""),
                    description=cluster_data.get("description", ""),
                    meetings=cluster_meetings,
                    keywords=cluster_data.get("keywords", []),
                    total_content_length=total_length,
                    recommended_document_type=cluster_data.get("recommended_document_type", "regulation"),
                    confidence=0.8
                )
                clusters.append(cluster)

            return clusters

        except Exception as e:
            logger.error(f"Clustering failed: {e}")
            return self._fallback_clustering(meetings, meeting_topics)

    def _fallback_clustering(
        self,
        meetings: List[Dict[str, Any]],
        meeting_topics: Dict[str, Dict[str, Any]]
    ) -> List[TopicCluster]:
        """Fallback: группировка по domain."""

        domain_groups = defaultdict(list)

        for meeting in meetings:
            meeting_id = meeting.get("id", meeting.get("meeting_id", ""))
            topic_info = meeting_topics.get(meeting_id, {})
            domain = topic_info.get("domain", "other")
            domain_groups[domain].append(meeting)

        clusters = []
        for domain, domain_meetings in domain_groups.items():
            total_length = sum(
                len(m.get("transcript", "") or m.get("summary", ""))
                for m in domain_meetings
            )

            cluster = TopicCluster(
                cluster_id=f"domain_{domain}",
                topic=f"Встречи по теме: {domain}",
                description=f"Автоматически сгруппированные встречи из области {domain}",
                meetings=domain_meetings,
                keywords=[domain],
                total_content_length=total_length,
                recommended_document_type="regulation",
                confidence=0.5
            )
            clusters.append(cluster)

        return clusters

    async def find_related_meetings(
        self,
        target_meeting: Dict[str, Any],
        all_meetings: List[Dict[str, Any]],
        max_related: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Найти встречи, связанные с целевой встречей.

        Args:
            target_meeting: Целевая встреча
            all_meetings: Все доступные встречи
            max_related: Максимальное количество связанных

        Returns:
            Список связанных встреч
        """
        # Кластеризуем все встречи включая целевую
        all_with_target = [target_meeting] + [
            m for m in all_meetings
            if m.get("id") != target_meeting.get("id")
        ]

        clusters = await self.cluster_meetings(all_with_target, min_cluster_size=1)

        # Найти кластер с целевой встречей
        target_id = target_meeting.get("id", target_meeting.get("meeting_id", ""))

        for cluster in clusters:
            cluster_ids = [m.get("id", m.get("meeting_id", "")) for m in cluster.meetings]
            if target_id in cluster_ids:
                # Вернуть другие встречи из этого кластера
                related = [
                    m for m in cluster.meetings
                    if m.get("id", m.get("meeting_id", "")) != target_id
                ]
                return related[:max_related]

        return []

