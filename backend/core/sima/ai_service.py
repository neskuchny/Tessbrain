"""
SIMA AI Service — основной сервис обработки чата и генерации.
Портировано из sima-app/src/lib/ai-service.ts
Использует LLMRouter Tessbrain вместо прямого OpenAI.
"""
import json

import structlog

from backend.core.llm.router import ModelTier, TaskType, get_llm_router
from backend.core.sima.prompts import (
    BRAINSTORM_BUILD_PROMPT,
    BRAINSTORM_PROMPT,
    DEEP_INTERVIEW_FOLLOWUP_PROMPT,
    DEEP_INTERVIEW_PROMPT,
    PARSE_TZ_PROMPT,
    SYSTEM_PROMPT,
)
from backend.db.sima_client import SimaDBClient, get_sima_db

logger = structlog.get_logger(__name__)


async def _get_insights_context(db: SimaDBClient, project_id: str) -> str:
    """Собирает контекст из инсайтов Data Ingestion Layer."""
    try:
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
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(ins)

        context = "\n\n=== ДАННЫЕ ИССЛЕДОВАНИЙ ===\n"
        context += f"Всего инсайтов: {len(insights)}\n\n"

        for category, items in grouped.items():
            context += f"### {category_labels.get(category, category)}\n"
            for item in items:
                priority_emoji = "🔴" if item.get("priority") == "high" else "🟡" if item.get("priority") == "medium" else "🟢"
                conf = item.get("confidence", 0.7)
                context += f"{priority_emoji} **{item['title']}** (уверенность: {round(conf * 100)}%)\n"
                context += f"  {item['content']}\n"
                if item.get("evidence"):
                    context += f'  📎 "{item["evidence"]}"\n'
                context += "\n"

        return context
    except Exception:
        return ""


def _build_project_context(blocks: list[dict], connections: list[dict], subschema_context: str | None = None) -> str:
    """Формирует контекст текущего состояния проекта."""
    subschema_note = ""
    if subschema_context:
        subschema_note = f"""

ВАЖНО: Пользователь сейчас находится ВНУТРИ ПОДСХЕМЫ: {subschema_context}.
Все создаваемые блоки и связи будут размещены в этой подсхеме.
Если пользователь просит «описать этот блок» — используй UPDATE_BLOCK с blockId = label родительского блока.
Заполни как можно больше полей: description, goal, businessValue, userBenefit, problemSolved, inputData, outputData, implementation, risks, blockNarrative, acceptanceCriteria, edgeCases."""

    if blocks:
        block_strs = [f'{b.get("label", "?")} "{b.get("name", "?")}" ({b.get("type", "?")})' for b in blocks]
        conn_strs = []
        for c in connections:
            from_block = next((b for b in blocks if b.get("id") == c.get("fromBlockId") or b.get("id") == c.get("from_block_id")), None)
            to_block = next((b for b in blocks if b.get("id") == c.get("toBlockId") or b.get("id") == c.get("to_block_id")), None)
            from_label = from_block.get("label", "?") if from_block else "?"
            to_label = to_block.get("label", "?") if to_block else "?"
            conn_strs.append(f'{from_label} → {to_label}: {c.get("label", "без подписи")}')
        return f"\n\nТЕКУЩЕЕ СОСТОЯНИЕ ПРОЕКТА:\nБлоки: {', '.join(block_strs)}\nСвязи: {', '.join(conn_strs)}{subschema_note}"
    else:
        return f"\n\nПроект пустой, блоков пока нет.{subschema_note}"


async def process_user_message(
    project_id: str,
    user_message: str,
    user_id: str,
    subschema_context: str | None = None,
) -> dict:
    """Обработать сообщение пользователя в чате SIMA."""
    db = await get_sima_db()
    router = get_llm_router()

    # Получаем текущее состояние проекта
    project = await db.get_project(project_id, user_id)
    if not project:
        return {"message": "Проект не найден.", "actions": []}

    blocks = project.get("blocks", [])
    connections = project.get("connections", [])

    # Контекст проекта
    project_context = _build_project_context(blocks, connections, subschema_context)

    # Контекст инсайтов
    insights_context = await _get_insights_context(db, project_id)

    # История сообщений
    messages = await db.get_messages(project_id)
    chat_history = [{"role": m["role"], "content": m["content"]} for m in messages[-16:]]

    # Формируем промпт
    full_system = SYSTEM_PROMPT + project_context + insights_context

    # Формируем сообщения для LLM
    llm_messages = [{"role": "system", "content": full_system}]
    llm_messages.extend(chat_history)
    llm_messages.append({"role": "user", "content": user_message})

    try:
        result = await router.generate_json(
            prompt=user_message,
            system_prompt=full_system,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
        )

        if isinstance(result, dict):
            message = result.get("message", "Обработано.")
            actions = result.get("actions", [])
        else:
            message = str(result)
            actions = []

        return {"message": message, "actions": actions}
    except Exception as e:
        logger.error("SIMA AI error", error=str(e))
        return {"message": f"Ошибка AI: {e!s}", "actions": []}


async def generate_block_description(
    project_id: str,
    block_id: str,
    sections: list[str],
    user_id: str,
) -> dict:
    """Генерация описания блока по запрошенным секциям."""
    db = await get_sima_db()
    router = get_llm_router()

    project = await db.get_project(project_id, user_id)
    if not project:
        return {}

    blocks = project.get("blocks", [])
    block = next((b for b in blocks if str(b.get("id", "")) == str(block_id)), None)
    if not block:
        return {}

    connections = project.get("connections", [])
    block_strs = [f'{b.get("label")} "{b.get("name")}"' for b in blocks]
    conn_strs = []
    for c in connections:
        c_from = c.get("fromBlockId") or c.get("from_block_id") or ""
        c_to = c.get("toBlockId") or c.get("to_block_id") or ""
        from_b = next((b for b in blocks if str(b.get("id", "")) == str(c_from)), None)
        to_b = next((b for b in blocks if str(b.get("id", "")) == str(c_to)), None)
        conn_strs.append(f'{from_b.get("label", "?") if from_b else "?"} -> {to_b.get("label", "?") if to_b else "?"}')

    # Маппинг секций на поля BlockData
    section_fields = {
        "business": ["businessValue", "userBenefit", "problemSolved", "impactIfRemoved", "metrics"],
        "functional": ["description", "goal", "inputData", "outputData", "acceptanceCriteria", "userFlow", "edgeCases"],
        "technical": ["implementation", "blockTechStack", "apiEndpoints", "dataModel", "filesToCreate", "technicalDeps"],
        "context": ["description", "goal"],
    }
    requested_fields = []
    for s in sections:
        requested_fields.extend(section_fields.get(s, ["description"]))
    requested_fields = list(dict.fromkeys(requested_fields))  # unique, preserve order

    prompt = f"""Заполни описание блока "{block.get('name')}" ({block.get('label')}).

Контекст проекта: {project.get('description') or project.get('name')}
Цель проекта: {project.get('goal') or 'не указана'}

Блок: {block.get('name')}, тип: {block.get('type')}, описание: {block.get('description') or 'нет'}
Другие блоки: {', '.join(block_strs)}
Связи: {', '.join(conn_strs)}

Заполни следующие поля блока: {', '.join(requested_fields)}

Ответь JSON объектом с ключами из списка полей выше. Значения — строки с подробным описанием.
Пример формата ответа:
{{
  "description": "Подробное описание...",
  "goal": "Цель модуля..."
}}

Пиши подробно и конкретно."""

    try:
        result = await router.generate_json(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
        )
        if isinstance(result, dict):
            return {"description": result}
        return {}
    except Exception as e:
        logger.error("Block description error", error=str(e))
        return {}


async def generate_narrative(project_id: str, user_id: str) -> dict:
    """Генерация нарратива продукта (5 разделов)."""
    db = await get_sima_db()
    router = get_llm_router()

    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    blocks = project.get("blocks", [])
    connections = project.get("connections", [])
    decisions = project.get("decisions", [])

    blocks_info = json.dumps([{
        "label": b.get("label"), "name": b.get("name"), "type": b.get("type"),
        "layer": b.get("layer"), "description": b.get("description"),
        "goal": b.get("goal"),
        "businessValue": b.get("businessValue") or b.get("business_value"),
        "problemSolved": b.get("problemSolved") or b.get("problem_solved"),
        "implementation": b.get("implementation"),
    } for b in blocks], ensure_ascii=False, indent=2)

    conn_info = json.dumps([{
        "from": next((b.get("label") for b in blocks if b.get("id") == c.get("fromBlockId") or b.get("id") == c.get("from_block_id")), "?"),
        "to": next((b.get("label") for b in blocks if b.get("id") == c.get("toBlockId") or b.get("id") == c.get("to_block_id")), "?"),
        "label": c.get("label"),
        "dataDescription": c.get("dataDescription") or c.get("data_description"),
    } for c in connections], ensure_ascii=False, indent=2)

    decisions_text = "\n".join([f"- {d['title']}: {d['description']}" for d in decisions]) if decisions else "нет"

    prompt = f"""Сгенерируй полный нарратив продукта.

Проект: {project.get('name')}
Описание: {project.get('description') or 'нет'}
Цель: {project.get('goal') or 'нет'}
Аудитория: {project.get('targetAudience') or project.get('target_audience') or 'не указана'}
MVP: {project.get('mvpDescription') or project.get('mvp_description') or 'не описан'}

Блоки: {blocks_info}
Связи: {conn_info}
Решения: {decisions_text}

Сгенерируй нарратив из 5 разделов в JSON:
{{"problem": "...", "solution": "...", "howItWorks": "...", "productMap": "...", "decisions": "..."}}

Каждый раздел — 3-5 абзацев. Пиши как для инвестора."""

    try:
        result = await router.generate_json(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
        )

        if isinstance(result, dict):
            full_text = "\n\n---\n\n".join([
                f"# Проблема и боль\n\n{result.get('problem', '')}",
                f"# Решение\n\n{result.get('solution', '')}",
                f"# Как это работает\n\n{result.get('howItWorks', '')}",
                f"# Карта продукта\n\n{result.get('productMap', '')}",
                f"# Ключевые решения\n\n{result.get('decisions', '')}",
            ])
            result["fullText"] = full_text
            return result
        return {"problem": "", "solution": "", "howItWorks": "", "productMap": "", "decisions": "", "fullText": ""}
    except Exception as e:
        logger.error("Narrative generation error", error=str(e))
        return {"problem": "", "solution": "", "howItWorks": "", "productMap": "", "decisions": "", "fullText": ""}


async def generate_tz(
    project_id: str,
    user_id: str,
    meeting_ids: list[str] | None = None,
    document_ids: list[str] | None = None,
) -> str:
    """Генерация полного ТЗ. Можно передать meeting_ids/document_ids для прямого включения данных."""
    db = await get_sima_db()
    router = get_llm_router()

    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    blocks = project.get("blocks", [])
    connections = project.get("connections", [])
    decisions = project.get("decisions", [])

    blocks_detailed = json.dumps([{
        "label": b.get("label"), "name": b.get("name"), "type": b.get("type"),
        "layer": b.get("layer"),
        "businessValue": b.get("businessValue") or b.get("business_value"),
        "userBenefit": b.get("userBenefit") or b.get("user_benefit"),
        "problemSolved": b.get("problemSolved") or b.get("problem_solved"),
        "metrics": b.get("metrics"),
        "description": b.get("description"), "goal": b.get("goal"),
        "inputData": b.get("inputData") or b.get("input_data"),
        "outputData": b.get("outputData") or b.get("output_data"),
        "acceptanceCriteria": b.get("acceptanceCriteria") or b.get("acceptance_criteria"),
        "userFlow": b.get("userFlow") or b.get("user_flow"),
        "edgeCases": b.get("edgeCases") or b.get("edge_cases"),
        "implementation": b.get("implementation"),
        "blockTechStack": b.get("blockTechStack") or b.get("block_tech_stack"),
        "apiEndpoints": b.get("apiEndpoints") or b.get("api_endpoints"),
        "dataModel": b.get("dataModel") or b.get("data_model"),
        "filesToCreate": b.get("filesToCreate") or b.get("files_to_create"),
        "estimatedComplexity": b.get("estimatedComplexity") or b.get("estimated_complexity"),
        "risks": b.get("risks"), "alternatives": b.get("alternatives"),
    } for b in blocks], ensure_ascii=False, indent=2)

    connections_detailed = json.dumps([{
        "from": f'{next((b.get("label") for b in blocks if b.get("id") == c.get("fromBlockId") or b.get("id") == c.get("from_block_id")), "?")} "{next((b.get("name") for b in blocks if b.get("id") == c.get("fromBlockId") or b.get("id") == c.get("from_block_id")), "?")}"',
        "to": f'{next((b.get("label") for b in blocks if b.get("id") == c.get("toBlockId") or b.get("id") == c.get("to_block_id")), "?")} "{next((b.get("name") for b in blocks if b.get("id") == c.get("toBlockId") or b.get("id") == c.get("to_block_id")), "?")}"',
        "type": c.get("type"), "label": c.get("label"), "description": c.get("description"),
        "dataDescription": c.get("dataDescription") or c.get("data_description"),
        "dataFormat": c.get("dataFormat") or c.get("data_format"),
        "condition": c.get("condition"),
        "errorHandling": c.get("errorHandling") or c.get("error_handling"),
        "businessMeaning": c.get("businessMeaning") or c.get("business_meaning"),
    } for c in connections], ensure_ascii=False, indent=2)

    decisions_text = "\n".join([f"- {d['title']}: {d['description']} (причина: {d.get('reasoning', 'не указана')})" for d in decisions]) if decisions else "нет"

    # === Собираем данные из исследований (встречи, документы, и пр.) ===
    insights_context = await _get_insights_context(db, project_id)

    # Собираем контент из ранее импортированных источников
    data_sources = await db.get_data_sources(project_id)
    sources_context = ""
    # Реестр источников s1..sN — для ЧЕСТНОЙ трассировки разделов ТЗ
    # (чипы «источники: s1, s3» в бумажном виде). Id стабильны в рамках
    # этой генерации и встраиваются в markdown скрытым комментарием.
    source_registry: list[dict] = []

    def _sid(name: str, kind: str) -> str:
        source_registry.append({"id": f"s{len(source_registry) + 1}",
                                "name": (name or "источник")[:120],
                                "kind": kind})
        return source_registry[-1]["id"]

    if data_sources:
        meeting_sources = []
        document_sources = []
        other_sources = []
        for src in data_sources:
            src_type = src.get("source_type", "")
            src_name = src.get("name", "")
            content = src.get("content", "") or ""
            content_preview = content[:3000] + ("..." if len(content) > 3000 else "")
            entry = {"name": src_name, "content": content_preview}
            if "meeting" in src_type or "transcript" in src_type:
                entry["sid"] = _sid(src_name, "meeting")
                meeting_sources.append(entry)
            elif "document" in src_type:
                entry["sid"] = _sid(src_name, "document")
                document_sources.append(entry)
            else:
                entry["sid"] = _sid(src_name, "other")
                other_sources.append(entry)

        if meeting_sources:
            sources_context += "\n\n=== ДАННЫЕ ИЗ ВСТРЕЧ (импортированные) ===\n"
            for ms in meeting_sources:
                sources_context += f"\n--- [{ms['sid']}] {ms['name']} ---\n{ms['content']}\n"
        if document_sources:
            sources_context += "\n\n=== ДАННЫЕ ИЗ ДОКУМЕНТОВ (импортированные) ===\n"
            for ds in document_sources:
                sources_context += f"\n--- [{ds['sid']}] {ds['name']} ---\n{ds['content']}\n"
        if other_sources:
            sources_context += "\n\n=== ДРУГИЕ ИСТОЧНИКИ ===\n"
            for os_ in other_sources:
                sources_context += f"\n--- [{os_['sid']}] {os_['name']} ---\n{os_['content']}\n"

    # === Прямая загрузка встреч/документов по ID (без предварительного импорта) ===
    direct_context = ""
    if meeting_ids:
        from backend.core.sima.tessent_provider import fetch_meeting_transcript
        direct_context += "\n\n=== ВЫБРАННЫЕ ВСТРЕЧИ ===\n"
        for mid in meeting_ids:
            try:
                data = await fetch_meeting_transcript(user_id, mid)
                if data and data.get("content"):
                    name = data.get("name", mid)
                    content = data["content"][:5000] + ("..." if len(data["content"]) > 5000 else "")
                    direct_context += f"\n--- [{_sid(name, 'meeting')}] {name} ---\n{content}\n"
            except Exception as e:
                logger.warning("Failed to load meeting for TZ", meeting_id=mid, error=str(e))

    if document_ids:
        from backend.core.sima.tessent_provider import fetch_document_content
        direct_context += "\n\n=== ВЫБРАННЫЕ ДОКУМЕНТЫ ===\n"
        for did in document_ids:
            try:
                data = await fetch_document_content(user_id, did)
                if data and data.get("content"):
                    name = data.get("name", did)
                    content = data["content"][:5000] + ("..." if len(data["content"]) > 5000 else "")
                    direct_context += f"\n--- [{_sid(name, 'document')}] {name} ---\n{content}\n"
            except Exception as e:
                logger.warning("Failed to load document for TZ", document_id=did, error=str(e))

    # Формируем расширенный контекст данных
    data_context = ""
    if insights_context:
        data_context += insights_context
    if sources_context:
        data_context += sources_context
    if direct_context:
        data_context += direct_context

    data_section = ""
    if data_context:
        data_section = f"""
ДАННЫЕ ИССЛЕДОВАНИЙ (встречи, документы, импортированные источники):
{data_context}

ВАЖНО: Используй данные из исследований для обоснования требований, описания потребностей пользователей,
формулирования критериев приёмки и определения приоритетов. Ссылайся на конкретные инсайты и цитаты."""

    prompt = f"""Сгенерируй полное Техническое Задание (ТЗ) в Markdown.

ПРОЕКТ: {project.get('name')}
Описание: {project.get('description') or 'нет'}
Цель: {project.get('goal') or 'нет'}
Аудитория: {project.get('targetAudience') or project.get('target_audience') or 'не указана'}

БЛОКИ: {blocks_detailed}
СВЯЗИ: {connections_detailed}
РЕШЕНИЯ: {decisions_text}
{data_section}
Структура ТЗ:
1. О продукте (цели, аудитория, проблемы которые решаем)
2. Как работает (пайплайн)
3. Модули — для КАЖДОГО блока подробно
4. Связи между модулями
5. Ключевые решения
6. Требования (функциональные и нефункциональные){' — на основе данных из встреч и документов' if data_context else ''}
7. Карта развития (MVP → Средний → Идеальный)

НЕ СОКРАЩАЙ. Пиши подробно, но СТРОГО по спроектированной схеме: описывай
блоки, связи и решения, которые реально есть выше. Не выдумывай модули,
требования, технологии и фичи, которых нет в схеме/данных. Где информации
для раздела не хватает — отметь «требует уточнения», а не додумывай.

ТРАССИРОВКА ИСТОЧНИКОВ: у источников выше есть метки [s1], [s2]… Если факты
раздела опираются на конкретные источники — добавь ПОСЛЕДНЕЙ строкой раздела:
`источники: s1, s3` (только реально использованные id; ничего не выдумывай).
Раздел построен только по схеме блоков — строку не добавляй вовсе."""

    try:
        result = await router.generate(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.5,
        )
        if result and source_registry:
            # реестр для чипов трассировки в бумажном виде ТЗ (не отображается)
            result += ("\n\n<!-- sima:sources "
                       + json.dumps(source_registry, ensure_ascii=False)
                       + " -->")
        return result or "Не удалось сгенерировать ТЗ"
    except Exception as e:
        logger.error("TZ generation error", error=str(e))
        return f"Ошибка при генерации ТЗ: {e!s}"


async def parse_tz_to_schema(project_id: str, tz_text: str, user_id: str) -> dict:
    """Парсинг готового ТЗ в схему блоков и связей."""
    router = get_llm_router()

    try:
        result = await router.generate_json(
            prompt=f"Вот ТЗ для анализа:\n\n{tz_text}",
            system_prompt=PARSE_TZ_PROMPT,
            task_type=TaskType.EXTRACTION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.5,
        )

        if isinstance(result, dict):
            raw_actions = result.get("actions", [])
            # LLM может вернуть actions как строку JSON
            if isinstance(raw_actions, str):
                try:
                    raw_actions = json.loads(raw_actions)
                except (json.JSONDecodeError, TypeError):
                    raw_actions = []
            if not isinstance(raw_actions, list):
                raw_actions = []
            logger.info("parse_tz_to_schema result", actions_count=len(raw_actions), message_preview=str(result.get("message", ""))[:100])
            return {
                "message": result.get("message", "ТЗ проанализировано."),
                "actions": raw_actions,
                "projectUpdate": result.get("projectUpdate"),
            }
        return {"message": "ТЗ проанализировано.", "actions": []}
    except Exception as e:
        logger.error("Parse TZ error", error=str(e))
        return {"message": f"Ошибка при анализе ТЗ: {e!s}", "actions": []}


async def seed_project_from_spec(
    user_id: str,
    tz_text: str,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """Мост «звено D»: сырое ТЗ → НАПОЛНЕННЫЙ проект SIMA за один вызов.

    Раньше этой функции не было: `create_project` даёт ПУСТОЙ проект, а
    `parse_tz_to_schema`/`tz-to-schema` требуют уже существующий projectId.
    Поэтому Tessbrain-сторона (заметил потребность → сформировал ТЗ) не могла
    программно завести продукт в SIMA — мост был 100% ручным (человек
    открывал SIMA и вставлял текст). Здесь: создаём проект → сохраняем ТЗ →
    раскладываем его в блоки/связи. Переиспользует существующие функции,
    ничего не дублируя.

    Возвращает {project, actions, message}. Честно: пустое ТЗ → без выдумки
    (проект не создаём, отдаём ошибку); если LLM не выделил блоков — проект
    всё равно создан с сохранённым ТЗ (его можно доработать вручную).
    """
    spec = (tz_text or "").strip()
    if not spec:
        return {"error": "tz_text is required (пустое ТЗ — нечего раскладывать)"}

    db = await get_sima_db()
    project = await db.create_project(
        user_id,
        {
            "name": (title or "").strip() or "Проект из ТЗ",
            "description": (description or "").strip() or None,
        },
    )
    project_id = str(project.get("id") or "")
    if not project_id:
        return {"error": "Не удалось создать проект"}

    # Сохраняем исходное ТЗ в проект (источник правды; переживает ре-парс).
    await db.update_project(project_id, user_id, {"generatedTZ": spec})

    # ТЗ → действия над схемой → блоки/связи в БД.
    parsed = await parse_tz_to_schema(project_id, spec, user_id)
    actions = parsed.get("actions", [])
    if not actions:
        # Проект создан и ТЗ сохранено — просто не из чего собрать блоки.
        fresh = await db.get_project(project_id, user_id) or project
        return {
            "project": fresh,
            "actions": [],
            "message": parsed.get("message", "Проект создан, но блоки из ТЗ не выделены"),
        }

    try:
        updated = await apply_schema_actions(
            project_id, actions, parsed.get("projectUpdate"), user_id
        )
    except Exception as e:
        logger.error("seed_project_from_spec: apply_schema_actions failed", error=str(e))
        updated = await db.get_project(project_id, user_id) or project

    return {
        "project": updated,
        "actions": actions,
        "message": parsed.get("message", f"Проект создан из ТЗ: {len(actions)} элементов"),
    }


_ACCEPTANCE_SYS = (
    "Ты — инженер приёмки Kanon. По описанию блока продукта формулируешь "
    "критерии приёмки. Пользователь — НЕ программист, поэтому проверки "
    "должны быть автоматическими, где это возможно. Форматы детерминированных "
    "проверок (строго такие):\n"
    "  file: <glob-путь>                — файл(ы) существуют\n"
    "  contains: <путь> :: <regex>      — файл содержит паттерн\n"
    "  cmd: <shell-команда>             — команда завершается кодом 0\n"
    "Остальное — обычные фразы (ручная проверка). НИЧЕГО не выдумывай: "
    "file:/contains: — только из указанных полей блока (файлы, API, стек); "
    "cmd: — только если из полей блока очевидна команда теста. Если "
    "технических полей нет — верни ручные критерии понятным языком."
)


async def suggest_acceptance_criteria(project_id: str, block_id: str,
                                      user_id: str) -> dict:
    """Автогенерация проверок приёмки из описания блока (roadmap Kanon;
    ключевая «за ручку»-функция: сотрудник-непрограммист не умеет писать
    file:/contains:/cmd: — SIMA пишет их сама из полей блока).

    Возвращает {"criteria": [строки], "deterministic": N}. Честно: без
    технических полей детерминированных маркеров не выдумываем."""
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        return {"error": "Project not found", "criteria": []}
    block = next((b for b in project.get("blocks", [])
                  if str(b.get("id")) == str(block_id)), None)
    if not block:
        return {"error": "Block not found", "criteria": []}

    def _f(*keys: str) -> str:
        for k in keys:
            v = block.get(k)
            if v:
                return str(v)
        return ""

    ctx = (
        f"Блок «{_f('name')}» (тип {_f('type')}).\n"
        f"Описание: {_f('description')[:800]}\n"
        f"Цель: {_f('goal')[:400]}\n"
        f"Выход: {_f('outputData', 'output_data')[:400]}\n"
        f"Сценарий: {_f('userFlow', 'user_flow')[:400]}\n"
        f"Файлы: {_f('filesToCreate', 'files_to_create')[:400]}\n"
        f"API: {_f('apiEndpoints', 'api_endpoints')[:300]}\n"
        f"Стек: {_f('blockTechStack', 'block_tech_stack')[:200]}\n"
        f"Текущие критерии: {_f('acceptanceCriteria', 'acceptance_criteria')[:400]}"
    )
    router = get_llm_router()
    try:
        res = await router.generate_json(
            prompt=(f"{ctx}\n\nСформулируй 3–6 критериев приёмки блока. "
                    "Верни строгий JSON {\"criteria\": [\"...\"]}. Сначала "
                    "детерминированные (file:/contains:/cmd:, только из "
                    "полей выше), затем 1–2 ручных понятным языком."),
            system_prompt=_ACCEPTANCE_SYS,
            task_type=TaskType.EXTRACTION,
            model_tier=ModelTier.STANDARD,
            temperature=0.3,
        )
    except Exception as e:
        logger.error("suggest_acceptance failed", error=str(e))
        return {"error": f"LLM недоступна: {e!s}", "criteria": []}

    raw = res.get("criteria") if isinstance(res, dict) else (res if isinstance(res, list) else [])
    criteria = [str(c).strip() for c in (raw or []) if str(c).strip()][:8]
    det = sum(1 for c in criteria
              if c.lower().startswith(("file:", "contains:", "cmd:")))
    return {"criteria": criteria, "deterministic": det}


def _get_block_position(data: dict, index: int) -> tuple[float, float]:
    """Извлечь позицию блока из различных форматов AI-ответа."""
    # Вариант 1: position как объект {x, y}
    pos = data.get("position")
    if isinstance(pos, dict):
        return (pos.get("x", 100 + (index % 4) * 300), pos.get("y", 200 + (index // 4) * 200))
    # Вариант 2: positionX / positionY
    px = data.get("positionX") or data.get("x")
    py = data.get("positionY") or data.get("y")
    if px is not None and py is not None:
        return (px, py)
    # Вариант 3: авторасстановка сеткой
    col = index % 4
    row = index // 4
    return (100 + col * 300, 200 + row * 200)


def _get_block_color(block_type: str) -> str:
    """Цвет по типу блока."""
    return {
        "core": "#6366F1", "feature": "#22D3EE", "integration": "#34D399",
        "data": "#FBBF24", "agent": "#A78BFA",
    }.get(block_type, "#6366F1")


async def apply_schema_actions(project_id: str, actions: list | str, project_update: dict | None, user_id: str, parent_block_id: str | None = None) -> dict:
    """Применить actions (CREATE_BLOCK, CREATE_CONNECTION, UPDATE_BLOCK, UPDATE_PROJECT, ADD_DECISION) к проекту в БД.

    Поддерживает различные форматы AI-ответов:
    - Блок: name/title, label/id, position/{x,y}/positionX/positionY
    - Связь: fromLabel/source/from, toLabel/target/to, fromBlockId/toBlockId

    Возвращает обновлённый проект.
    """
    import traceback as tb_module
    db = await get_sima_db()

    logger.info(f"[SIMA apply_schema_actions] CALLED for project={project_id}, user={user_id}, actions type={type(actions).__name__}", flush=True)

    # Если actions — строка (LLM иногда возвращает JSON-строку), распарсим
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
            logger.info(f"[SIMA] Parsed actions from JSON string, got {len(actions)} actions", flush=True)
        except (json.JSONDecodeError, TypeError):
            logger.info(f"[SIMA] ERROR: actions is not valid JSON string: {actions[:200] if actions else ''}", flush=True)
            return await db.get_project(project_id, user_id) or {}

    if not isinstance(actions, list):
        logger.info(f"[SIMA] ERROR: actions is not a list, type={type(actions).__name__}", flush=True)
        return await db.get_project(project_id, user_id) or {}

    logger.info(f"[SIMA] apply_schema_actions START: {len(actions)} actions for project {project_id}", flush=True)

    # Загружаем существующие блоки проекта для маппинга label→id
    # Пробуем с user_id, если не нашли — без user_id (для reapply с dev token)
    existing_project = await db.get_project(project_id, user_id)
    if not existing_project:
        existing_project = await db.get_project(project_id)
    # Используем реальный user_id из проекта (если запрос шёл с dev token)
    real_user_id = str(existing_project.get("userId") or existing_project.get("user_id") or user_id) if existing_project else user_id
    logger.info(f"[SIMA] Real user_id from project: {real_user_id}", flush=True)
    existing_blocks_map: dict[str, str] = {}  # label → real DB id
    if existing_project:
        for b in existing_project.get("blocks", []):
            blabel = str(b.get("label") or "")
            bid = str(b.get("id") or "")
            if blabel and bid:
                existing_blocks_map[blabel] = bid
            bname = str(b.get("name") or "")
            if bname and bid:
                existing_blocks_map[bname] = bid
    logger.info(f"[SIMA] Existing blocks mapping: {list(existing_blocks_map.keys())}", flush=True)

    # Обновляем мета проекта из project_update или из UPDATE_PROJECT action
    if project_update and isinstance(project_update, dict):
        update_data = {}
        for key in ("description", "goal"):
            if project_update.get(key):
                update_data[key] = project_update[key]
        if project_update.get("targetAudience"):
            update_data["targetAudience"] = project_update["targetAudience"]
        if project_update.get("mvpDescription"):
            update_data["mvpDescription"] = project_update["mvpDescription"]
        if project_update.get("idealProduct"):
            update_data["idealProduct"] = project_update["idealProduct"]
        if update_data:
            try:
                await db.update_project(project_id, real_user_id, update_data)
                logger.info(f"[SIMA] Updated project meta: {list(update_data.keys())}", flush=True)
            except Exception as e:
                logger.info(f"[SIMA] WARNING: Failed to update project meta: {e}", flush=True)

    created_blocks: dict[str, str] = {}  # label/id → real DB id (новые блоки)
    # Объединяем с существующими для резолва связей
    all_blocks_map: dict[str, str] = dict(existing_blocks_map)
    block_index = 0

    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            logger.info(f"[SIMA] Skipping non-dict action #{i}: {type(action).__name__}", flush=True)
            continue

        action_type = action.get("type", "")
        data = action.get("data", {})
        if not isinstance(data, dict):
            logger.info(f"[SIMA] Skipping action #{i} ({action_type}) with non-dict data", flush=True)
            continue

        logger.info(f"[SIMA] Processing action #{i}: {action_type}", flush=True)

        if action_type == "UPDATE_PROJECT":
            # Обновляем мета проекта из action
            update_data = {}
            for key in ("description", "goal", "targetAudience", "mvpDescription", "idealProduct"):
                if data.get(key):
                    update_data[key] = data[key]
            if update_data:
                try:
                    await db.update_project(project_id, real_user_id, update_data)
                    logger.info(f"[SIMA] OK Updated project from action: {list(update_data.keys())}", flush=True)
                except Exception as e:
                    logger.info(f"[SIMA] WARN Failed to update project from action: {e}", flush=True)

        elif action_type == "UPDATE_BLOCK":
            block_ref = action.get("blockId") or data.get("blockId") or data.get("id") or data.get("label") or ""
            block_ref = str(block_ref)
            # Ищем реальный ID блока
            real_block_id = str(all_blocks_map.get(block_ref) or created_blocks.get(block_ref) or block_ref)
            logger.info(f"[SIMA] Updating block: ref={block_ref}, real_id={real_block_id[:8] if real_block_id else '?'}", flush=True)
            if real_block_id:
                try:
                    updated = await db.update_block(real_block_id, data)
                    if updated:
                        logger.info(f"[SIMA] OK Block updated: {real_block_id[:8]}", flush=True)
                    else:
                        logger.info(f"[SIMA] WARN Block not found for update: {real_block_id[:8]}", flush=True)
                except Exception as e:
                    logger.info(f"[SIMA] FAIL Failed to update block {block_ref}: {e}", flush=True)

        elif action_type == "CREATE_BLOCK":
            # AI может вернуть name или title
            name = str(data.get("name") or data.get("title") or data.get("label") or "Блок")
            label = str(data.get("label") or data.get("id") or f"B{block_index + 1}")
            # Идемпотентность: если блок с таким label/name уже есть в проекте
            # (включая созданные в этом же прогоне), маппим на него и не дублируем.
            # Это делает reapply-actions безопасным и не плодит дубли, если AI
            # повторно вернёт тот же CREATE_BLOCK.
            existing_id = (all_blocks_map.get(label)
                           or all_blocks_map.get(name)
                           or all_blocks_map.get(str(data.get("id") or "")))
            if existing_id:
                logger.info(f"[SIMA] SKIP Block already exists: label={label}, name={name}, id={existing_id[:8]}", flush=True)
                created_blocks[label] = existing_id
                if name and name != label:
                    created_blocks[name] = existing_id
                ai_id = data.get("id")
                if ai_id and str(ai_id) != label:
                    created_blocks[str(ai_id)] = existing_id
                continue
            block_type = str(data.get("type", "feature"))
            color = str(data.get("color") or _get_block_color(block_type))
            pos_x, pos_y = _get_block_position(data, block_index)

            # Приведём позиции к float (AI может вернуть строки)
            try:
                pos_x = float(pos_x)
            except (ValueError, TypeError):
                pos_x = 100.0 + (block_index % 4) * 300.0
            try:
                pos_y = float(pos_y)
            except (ValueError, TypeError):
                pos_y = 200.0 + (block_index // 4) * 200.0

            block_data = {
                "projectId": project_id,
                "parentBlockId": data.get("parentBlockId") or parent_block_id or None,
                "name": name,
                "label": label,
                "type": block_type,
                "layer": str(data.get("layer", "mvp")),
                "color": color,
                "positionX": pos_x,
                "positionY": pos_y,
                "description": str(data.get("description") or ""),
                "goal": str(data.get("goal") or ""),
            }
            logger.info(f"[SIMA] Creating block #{block_index}: label={label}, name={name}, type={block_type}, pos=({pos_x},{pos_y})", flush=True)
            try:
                created = await db.create_block(block_data)
                real_id = str(created.get("id", ""))
                logger.info(f"[SIMA] OK Block created in DB: label={label}, name={name}, id={real_id[:8]}", flush=True)
                # Маппим все возможные идентификаторы для связей
                if label:
                    created_blocks[label] = real_id
                    all_blocks_map[label] = real_id
                ai_id = data.get("id")
                if ai_id and str(ai_id) != label:
                    created_blocks[str(ai_id)] = real_id
                    all_blocks_map[str(ai_id)] = real_id
                # Также маппим по name на случай если связи ссылаются на название
                if name and name != label:
                    created_blocks[name] = real_id
                    all_blocks_map[name] = real_id
                block_index += 1
            except Exception as e:
                logger.info(f"[SIMA] FAIL Failed to create block: label={label}, name={name}, error={e}", flush=True)
                logger.info(f"[SIMA] Traceback: {tb_module.format_exc()}", flush=True)

        elif action_type == "CREATE_CONNECTION":
            # AI может вернуть разные форматы для source/target
            from_ref = str(
                data.get("fromLabel") or data.get("source") or
                data.get("from") or data.get("fromBlockId") or ""
            )
            to_ref = str(
                data.get("toLabel") or data.get("target") or
                data.get("to") or data.get("toBlockId") or ""
            )
            # Ищем реальные ID: сначала в созданных блоках, потом в существующих
            from_id = str(all_blocks_map.get(from_ref, from_ref))
            to_id = str(all_blocks_map.get(to_ref, to_ref))

            logger.info(f"[SIMA] Creating connection: {from_ref}->{to_ref}, resolved: {from_id[:8] if from_id else '?'}->{to_id[:8] if to_id else '?'}", flush=True)
            logger.info(f"[SIMA]   Available mapping keys: {list(all_blocks_map.keys())}", flush=True)

            if from_id and to_id:
                conn_data = {
                    "projectId": project_id,
                    "fromBlockId": from_id,
                    "toBlockId": to_id,
                    "type": str(data.get("type", "data_flow")),
                    "label": str(data.get("label") or ""),
                    "description": str(data.get("description") or ""),
                    "dataDescription": str(data.get("dataDescription") or ""),
                }
                try:
                    await db.create_connection(conn_data)
                    logger.info(f"[SIMA] OK Connection created: {from_ref}->{to_ref}", flush=True)
                except Exception as e:
                    logger.info(f"[SIMA] FAIL Failed to create connection: {from_ref}->{to_ref}, error={e}", flush=True)
                    logger.info(f"[SIMA] Connection IDs: from={from_id}, to={to_id}", flush=True)
                    logger.info(f"[SIMA] Traceback: {tb_module.format_exc()}", flush=True)
            else:
                logger.info(f"[SIMA] WARN Skipped connection - missing refs: from={from_ref}, to={to_ref}", flush=True)

        elif action_type == "ADD_DECISION":
            # Добавляем решение
            title = str(data.get("title") or "Решение")
            description = str(data.get("description") or "")
            reasoning = str(data.get("reasoning") or "")
            decision_data = {
                "projectId": project_id,
                "title": title,
                "description": description,
                "reasoning": reasoning,
                "status": "accepted",
            }
            try:
                await db.create_decision(decision_data)
                logger.info(f"[SIMA] OK Decision added: {title}", flush=True)
            except Exception as e:
                logger.info(f"[SIMA] WARN Failed to add decision: {e}", flush=True)

        else:
            logger.info(f"[SIMA] Unknown action type: {action_type}", flush=True)

    logger.info(f"[SIMA] apply_schema_actions DONE: {len(created_blocks)} blocks created, project={project_id}", flush=True)

    # Вернём обновлённый проект
    project = await db.get_project(project_id, real_user_id)
    if not project:
        project = await db.get_project(project_id)
    return project or {}


async def brainstorm_idea(
    project_id: str,
    user_message: str,
    user_id: str,
    should_build: bool = False,
) -> dict:
    """Режим brainstorm — обсуждение идеи или построение схемы."""
    db = await get_sima_db()
    router = get_llm_router()

    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    blocks = project.get("blocks", [])
    project_context = ""
    if blocks:
        block_strs = [f'{b.get("label")} "{b.get("name")}" ({b.get("type")})' for b in blocks]
        project_context = f"\nТЕКУЩИЕ БЛОКИ: {', '.join(block_strs)}"
    else:
        project_context = "\nПроект пустой."

    insights_context = await _get_insights_context(db, project_id)

    # История сообщений
    messages = await db.get_messages(project_id)
    [{"role": m["role"], "content": m["content"]} for m in messages[-20:]]

    system_prompt = BRAINSTORM_BUILD_PROMPT if should_build else BRAINSTORM_PROMPT

    try:
        result = await router.generate_json(
            prompt=user_message,
            system_prompt=system_prompt + project_context + insights_context,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.5 if should_build else 0.8,
        )

        if isinstance(result, dict):
            return {
                "message": result.get("message", "Обработано."),
                "actions": result.get("actions", []),
                "projectUpdate": result.get("projectUpdate"),
                "proposal": result.get("proposal"),
            }
        return {"message": "Обработано.", "actions": []}
    except Exception as e:
        logger.error("Brainstorm error", error=str(e))
        return {"message": f"Ошибка: {e!s}", "actions": []}


async def deep_interview(
    project_id: str,
    user_message: str,
    user_id: str,
) -> dict:
    """Режим глубокого интервью."""
    db = await get_sima_db()
    router = get_llm_router()

    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    messages = await db.get_messages(project_id)
    chat_history = [{"role": m["role"], "content": m["content"]} for m in messages[-20:]]

    user_messages = [m for m in chat_history if m["role"] == "user"]
    is_first = len(user_messages) == 0
    system_prompt = DEEP_INTERVIEW_PROMPT if is_first else DEEP_INTERVIEW_FOLLOWUP_PROMPT

    blocks = project.get("blocks", [])
    block_context = ""
    if blocks:
        block_strs = [f'{b.get("label")} "{b.get("name")}"' for b in blocks]
        block_context = f"\nТЕКУЩИЕ БЛОКИ: {', '.join(block_strs)}"

    insights_context = await _get_insights_context(db, project_id)

    try:
        result = await router.generate_json(
            prompt=user_message,
            system_prompt=system_prompt + block_context + insights_context,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
        )

        if isinstance(result, dict):
            return {
                "message": result.get("message", "Вопрос обработан."),
                "actions": result.get("actions", []),
                "interviewQuestions": result.get("interviewQuestions"),
                "readyToBuild": result.get("readyToBuild"),
            }
        return {"message": "Обработано.", "actions": []}
    except Exception as e:
        logger.error("Deep interview error", error=str(e))
        return {"message": f"Ошибка: {e!s}", "actions": []}


async def generate_connection_description(
    from_block: dict,
    to_block: dict,
    project: dict | None = None,
    all_blocks: list[dict] | None = None,
    sections: list[str] | None = None,
) -> dict:
    """Автогенерация описания связи между блоками с полным контекстом проекта."""
    router = get_llm_router()

    def _gb(b: dict, *keys: str) -> str:
        for k in keys:
            v = b.get(k)
            if v:
                return str(v)
        return "нет"

    # Контекст проекта
    project_ctx = ""
    if project:
        project_ctx = f"""Проект: {project.get('name', '?')}
Описание: {project.get('description', '')}
Цель: {project.get('goal', '')}
Целевая аудитория: {project.get('targetAudience') or project.get('target_audience') or 'не указана'}
"""
    # Другие блоки
    blocks_ctx = ""
    if all_blocks:
        b_list = [f'  - {b.get("label","?")} "{b.get("name","?")}" ({b.get("type","?")}): {b.get("description","")[:100]}' for b in all_blocks]
        blocks_ctx = "\nДругие блоки системы:\n" + "\n".join(b_list) + "\n"

    # Определяем, какие поля запрошены
    all_connection_fields = ["label", "description", "dataDescription", "dataFormat", "dataExample",
                             "dataVolume", "condition", "errorHandling", "businessMeaning"]
    if sections:
        requested = sections
    else:
        requested = all_connection_fields

    prompt = f"""{project_ctx}Опиши связь между двумя блоками системы:
{blocks_ctx}
=== ИСХОДНЫЙ БЛОК ===
Метка: {from_block.get('label', '?')}
Имя: {from_block.get('name', '?')}
Тип: {from_block.get('type', '?')}
Описание: {_gb(from_block, 'description')}
Цель: {_gb(from_block, 'goal')}
Выходные данные: {_gb(from_block, 'outputData', 'output_data')}
Реализация: {_gb(from_block, 'implementation')}
API endpoints: {_gb(from_block, 'apiEndpoints', 'api_endpoints')}
Модель данных: {_gb(from_block, 'dataModel', 'data_model')}

=== ЦЕЛЕВОЙ БЛОК ===
Метка: {to_block.get('label', '?')}
Имя: {to_block.get('name', '?')}
Тип: {to_block.get('type', '?')}
Описание: {_gb(to_block, 'description')}
Цель: {_gb(to_block, 'goal')}
Входные данные: {_gb(to_block, 'inputData', 'input_data')}
Реализация: {_gb(to_block, 'implementation')}
API endpoints: {_gb(to_block, 'apiEndpoints', 'api_endpoints')}
Модель данных: {_gb(to_block, 'dataModel', 'data_model')}

Заполни подробно следующие поля связи: {', '.join(requested)}

Описание каждого поля:
- label: Краткая подпись на стрелке (3-5 слов)
- description: Полное описание связи — что она делает, зачем нужна
- dataDescription: Какие конкретно данные передаются от одного блока к другому
- dataFormat: Формат передачи (JSON, REST, gRPC, WebSocket, Event, Internal и т.д.)
- dataExample: Пример передаваемых данных (мини-JSON/текст)
- dataVolume: Ожидаемый объём данных (например: "~1KB на запрос", "до 100 записей")
- condition: При каком условии происходит передача данных
- errorHandling: Что происходит при ошибке (retry, fallback, уведомление)
- businessMeaning: Зачем эта связь нужна с точки зрения бизнеса/пользователя

Ответь JSON объектом с запрошенными ключами. Значения — строки с подробным описанием.
ТОЛЬКО JSON."""

    try:
        result = await router.generate_json(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
        )
        return result if isinstance(result, dict) else {}
    except Exception as e:
        logger.error("Connection description error", error=str(e))
        return {}


# Поля карты продукта, которые Сима умеет заполнить по данным проекта.
# key → инструкция (что это за поле, как писать). Только эти поля разрешены
# к AI-заполнению из вида «Поля» (Sima Remix, слой 2).
_FILLABLE_FIELDS: dict[str, str] = {
    "description": "миссия/суть продукта одним-двумя предложениями",
    "goal": "главная цель продукта (чего достигаем)",
    "targetAudience": "целевая аудитория (для кого продукт)",
    "mvpDescription": "минимальная ценная версия (MVP) — что войдёт в первую версию",
    "idealProduct": "образ идеального продукта (каким он станет в развитии)",
    "narrativeProblem": "проблема, которую решает продукт",
    "narrativeSolution": "как продукт решает эту проблему",
    "narrativeHowItWorks": "как продукт работает по шагам",
}


async def fill_project_field(project_id: str, field: str, user_id: str) -> dict:
    """Заполнить ОДНО поле карты продукта по данным проекта (блоки + инсайты
    источников). Возвращает {field, value} или {error}. Never-raise: ошибка
    LLM → {error}, а не исключение. Только поля из _FILLABLE_FIELDS."""
    if field not in _FILLABLE_FIELDS:
        return {"error": f"поле {field} не заполняется автоматически"}
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        return {"error": "Проект не найден"}
    blocks = project.get("blocks", [])
    connections = project.get("connections", [])
    project_context = _build_project_context(blocks, connections)
    insights_context = await _get_insights_context(db, project_id)
    instruction = _FILLABLE_FIELDS[field]
    prompt = (
        f"На основе данных проекта ниже напиши поле карты продукта: "
        f"{instruction}.\n"
        "Пиши по-русски, кратко и по делу (1–4 предложения), ТОЛЬКО факты из "
        "данных (не выдумывай цифры; где данных нет — общая, но осмысленная "
        "формулировка). Верни ТОЛЬКО текст поля, без заголовков и markdown.\n"
        f"{project_context}{insights_context}"
    )
    try:
        router = get_llm_router()
        text = await router.generate(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.6,
            max_tokens=600,
        )
        value = (text or "").strip()
        if not value:
            return {"error": "LLM не вернул текст — попробуйте позже"}
        return {"field": field, "value": value}
    except Exception as e:  # noqa: BLE001
        logger.warning("fill_project_field failed", field=field, error=str(e))
        return {"error": f"не удалось заполнить: {e!s}"}
