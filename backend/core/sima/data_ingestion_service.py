"""
SIMA Data Ingestion Service — загрузка, парсинг и анализ данных.
Портировано из sima-app/src/lib/data-ingestion-service.ts
Включает TessentProvider для интеграции с транскриптами встреч и Knowledge Graph.
"""
import csv
import io
import json

import structlog

from backend.core.llm.router import ModelTier, TaskType, get_llm_router
from backend.core.sima.prompts import INSIGHT_EXTRACTION_PROMPT
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════
# Парсинг файлов
# ═══════════════════════════════════════════

def parse_csv_content(content: str) -> dict:
    """Парсит CSV/TSV содержимое."""
    lines = content.strip().split("\n")
    if not lines:
        return {"headers": [], "rows": [], "summary": "Пустой CSV"}

    # Определяем разделитель
    first_line = lines[0]
    if "\t" in first_line:
        sep = "\t"
    elif ";" in first_line:
        sep = ";"
    else:
        sep = ","

    reader = csv.reader(io.StringIO(content), delimiter=sep)
    rows = list(reader)
    headers = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    summary = f"CSV: {len(headers)} колонок, {len(data_rows)} строк.\nКолонки: {', '.join(headers)}"
    if data_rows:
        summary += f"\nПример первой строки: {' | '.join(data_rows[0])}"

    return {"headers": headers, "rows": data_rows, "summary": summary}


def parse_json_content(content: str) -> dict:
    """Парсит JSON содержимое."""
    try:
        data = json.loads(content)
        if isinstance(data, list):
            keys = list(data[0].keys()) if data and isinstance(data[0], dict) else []
            summary = f"JSON массив: {len(data)} элементов. Ключи первого: {', '.join(keys)}"
        else:
            summary = f"JSON объект. Ключи: {', '.join(data.keys()) if isinstance(data, dict) else 'неизвестно'}"
        return {"data": data, "summary": summary}
    except json.JSONDecodeError:
        return {"data": None, "summary": "Невалидный JSON"}


def parse_markdown_content(content: str) -> dict:
    """Парсит Markdown содержимое."""
    sections = []
    lines = content.split("\n")
    current_heading = "Введение"
    current_content = []

    for line in lines:
        if line.startswith("#"):
            heading_text = line.lstrip("#").strip()
            if current_content:
                sections.append({"heading": current_heading, "content": "\n".join(current_content).strip()})
            current_heading = heading_text
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections.append({"heading": current_heading, "content": "\n".join(current_content).strip()})

    summary = f"Markdown: {len(sections)} секций. Разделы: {', '.join(s['heading'] for s in sections)}"
    return {"sections": sections, "summary": summary}


def parse_content(content: str, mime_type: str | None = None) -> str:
    """Автопарсинг по MIME-типу."""
    if not mime_type:
        return content[:10000]

    if mime_type in ("text/csv", "application/csv", "text/tab-separated-values"):
        result = parse_csv_content(content)
        return f"{result['summary']}\n\n--- RAW DATA ---\n{content[:5000]}"
    elif mime_type == "application/json":
        result = parse_json_content(content)
        return f"{result['summary']}\n\n--- RAW DATA ---\n{content[:5000]}"
    elif mime_type == "text/markdown":
        result = parse_markdown_content(content)
        return f"{result['summary']}\n\n--- RAW DATA ---\n{content[:5000]}"
    else:
        return content[:10000]


# ═══════════════════════════════════════════
# Сохранение и получение
# ═══════════════════════════════════════════

async def save_data_source(project_id: str, item: dict) -> dict:
    """Сохранить источник данных."""
    db = await get_sima_db()
    return await db.create_data_source({
        "projectId": project_id,
        "name": item.get("name", "Без названия"),
        "sourceType": item.get("sourceType", "text"),
        "mimeType": item.get("mimeType", "text/plain"),
        "content": item.get("content", ""),
        "rawMetadata": item.get("metadata"),
        "status": "uploaded",
    })


async def get_project_data_sources(project_id: str) -> list[dict]:
    db = await get_sima_db()
    return await db.get_data_sources(project_id)


async def delete_data_source(ds_id: str) -> bool:
    db = await get_sima_db()
    return await db.delete_data_source(ds_id)


# ═══════════════════════════════════════════
# Извлечение инсайтов
# ═══════════════════════════════════════════

async def extract_insights(project_id: str, data_source_id: str, user_id: str) -> list[dict]:
    """AI анализирует данные и извлекает инсайты."""
    db = await get_sima_db()

    # Получаем источник данных
    sources = await db.get_data_sources(project_id)
    ds = next((s for s in sources if s.get("id") == data_source_id), None)
    if not ds:
        raise ValueError("DataSource not found")

    # Контекст проекта
    project = await db.get_project(project_id, user_id)
    if project:
        blocks = project.get("blocks", [])
        project_context = f'Проект: "{project.get("name")}". {project.get("description") or ""}\n'
        block_labels = ", ".join(f'{b.get("label")}: {b.get("name")}' for b in blocks) or "пока нет"
        project_context += f"Блоки: {block_labels}"
    else:
        project_context = "Проект пустой."

    parsed_content = parse_content(ds.get("content", ""), ds.get("mime_type"))

    # Обновляем статус
    await db.update_data_source_status(data_source_id, "processing")

    try:
        router = get_llm_router()
        result = await router.generate_json(
            prompt=f'Источник: "{ds.get("name")}" ({ds.get("source_type")}, {ds.get("mime_type")})\n\n{parsed_content}',
            system_prompt=INSIGHT_EXTRACTION_PROMPT + f"\n\nКОНТЕКСТ ПРОЕКТА:\n{project_context}",
            task_type=TaskType.EXTRACTION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.5,
        )

        insights = result.get("insights", []) if isinstance(result, dict) else []

        # Сохраняем инсайты
        saved_insights = []
        for insight in insights:
            saved = await db.create_data_insight({
                "dataSourceId": data_source_id,
                "projectId": project_id,
                "category": insight.get("category", "user_need"),
                "title": insight.get("title", ""),
                "content": insight.get("content", ""),
                "evidence": insight.get("evidence"),
                "confidence": insight.get("confidence", 0.7),
                "priority": insight.get("priority", "medium"),
            })
            saved_insights.append(saved)

        await db.update_data_source_status(data_source_id, "analyzed")
        return saved_insights

    except Exception as e:
        await db.update_data_source_status(data_source_id, "error")
        logger.error("Insight extraction error", error=str(e))
        raise


async def link_insight_to_block(insight_id: str, block_id: str) -> None:
    db = await get_sima_db()
    await db.link_insight_to_block(insight_id, block_id)


async def unlink_insight(insight_id: str) -> None:
    db = await get_sima_db()
    await db.unlink_insight(insight_id)


async def get_project_insights_context(project_id: str) -> str:
    """Собирает инсайты проекта в текстовый контекст для AI."""
    db = await get_sima_db()
    insights = await db.get_data_insights(project_id)

    if not insights:
        return ""

    category_labels = {
        "user_need": "Потребности пользователей",
        "pain_point": "Проблемы и боли",
        "feature_idea": "Идеи функций",
        "market_trend": "Рыночные тренды",
        "tech_requirement": "Технические требования",
        "business_metric": "Бизнес-метрики",
        "quote": "Ключевые цитаты",
        "persona_trait": "Характеристики аудитории",
    }

    grouped: dict[str, list] = {}
    for ins in insights:
        cat = ins.get("category", "other")
        grouped.setdefault(cat, []).append(ins)

    context = f"\n\n=== ДАННЫЕ ИССЛЕДОВАНИЙ ===\nВсего инсайтов: {len(insights)}\n\n"
    for category, items in grouped.items():
        context += f"### {category_labels.get(category, category)}\n"
        for item in items:
            emoji = "🔴" if item.get("priority") == "high" else "🟡" if item.get("priority") == "medium" else "🟢"
            context += f"{emoji} **{item['title']}** ({round(item.get('confidence', 0.7) * 100)}%)\n  {item['content']}\n"
            if item.get("evidence"):
                context += f'  📎 "{item["evidence"]}"\n'
            context += "\n"

    return context
