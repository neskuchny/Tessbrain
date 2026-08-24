# -*- coding: utf-8 -*-
"""
DocumentWriterAgent — генерирует полноценные документы из агрегированного контента.

Создаёт:
- Регламенты с примерами и обоснованиями
- Операционные листы (SOP)
- Технические задания
- Чеклисты
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from .content_aggregator_agent import AggregatedContent

logger = logging.getLogger(__name__)


@dataclass
class GeneratedDocument:
    """Сгенерированный документ."""
    document_id: str
    title: str
    document_type: str  # regulation, sop, specification, checklist
    version: str
    created_at: datetime
    updated_at: datetime

    # Контент
    summary: str
    content_markdown: str  # Полный документ в Markdown
    content_html: str  # HTML версия

    # Метаданные
    topic: str
    keywords: List[str] = field(default_factory=list)
    source_meetings: List[str] = field(default_factory=list)
    authors: List[str] = field(default_factory=list)

    # Структура
    sections: List[Dict[str, Any]] = field(default_factory=list)
    table_of_contents: List[Dict[str, str]] = field(default_factory=list)

    # Статус
    status: str = "draft"  # draft, review, approved, archived
    confidence: float = 0.0
    word_count: int = 0

    # Привязка к пользователю
    user_id: str = ""


class DocumentWriterAgent:
    """
    Агент генерации документов — создаёт полноценные документы из агрегированного контента.
    """

    def __init__(self, llm_router):
        self.llm = llm_router
        logger.info("✅ DocumentWriterAgent initialized")

    async def generate_document(
        self,
        aggregated: AggregatedContent,
        document_type: str = "regulation",
        style: str = "formal",  # formal, casual, technical
        model_tier=None,
    ) -> GeneratedDocument:
        """
        Сгенерировать полноценный документ.

        Args:
            aggregated: Агрегированный контент
            document_type: Тип документа
            style: Стиль написания

        Returns:
            GeneratedDocument
        """
        # Шаг 1: Создать структуру документа
        structure = await self._plan_structure(aggregated, document_type, model_tier=model_tier)

        # Шаг 2: Написать каждый раздел
        sections = await self._write_sections(aggregated, structure, style, model_tier=model_tier)

        # Шаг 3: Собрать документ
        markdown_content = self._assemble_document(aggregated.topic, sections, document_type)

        # Шаг 4: Конвертировать в HTML
        html_content = self._markdown_to_html(markdown_content)

        # Создать документ
        now = datetime.utcnow()
        doc = GeneratedDocument(
            document_id=str(uuid.uuid4()),
            title=self._generate_title(aggregated.topic, document_type),
            document_type=document_type,
            version="1.0",
            created_at=now,
            updated_at=now,
            summary=aggregated.summary,
            content_markdown=markdown_content,
            content_html=html_content,
            topic=aggregated.topic,
            keywords=aggregated.tools_mentioned + list(aggregated.definitions.keys()),
            source_meetings=aggregated.source_meetings,
            authors=aggregated.people_involved[:3],  # Первые 3 участника
            sections=sections,
            table_of_contents=self._generate_toc(sections),
            status="draft",
            confidence=0.8,
            word_count=len(markdown_content.split())
        )

        logger.info(f"📄 Generated document: {doc.title} ({doc.word_count} words)")

        return doc

    async def _plan_structure(
        self,
        aggregated: AggregatedContent,
        document_type: str,
        model_tier=None,
    ) -> List[Dict[str, Any]]:
        """Спланировать структуру документа."""

        system_prompt = f"""Ты — DOCUMENT STRUCTURE PLANNER. Создай структуру документа типа "{document_type}".

## ТИПЫ ДОКУМЕНТОВ И ИХ СТРУКТУРА:

### regulation (Регламент):
1. Введение (цель, область применения)
2. Термины и определения
3. Основной процесс (этапы)
4. Требования и стандарты
5. Примеры
6. Чеклист
7. Приложения

### sop (Операционный лист):
1. Цель процедуры
2. Область применения
3. Ответственные
4. Пошаговая инструкция
5. Контрольные точки
6. Типичные ошибки

### specification (ТЗ):
1. Общие сведения
2. Цели и задачи
3. Функциональные требования
4. Нефункциональные требования
5. Ограничения
6. Критерии приёмки

### checklist (Чеклист):
1. Название и цель
2. Категории проверок
3. Пункты проверки
4. Примечания

## ФОРМАТ ОТВЕТА (JSON):

```json
{{
  "sections": [
    {{
      "id": "section_1",
      "title": "Название раздела",
      "level": 1,
      "description": "Что должно быть в этом разделе",
      "required": true,
      "subsections": [
        {{
          "id": "section_1_1",
          "title": "Подраздел",
          "level": 2,
          "description": "Описание"
        }}
      ]
    }}
  ]
}}
```

ВАЖНО:
- Структура должна соответствовать имеющимся данным
- Не добавляй разделы если нет данных для них
- Учитывай специфику темы
"""

        user_prompt = f"""Тема документа: {aggregated.topic}
Тип документа: {document_type}

Имеющиеся данные:
- Ключевые моменты: {len(aggregated.key_points)}
- Шаги процесса: {len(aggregated.steps)}
- Примеры: {len(aggregated.examples)}
- Лучшие практики: {len(aggregated.best_practices)}
- Предупреждения: {len(aggregated.warnings)}
- Определения: {len(aggregated.definitions)}
- Цитаты: {len(aggregated.raw_quotes)}

Краткое содержание:
{aggregated.summary}

Создай оптимальную структуру документа."""

        try:
            _kw = {"model_tier": model_tier} if model_tier is not None else {}
            result = await self.llm.generate_json(
                prompt=user_prompt,
                system_prompt=system_prompt,
                **_kw,
            )

            if result and "sections" in result:
                return result["sections"]
        except Exception as e:
            logger.error(f"Structure planning failed: {e}")

        # Fallback структура
        return self._default_structure(document_type)

    async def _write_sections(
        self,
        aggregated: AggregatedContent,
        structure: List[Dict[str, Any]],
        style: str,
        model_tier=None,
    ) -> List[Dict[str, Any]]:
        """Написать контент для каждого раздела."""

        written_sections = []

        for section in structure:
            content = await self._write_section(aggregated, section, style, model_tier=model_tier)

            written_section = {
                "id": section.get("id", ""),
                "title": section.get("title", ""),
                "level": section.get("level", 1),
                "content": content,
                "subsections": []
            }

            # Обработать подразделы
            for subsection in section.get("subsections", []):
                sub_content = await self._write_section(aggregated, subsection, style, model_tier=model_tier)
                written_section["subsections"].append({
                    "id": subsection.get("id", ""),
                    "title": subsection.get("title", ""),
                    "level": subsection.get("level", 2),
                    "content": sub_content
                })

            written_sections.append(written_section)

        return written_sections

    async def _write_section(
        self,
        aggregated: AggregatedContent,
        section: Dict[str, Any],
        style: str,
        model_tier=None,
    ) -> str:
        """Написать контент одного раздела."""

        section_title = section.get("title", "")
        section_desc = section.get("description", "")

        system_prompt = f"""Ты — DOCUMENT WRITER. Напиши раздел документа.

## СТИЛЬ: {style}
- formal: Официальный, деловой стиль
- casual: Дружелюбный, понятный стиль
- technical: Технический, точный стиль

## ПРАВИЛА:
1. Пиши ТОЛЬКО на основе предоставленных данных
2. НЕ выдумывай информацию
3. Используй примеры и цитаты из данных
4. Добавляй обоснования ("потому что...", "это важно так как...")
5. Структурируй текст: списки, подзаголовки
6. Если данных недостаточно — напиши кратко

## ФОРМАТ:
- Используй Markdown
- Не добавляй заголовок раздела (он будет добавлен автоматически)
- Начинай сразу с контента
"""

        # Подготовить релевантные данные для раздела
        relevant_data = self._get_relevant_data(aggregated, section_title)

        user_prompt = f"""Раздел: {section_title}
Описание раздела: {section_desc}

## ДАННЫЕ ДЛЯ РАЗДЕЛА:

{relevant_data}

Напиши контент раздела (2-5 абзацев, или список если уместно)."""

        try:
            _kw = {"model_tier": model_tier} if model_tier is not None else {}
            result = await self.llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.3,
                **_kw,
            )

            content = result.strip() if result else ""

            # Удаляем дублирующийся заголовок, если LLM его добавила
            # Проверяем варианты: "# Заголовок", "## Заголовок", "**Заголовок**", "Заголовок"
            lines = content.split('\n')
            if lines:
                first_line = lines[0].strip()
                clean_title = section_title.strip()

                # Очищаем первую строку от маркеров Markdown
                clean_first_line = first_line.lstrip('#').strip()
                if clean_first_line.startswith('**') and clean_first_line.endswith('**'):
                    clean_first_line = clean_first_line[2:-2].strip()

                # Если первая строка совпадает с заголовком (почти)
                if clean_first_line.lower() == clean_title.lower():
                    # Удаляем первую строку и пустые строки после неё
                    content = '\n'.join(lines[1:]).strip()

            return content
        except Exception as e:
            logger.error(f"Section writing failed: {e}")
            return "*Раздел в разработке*"

    def _get_relevant_data(self, aggregated: AggregatedContent, section_title: str) -> str:
        """Получить релевантные данные для раздела."""

        parts = []
        title_lower = section_title.lower()

        # Введение / Общие сведения
        if any(word in title_lower for word in ["введение", "общие", "цель", "назначение"]):
            parts.append(f"Резюме: {aggregated.summary}")
            if aggregated.key_points:
                parts.append("Ключевые моменты:\n" + "\n".join(f"- {p}" for p in aggregated.key_points[:5]))

        # Термины / Определения
        elif any(word in title_lower for word in ["термин", "определени", "глоссарий"]):
            if aggregated.definitions:
                parts.append("Определения:")
                for term, definition in aggregated.definitions.items():
                    parts.append(f"- **{term}**: {definition}")

        # Процесс / Этапы / Шаги
        elif any(word in title_lower for word in ["процесс", "этап", "шаг", "инструкци", "порядок"]):
            if aggregated.steps:
                parts.append("Шаги процесса:")
                for step in aggregated.steps:
                    parts.append(f"\n**Шаг {step.get('step_number', '?')}: {step.get('title', '')}**")
                    parts.append(step.get('description', ''))
                    if step.get('tips'):
                        parts.append("Советы: " + ", ".join(step['tips']))
                    if step.get('example'):
                        parts.append(f"Пример: {step['example']}")

        # Примеры
        elif any(word in title_lower for word in ["пример", "кейс", "случа"]):
            if aggregated.examples:
                parts.append("Примеры:")
                for ex in aggregated.examples:
                    parts.append(f"\n**{ex.get('title', 'Пример')}**")
                    parts.append(ex.get('description', ''))
                    if ex.get('outcome'):
                        parts.append(f"Результат: {ex['outcome']}")
            if aggregated.raw_quotes:
                parts.append("\nЦитаты из обсуждений:")
                for q in aggregated.raw_quotes[:3]:
                    parts.append(f'> "{q.get("quote", "")}"')
                    if q.get('speaker'):
                        parts.append(f"— {q['speaker']}")

        # Лучшие практики / Рекомендации
        elif any(word in title_lower for word in ["практик", "рекомендац", "совет"]):
            if aggregated.best_practices:
                parts.append("Лучшие практики:\n" + "\n".join(f"- {p}" for p in aggregated.best_practices))

        # Ошибки / Предупреждения
        elif any(word in title_lower for word in ["ошибк", "предупрежд", "избега", "не делай"]):
            if aggregated.warnings:
                parts.append("Чего избегать:\n" + "\n".join(f"- ⚠️ {w}" for w in aggregated.warnings))

        # Чеклист
        elif any(word in title_lower for word in ["чеклист", "проверк", "контрол"]):
            checklist_items = []
            if aggregated.steps:
                checklist_items.extend([f"☐ {s.get('title', '')}" for s in aggregated.steps])
            if aggregated.best_practices:
                checklist_items.extend([f"☐ {p.split('.')[0]}" for p in aggregated.best_practices[:5]])
            if checklist_items:
                parts.append("Чеклист:\n" + "\n".join(checklist_items))

        # Инструменты
        elif any(word in title_lower for word in ["инструмент", "tool", "софт", "программ"]):
            if aggregated.tools_mentioned:
                parts.append("Используемые инструменты:\n" + "\n".join(f"- {t}" for t in aggregated.tools_mentioned))

        # Fallback: все данные
        else:
            parts.append(f"Резюме: {aggregated.summary}")
            if aggregated.key_points:
                parts.append("Ключевые моменты:\n" + "\n".join(f"- {p}" for p in aggregated.key_points[:3]))

        return "\n\n".join(parts) if parts else "Данные отсутствуют"

    def _assemble_document(
        self,
        topic: str,
        sections: List[Dict[str, Any]],
        document_type: str
    ) -> str:
        """Собрать документ из разделов."""

        parts = []

        # Заголовок
        type_names = {
            "regulation": "Регламент",
            "sop": "Операционный лист",
            "specification": "Техническое задание",
            "checklist": "Чеклист",
            "guideline": "Руководство"
        }
        type_name = type_names.get(document_type, "Документ")

        parts.append(f"# {type_name}: {topic}\n")
        parts.append(f"*Версия 1.0 | Создано: {datetime.utcnow().strftime('%d.%m.%Y')}*\n")
        parts.append("---\n")

        # Оглавление
        parts.append("## Содержание\n")
        for i, section in enumerate(sections, 1):
            parts.append(f"{i}. [{section['title']}](#{section['id']})")
            for j, sub in enumerate(section.get("subsections", []), 1):
                parts.append(f"   {i}.{j}. [{sub['title']}](#{sub['id']})")
        parts.append("\n---\n")

        # Разделы
        for section in sections:
            level = section.get("level", 1)
            heading = "#" * (level + 1)

            parts.append(f"\n{heading} {section['title']}\n")
            parts.append(f"<a id=\"{section['id']}\"></a>\n")

            if section.get("content"):
                parts.append(section["content"])

            for sub in section.get("subsections", []):
                sub_level = sub.get("level", 2)
                sub_heading = "#" * (sub_level + 1)

                parts.append(f"\n{sub_heading} {sub['title']}\n")
                parts.append(f"<a id=\"{sub['id']}\"></a>\n")

                if sub.get("content"):
                    parts.append(sub["content"])

        # Футер
        parts.append("\n---\n")
        parts.append("*Документ сгенерирован автоматически системой Tessbrain*")

        return "\n".join(parts)

    def _markdown_to_html(self, markdown: str) -> str:
        """Конвертировать Markdown в HTML (базовая конвертация).

        Текст экранируется ПЕРЕД разметкой. Иначе сюда проходил любой сырой
        HTML из исходника: документ собирается из встреч, а встречи пишут
        люди. Строка `<img src=x onerror=…>`, сказанная на созвоне, доезжала
        до content_html как есть и выполнялась у каждого, кто откроет
        документ (фронт рендерит его через dangerouslySetInnerHTML).

        Порядок важен: сначала «&», иначе экранированные скобки схлопнутся
        обратно. Дальше по экранированному тексту работают регулярки — они
        сами вставляют теги, и эти теги остаются настоящими.
        """
        import re

        html = (markdown or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Цитаты markdown («> текст») после экранирования выглядят как «&gt; »
        # — возвращаем им исходный вид, чтобы правило ниже сработало.
        html = re.sub(r'^&gt; ', '> ', html, flags=re.MULTILINE)

        # Заголовки
        html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
        html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
        html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)

        # Жирный и курсив
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)

        # Списки
        html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
        html = re.sub(r'^☐ (.+)$', r'<li class="checklist">☐ \1</li>', html, flags=re.MULTILINE)

        # Цитаты
        html = re.sub(r'^> (.+)$', r'<blockquote>\1</blockquote>', html, flags=re.MULTILINE)

        # Горизонтальные линии
        html = re.sub(r'^---$', r'<hr>', html, flags=re.MULTILINE)

        # Параграфы
        html = re.sub(r'\n\n', '</p><p>', html)
        html = f'<p>{html}</p>'

        return html

    def _generate_title(self, topic: str, document_type: str) -> str:
        """Сгенерировать название документа."""
        type_prefixes = {
            "regulation": "Регламент:",
            "sop": "SOP:",
            "specification": "ТЗ:",
            "checklist": "Чеклист:",
            "guideline": "Руководство:"
        }
        prefix = type_prefixes.get(document_type, "Документ:")
        return f"{prefix} {topic}"

    def _generate_toc(self, sections: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Сгенерировать оглавление."""
        toc = []
        for section in sections:
            toc.append({
                "id": section.get("id", ""),
                "title": section.get("title", ""),
                "level": str(section.get("level", 1))
            })
            for sub in section.get("subsections", []):
                toc.append({
                    "id": sub.get("id", ""),
                    "title": sub.get("title", ""),
                    "level": str(sub.get("level", 2))
                })
        return toc

    def _default_structure(self, document_type: str) -> List[Dict[str, Any]]:
        """Структура по умолчанию."""
        structures = {
            "regulation": [
                {"id": "intro", "title": "Введение", "level": 1, "description": "Цель и область применения"},
                {"id": "process", "title": "Процесс", "level": 1, "description": "Основные этапы"},
                {"id": "examples", "title": "Примеры", "level": 1, "description": "Практические примеры"},
                {"id": "checklist", "title": "Чеклист", "level": 1, "description": "Контрольный список"},
            ],
            "sop": [
                {"id": "purpose", "title": "Цель", "level": 1, "description": "Назначение процедуры"},
                {"id": "steps", "title": "Пошаговая инструкция", "level": 1, "description": "Шаги выполнения"},
                {"id": "errors", "title": "Типичные ошибки", "level": 1, "description": "Чего избегать"},
            ],
            "checklist": [
                {"id": "items", "title": "Пункты проверки", "level": 1, "description": "Список проверок"},
            ]
        }
        return structures.get(document_type, structures["regulation"])

