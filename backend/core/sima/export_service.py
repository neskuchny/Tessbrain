"""
SIMA Export Service — генерация полных ТЗ для AI-платформ.

Поддерживаемые платформы:
  - cursor     → .cursorrules + AGENTS.md + IMPLEMENTATION_PLAN.md + README.md + docs/ + package.json + tsconfig.json + TZ_DESCRIPTION.md
  - claude-code → CLAUDE.md + IMPLEMENTATION_PLAN.md + TZ_DESCRIPTION.md
  - lovable    → LOVABLE_PROMPT.md + LOVABLE_FOLLOWUP.md
  - github     → README.md + docs/ + .github/workflows/ + package.json + TZ_DESCRIPTION.md

Каждая платформа получает:
  1. Полную спецификацию проекта (ВСЕ данные из СИМА)
  2. Пошаговый план реализации
  3. Критерии проверки на каждом шаге
  4. Описание хорошего и плохого результата
  5. Инструкции по отладке
  6. Цикл самокоррекции
  7. LLM-сгенерированное связное описание ТЗ (TZ_DESCRIPTION.md)
"""
from __future__ import annotations

import json
import re

import structlog

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# Утилиты извлечения данных
# ═══════════════════════════════════════════════════════════════

def _get_field(obj: dict, camel_key: str) -> str:
    """Получить поле из объекта, пробуя camelCase и snake_case варианты."""
    val = obj.get(camel_key)
    if val and str(val).strip():
        return str(val).strip()
    snake = re.sub(r'([A-Z])', r'_\1', camel_key).lower()
    val = obj.get(snake)
    if val and str(val).strip():
        return str(val).strip()
    return ""


def _extract_project_info(project: dict) -> dict:
    """Извлечь все метаданные проекта в единый словарь."""
    return {
        "name": _get_field(project, "name") or "Проект",
        "description": _get_field(project, "description"),
        "goal": _get_field(project, "goal"),
        "audience": _get_field(project, "targetAudience"),
        "mvp": _get_field(project, "mvpDescription"),
        "ideal": _get_field(project, "idealProduct"),
        "narr_problem": _get_field(project, "narrativeProblem"),
        "narr_solution": _get_field(project, "narrativeSolution"),
        "narr_how": _get_field(project, "narrativeHowItWorks"),
        "narr_map": _get_field(project, "narrativeProductMap"),
        "narr_decisions": _get_field(project, "narrativeDecisions"),
    }


def _build_block_index(blocks: list[dict]) -> dict[str, dict]:
    """Построить индекс блоков по ID."""
    idx = {}
    for b in blocks:
        bid = str(b.get("id", ""))
        idx[bid] = b
    return idx


# ═══════════════════════════════════════════════════════════════
# Сериализация блоков и связей
# ═══════════════════════════════════════════════════════════════

BLOCK_FIELD_MAP = [
    ("Описание", "description"),
    ("Цель", "goal"),
    ("Бизнес-ценность", "businessValue"),
    ("Польза пользователю", "userBenefit"),
    ("Какую проблему решает", "problemSolved"),
    ("Что будет если убрать", "impactIfRemoved"),
    ("Ключевые метрики", "metrics"),
    ("Входные данные", "inputData"),
    ("Выходные данные", "outputData"),
    ("Критерии приёмки", "acceptanceCriteria"),
    ("Пользовательский сценарий", "userFlow"),
    ("Граничные случаи", "edgeCases"),
    ("Детали реализации", "implementation"),
    ("Технологический стек", "techStack"),
    ("API endpoints", "apiEndpoints"),
    ("Модель данных", "dataModel"),
    ("Файлы для создания", "filesToCreate"),
    ("Технические зависимости", "technicalDeps"),
    ("Риски", "risks"),
    ("Альтернативы", "alternatives"),
    ("Аналоги на рынке", "analogues"),
    ("Нарратив блока", "blockNarrative"),
    ("Будущее развитие", "futureEvolution"),
    ("Известные проблемы", "problems"),
    ("Оценка сложности", "estimatedComplexity"),
]

CONN_FIELD_MAP = [
    ("Подпись", "label"),
    ("Тип", "type"),
    ("Описание", "description"),
    ("Передаваемые данные", "dataDescription"),
    ("Формат данных", "dataFormat"),
    ("Пример данных", "dataExample"),
    ("Объём данных", "dataVolume"),
    ("Условие срабатывания", "condition"),
    ("Обработка ошибок", "errorHandling"),
    ("Бизнес-смысл", "businessMeaning"),
]


def _serialize_block_full(b: dict, block_idx: dict[str, dict]) -> str:
    """Полная сериализация блока со ВСЕМИ полями."""
    parent_id = _get_field(b, "parentBlockId")
    parent_info = ""
    if parent_id:
        pb = block_idx.get(str(parent_id), {})
        parent_info = f"\n  Вложен в: [{_get_field(pb, 'label') or '?'}] {_get_field(pb, 'name') or '?'}"

    label = _get_field(b, "label") or "?"
    name = _get_field(b, "name") or "?"
    btype = _get_field(b, "type") or "?"
    layer = _get_field(b, "layer") or "?"

    lines = [f"### [{label}] {name}", f"  Тип: {btype} | Слой: {layer}"]
    if parent_info:
        lines.append(parent_info)

    for field_label, key in BLOCK_FIELD_MAP:
        val = _get_field(b, key)
        if val:
            lines.append(f"  {field_label}: {val}")
    return "\n".join(lines)


def _serialize_connection_full(c: dict, block_idx: dict[str, dict]) -> str:
    """Полная сериализация связи."""
    from_id = str(_get_field(c, "fromBlockId") or "")
    to_id = str(_get_field(c, "toBlockId") or "")
    fb = block_idx.get(from_id, {})
    tb = block_idx.get(to_id, {})
    from_name = f'[{_get_field(fb, "label") or "?"}] {_get_field(fb, "name") or "?"}'
    to_name = f'[{_get_field(tb, "label") or "?"}] {_get_field(tb, "name") or "?"}'

    parts = [f"  {from_name} --> {to_name}"]
    for fl, key in CONN_FIELD_MAP:
        val = _get_field(c, key)
        if val:
            parts.append(f"    {fl}: {val}")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# Общие строительные блоки для генерации
# ═══════════════════════════════════════════════════════════════

def _sorted_blocks_by_priority(blocks: list[dict]) -> list[dict]:
    """Сортировка блоков по приоритету реализации (слой + зависимости)."""
    layer_order = {"problem": 1, "solution": 2, "mvp": 3, "medium": 4, "ideal": 5}
    return sorted(blocks, key=lambda x: layer_order.get(_get_field(x, "layer"), 3))


def _build_narrative_section(info: dict) -> str:
    """Построить секцию нарратива."""
    parts = []
    if info["narr_problem"]:
        parts.append(f"### Проблема\n{info['narr_problem']}")
    if info["narr_solution"]:
        parts.append(f"### Решение\n{info['narr_solution']}")
    if info["narr_how"]:
        parts.append(f"### Как это работает\n{info['narr_how']}")
    if info["narr_map"]:
        parts.append(f"### Карта продукта\n{info['narr_map']}")
    if info["narr_decisions"]:
        parts.append(f"### Ключевые решения\n{info['narr_decisions']}")
    return "\n\n".join(parts) if parts else ""


def _build_project_header(info: dict) -> str:
    """Построить заголовок с общей информацией о проекте."""
    lines = [f"**Название:** {info['name']}"]
    if info["description"]:
        lines.append(f"**Описание:** {info['description']}")
    if info["goal"]:
        lines.append(f"**Цель:** {info['goal']}")
    if info["audience"]:
        lines.append(f"**Целевая аудитория:** {info['audience']}")
    if info["mvp"]:
        lines.append(f"**MVP:** {info['mvp']}")
    if info["ideal"]:
        lines.append(f"**Идеальный продукт:** {info['ideal']}")
    return "\n".join(lines)


def _build_file_tree(blocks: list[dict]) -> str:
    """Построить дерево файлов из блоков."""
    lines = ["```", "src/"]
    for b in blocks:
        nm = (_get_field(b, "name") or "module").lower()
        # Убираем метку типа "M1: " или "A1: " из начала имени
        nm = re.sub(r'^[a-z]\d+:\s*', '', nm)
        nm = nm.strip().replace(" ", "-").replace("/", "-")
        if not nm:
            nm = "module"
        ftc = _get_field(b, "filesToCreate")
        lines.append(f"  {nm}/")
        if ftc:
            for fline in ftc.split("\n"):
                fline = fline.strip().lstrip("-").strip()
                if fline:
                    lines.append(f"    {fline}")
        else:
            lines.append("    index.ts")
    lines.append("package.json")
    lines.append("tsconfig.json")
    lines.append("README.md")
    lines.append("```")
    return "\n".join(lines)


def _build_connections_for_block(block_id: str, connections: list[dict], block_idx: dict) -> list[str]:
    """Найти все связи блока (входящие и исходящие)."""
    result = []
    for c in connections:
        from_id = str(_get_field(c, "fromBlockId") or "")
        to_id = str(_get_field(c, "toBlockId") or "")
        if from_id == block_id:
            tb = block_idx.get(to_id, {})
            desc = _get_field(c, "label") or _get_field(c, "description") or "данные"
            result.append(f"→ [{_get_field(tb, 'label')}] {_get_field(tb, 'name')}: {desc}")
        elif to_id == block_id:
            fb = block_idx.get(from_id, {})
            desc = _get_field(c, "label") or _get_field(c, "description") or "данные"
            result.append(f"← [{_get_field(fb, 'label')}] {_get_field(fb, 'name')}: {desc}")
    return result


# ═══════════════════════════════════════════════════════════════
# IMPLEMENTATION PLAN — общий для всех платформ
# ═══════════════════════════════════════════════════════════════

def _build_implementation_plan(
    info: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
    block_idx: dict[str, dict],
    platform: str = "cursor",
) -> str:
    """Генерация пошагового плана реализации с проверками, тестами и отладкой."""
    sorted_blocks = _sorted_blocks_by_priority(blocks)

    lines = [
        f"# {info['name']} — Пошаговый план реализации",
        "",
        f"> Платформа: **{platform}**",
        f"> Модулей: {len(blocks)} | Связей: {len(connections)}",
        "",
        "---",
        "",
        "## Общие правила работы",
        "",
        "1. **Выполняй шаги строго последовательно** — каждый следующий шаг зависит от предыдущего",
        "2. **После каждого шага — проверяй** результат по указанным критериям",
        "3. **Если проверка не пройдена** — исправь проблему ПЕРЕД переходом к следующему шагу",
        "4. **Не пропускай шаги** — даже если кажется, что шаг тривиальный",
        "5. **Фиксируй что сделано** — после каждого шага отмечай статус",
        "",
        "---",
        "",
    ]

    # ШАГ 0: Инициализация проекта
    lines.extend([
        "## Шаг 0: Инициализация проекта",
        "",
        "### Что сделать:",
        "1. Создай корневую структуру проекта",
        "2. Инициализируй `package.json` с зависимостями",
        "3. Настрой `tsconfig.json`",
        "4. Создай базовую файловую структуру:",
        "",
        _build_file_tree(blocks),
        "",
        "### Проверка:",
        "- [ ] `npm install` завершается без ошибок",
        "- [ ] `npx tsc --noEmit` завершается без ошибок",
        "- [ ] Все директории модулей созданы",
        "",
        "### Хороший результат:",
        "Проект компилируется, все зависимости установлены, структура папок соответствует спецификации.",
        "",
        "### Плохой результат:",
        "- `npm install` падает → проверь версии зависимостей в package.json",
        "- TypeScript ошибки → проверь tsconfig.json, включи strict: true",
        "",
        "### Отладка:",
        "- Если `npm install` падает: удали `node_modules` и `package-lock.json`, запусти заново",
        "- Если TypeScript ругается: проверь что `\"moduleResolution\": \"node\"` в tsconfig",
        "",
        "---",
        "",
    ])

    # ШАГ N: Для каждого блока
    for step_num, b in enumerate(sorted_blocks, 1):
        label = _get_field(b, "label") or "?"
        name = _get_field(b, "name") or "?"
        desc = _get_field(b, "description") or "нет описания"
        goal = _get_field(b, "goal") or ""
        btype = _get_field(b, "type") or "?"
        layer = _get_field(b, "layer") or "?"
        impl = _get_field(b, "implementation") or ""
        tech = _get_field(b, "techStack") or ""
        api = _get_field(b, "apiEndpoints") or ""
        data_model = _get_field(b, "dataModel") or ""
        files = _get_field(b, "filesToCreate") or ""
        deps = _get_field(b, "technicalDeps") or ""
        input_data = _get_field(b, "inputData") or ""
        output_data = _get_field(b, "outputData") or ""
        criteria = _get_field(b, "acceptanceCriteria") or ""
        user_flow = _get_field(b, "userFlow") or ""
        edge_cases = _get_field(b, "edgeCases") or ""
        risks = _get_field(b, "risks") or ""
        problems = _get_field(b, "problems") or ""
        complexity = _get_field(b, "estimatedComplexity") or "medium"
        biz_value = _get_field(b, "businessValue") or ""
        metrics = _get_field(b, "metrics") or ""

        bid = str(b.get("id", ""))
        block_conns = _build_connections_for_block(bid, connections, block_idx)

        lines.extend([
            f"## Шаг {step_num}: [{label}] {name}",
            f"> Тип: {btype} | Слой: {layer} | Сложность: {complexity}",
            "",
        ])

        problem_solved = _get_field(b, "problemSolved") or ""
        user_benefit = _get_field(b, "userBenefit") or ""
        impact = _get_field(b, "impactIfRemoved") or ""

        # Что сделать
        lines.append("### Что сделать:")
        lines.append(f"Реализуй модуль **{name}**.")
        if desc and desc != "нет описания":
            lines.append(f"\n**Описание:** {desc}")
        if goal:
            lines.append(f"\n**Цель модуля:** {goal}")
        if problem_solved:
            lines.append(f"\n**Какую проблему решает:** {problem_solved}")
        if biz_value:
            lines.append(f"\n**Бизнес-ценность:** {biz_value}")
        if user_benefit:
            lines.append(f"\n**Польза пользователю:** {user_benefit}")
        if impact:
            lines.append(f"\n**Что будет если убрать:** {impact}")
        lines.append("")

        # Детали реализации
        if impl or tech or api or data_model:
            lines.append("### Техническая реализация:")
            if impl:
                lines.append(f"**Детали:** {impl}")
            if tech:
                lines.append(f"**Технологии:** {tech}")
            if api:
                lines.append(f"**API endpoints:** {api}")
            if data_model:
                lines.append(f"**Модель данных:** {data_model}")
            lines.append("")

        # Файлы
        if files:
            lines.append("### Файлы для создания:")
            for fline in files.split("\n"):
                fline = fline.strip().lstrip("-").strip()
                if fline:
                    lines.append(f"- `{fline}`")
            lines.append("")

        # Зависимости
        if deps:
            lines.append(f"### Зависимости: {deps}")
            lines.append("")

        # Входные/выходные данные
        if input_data or output_data:
            lines.append("### Данные:")
            if input_data:
                lines.append(f"**Входные данные:** {input_data}")
            if output_data:
                lines.append(f"**Выходные данные:** {output_data}")
            lines.append("")

        # Связи с другими модулями
        if block_conns:
            lines.append("### Связи с другими модулями:")
            for conn_desc in block_conns:
                lines.append(f"- {conn_desc}")
            lines.append("")

        # Пользовательский сценарий
        if user_flow:
            lines.append(f"### Пользовательский сценарий:\n{user_flow}")
            lines.append("")

        # Проверка
        lines.append("### Проверка после реализации:")
        if criteria:
            lines.append(f"**Критерии приёмки:** {criteria}")
            lines.append("")
        lines.append("- [ ] Модуль компилируется без ошибок (`npx tsc --noEmit`)")
        if api:
            lines.append("- [ ] API endpoints отвечают корректно")
        if input_data:
            lines.append(f"- [ ] Модуль корректно обрабатывает входные данные: {input_data[:100]}")
        if output_data:
            lines.append(f"- [ ] Модуль возвращает ожидаемый результат: {output_data[:100]}")
        if metrics:
            lines.append(f"- [ ] Метрики: {metrics}")
        lines.append("- [ ] Нет console.error в логах")
        lines.append("")

        # Хороший/плохой результат
        lines.append("### Хороший результат:")
        lines.append(f"Модуль [{label}] {name} полностью работает, все связи с другими модулями функционируют.")
        if output_data:
            lines.append(f"На выходе: {output_data[:200]}")
        lines.append("")

        lines.append("### Плохой результат и отладка:")
        if edge_cases:
            lines.append(f"**Граничные случаи:** {edge_cases}")
        if risks:
            lines.append(f"**Известные риски:** {risks}")
        if problems:
            lines.append(f"**Известные проблемы:** {problems}")
        lines.append("- Если модуль не компилируется → проверь импорты и типы")
        lines.append("- Если данные не приходят → проверь связи с другими модулями")
        lines.append("- Если результат некорректный → добавь логирование на входе и выходе")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ФИНАЛЬНАЯ ПРОВЕРКА
    lines.extend([
        "## Финальная проверка всей системы",
        "",
        "После реализации ВСЕХ модулей выполни комплексную проверку:",
        "",
        "### 1. Компиляция",
        "```bash",
        "npx tsc --noEmit",
        "```",
        "**Ожидание:** 0 ошибок",
        "",
        "### 2. Запуск",
        "```bash",
        "npm start",
        "```",
        "**Ожидание:** приложение запускается без ошибок",
        "",
        "### 3. Проверка связей между модулями",
    ])

    for c in connections:
        from_id = str(_get_field(c, "fromBlockId") or "")
        to_id = str(_get_field(c, "toBlockId") or "")
        fb = block_idx.get(from_id, {})
        tb = block_idx.get(to_id, {})
        clabel = _get_field(c, "label") or _get_field(c, "description") or "данные"
        data_desc = _get_field(c, "dataDescription") or ""
        lines.append(
            f"- [ ] [{_get_field(fb, 'label')}] {_get_field(fb, 'name')} → "
            f"[{_get_field(tb, 'label')}] {_get_field(tb, 'name')}: {clabel}"
        )
        if data_desc:
            lines.append(f"  Данные: {data_desc}")

    lines.extend([
        "",
        "### 4. Тестирование",
        "```bash",
        "npm test",
        "```",
        "**Ожидание:** все тесты проходят",
        "",
        "### 5. Проверка по бизнес-целям",
    ])

    if info["goal"]:
        lines.append(f"- [ ] Цель проекта достигнута: {info['goal']}")
    for b in blocks:
        bv = _get_field(b, "businessValue")
        if bv:
            lines.append(f"- [ ] [{_get_field(b, 'label')}] Бизнес-ценность: {bv[:120]}")

    lines.extend([
        "",
        "---",
        "",
        "## Цикл самокоррекции",
        "",
        "Если финальная проверка не прошла, выполни следующее:",
        "",
        "1. **Определи проблемный модуль** — какой шаг/модуль вызывает ошибку?",
        "2. **Прочитай логи ошибок** — что именно сломалось?",
        "3. **Проверь связи** — правильно ли модуль получает/передаёт данные?",
        "4. **Проверь типы** — совпадают ли входные/выходные типы данных?",
        "5. **Исправь и перепроверь** — запусти проверку конкретного модуля заново",
        "6. **Повтори финальную проверку** — убедись, что исправление не сломало другие модули",
        "",
        "### Типичные проблемы:",
        "| Проблема | Причина | Решение |",
        "|----------|---------|---------|",
        "| `Cannot find module` | Неправильный путь импорта | Проверь пути в import, используй relative paths |",
        "| `Type error` | Несовпадение типов | Проверь интерфейсы на входе/выходе модуля |",
        "| `undefined` | Данные не инициализированы | Добавь проверки null/undefined |",
        "| `ECONNREFUSED` | Сервис недоступен | Проверь что все зависимые сервисы запущены |",
        "| `Timeout` | Медленная операция | Увеличь таймаут или оптимизируй |",
        "| Пустой результат | Нет данных на входе | Проверь что предыдущий модуль отработал |",
        "",
    ])

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Полная спецификация — общая для всех платформ
# ═══════════════════════════════════════════════════════════════

def _build_full_spec(
    info: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
    block_idx: dict[str, dict],
) -> str:
    """Построить полную спецификацию проекта (ВСЕ данные из СИМА)."""
    lines = [
        f"# {info['name']} — Полная спецификация системы",
        "",
        "Этот файл содержит ПОЛНУЮ спецификацию проекта, сгенерированную системой СИМА.",
        "При реализации используй ВСЮ информацию ниже: каждый модуль, каждую связь, каждое описание.",
        "",
        "---",
        "",
        "## 1. Общая информация",
        _build_project_header(info),
    ]

    narrative = _build_narrative_section(info)
    if narrative:
        lines.extend(["", "## 2. Нарратив продукта", "", narrative])

    lines.extend(["", f"## 3. Модули системы ({len(blocks)})", ""])
    for b in blocks:
        lines.append(_serialize_block_full(b, block_idx))
        lines.append("")

    lines.extend([f"## 4. Связи между модулями ({len(connections)})", ""])
    if connections:
        for c in connections:
            lines.append(_serialize_connection_full(c, block_idx))
            lines.append("")
    else:
        lines.append("Связи не определены.")
        lines.append("")

    if decisions:
        lines.extend([f"## 5. Архитектурные решения ({len(decisions)})", ""])
        for d in decisions:
            lines.append(f"### {d.get('title', 'Решение')}")
            if d.get("description"):
                lines.append(f"  {d['description']}")
            if d.get("reasoning"):
                lines.append(f"  Обоснование: {d['reasoning']}")
            if d.get("alternatives"):
                lines.append(f"  Альтернативы: {d['alternatives']}")
            lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Вспомогательные генераторы проектных файлов
# ═══════════════════════════════════════════════════════════════

def _slugify_module(name: str) -> str:
    """Превратить имя блока в slug для директории."""
    nm = name.lower()
    nm = re.sub(r'^[a-z]\d+:\s*', '', nm)
    nm = nm.strip().replace(" ", "-").replace("/", "-").replace("_", "-")
    nm = re.sub(r'[^a-z0-9\-]', '', nm)
    nm = re.sub(r'-+', '-', nm).strip('-')
    return nm or "module"


def _camel_case(slug: str) -> str:
    """Превратить slug в camelCase для TypeScript."""
    parts = slug.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _pascal_case(slug: str) -> str:
    """Превратить slug в PascalCase для TypeScript."""
    return "".join(p.capitalize() for p in slug.split("-"))


def _build_cursor_readme(
    info: dict,
    blocks: list[dict],
    connections: list[dict],
    block_idx: dict[str, dict],
) -> str:
    """Генерация README.md для Cursor проекта."""
    lines = [
        f"# {info['name']}",
        "",
    ]
    if info["description"]:
        lines.append(f"> {info['description']}")
        lines.append("")
    if info["goal"]:
        lines.append(f"**Цель:** {info['goal']}")
        lines.append("")

    if info["narr_problem"] or info["narr_solution"]:
        lines.append("## О проекте")
        lines.append("")
        if info["narr_problem"]:
            lines.append(f"### Проблема\n{info['narr_problem']}")
            lines.append("")
        if info["narr_solution"]:
            lines.append(f"### Решение\n{info['narr_solution']}")
            lines.append("")
        if info["narr_how"]:
            lines.append(f"### Как это работает\n{info['narr_how']}")
            lines.append("")

    lines.extend([
        f"## Архитектура ({len(blocks)} модулей, {len(connections)} связей)",
        "",
    ])
    for b in blocks:
        lbl = _get_field(b, "label") or "?"
        nm = _get_field(b, "name") or "?"
        desc = _get_field(b, "description") or ""
        btype = _get_field(b, "type") or ""
        _get_field(b, "layer") or ""
        line = f"- **[{lbl}] {nm}**"
        if btype:
            line += f" ({btype})"
        if desc:
            line += f" — {desc}"
        lines.append(line)
    lines.append("")

    if connections:
        lines.append("### Схема связей")
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph LR")
        for c in connections:
            from_id = str(_get_field(c, "fromBlockId") or "")
            to_id = str(_get_field(c, "toBlockId") or "")
            fb = block_idx.get(from_id, {})
            tb = block_idx.get(to_id, {})
            fn = _get_field(fb, "label") or "?"
            tn = _get_field(tb, "label") or "?"
            clabel = _get_field(c, "label") or ""
            fn_safe = fn.replace(" ", "_")
            tn_safe = tn.replace(" ", "_")
            fn_name = _get_field(fb, "name") or "?"
            tn_name = _get_field(tb, "name") or "?"
            if clabel:
                lines.append(f"    {fn_safe}[{fn_name}] -->|{clabel}| {tn_safe}[{tn_name}]")
            else:
                lines.append(f"    {fn_safe}[{fn_name}] --> {tn_safe}[{tn_name}]")
        lines.append("```")
        lines.append("")

    lines.extend([
        "## Структура проекта",
        "",
        _build_file_tree(blocks),
        "",
        "## Быстрый старт",
        "",
        "```bash",
        "npm install",
        "npm run dev",
        "```",
        "",
        "## Скрипты",
        "",
        "```bash",
        "npm run dev      # Режим разработки (nodemon + ts-node)",
        "npm run build    # Компиляция TypeScript",
        "npm start        # Запуск скомпилированного кода",
        "npm test         # Запуск тестов",
        "npm run lint     # Проверка линтером",
        "```",
        "",
    ])

    if info["audience"]:
        lines.append(f"## Целевая аудитория\n{info['audience']}")
        lines.append("")

    lines.extend([
        "## Документация",
        "",
        "- [Полная спецификация](AGENTS.md) — детальная спецификация каждого модуля",
        "- [Архитектура](docs/ARCHITECTURE.md) — архитектурная документация",
        "- [API](docs/API.md) — API документация",
        "- [План реализации](IMPLEMENTATION_PLAN.md) — пошаговый план с проверками",
        "- [Техническое задание](TZ_DESCRIPTION.md) — связное описание ТЗ",
        "",
        "## Для AI-ассистентов (Cursor)",
        "",
        "Если ты AI-ассистент в Cursor IDE:",
        "1. Прочитай `.cursorrules` — основные правила и архитектура",
        "2. Прочитай `AGENTS.md` — полная спецификация каждого модуля",
        "3. Следуй шагам из `IMPLEMENTATION_PLAN.md` — строго последовательно",
        "",
    ])

    return "\n".join(lines)


def _build_package_json(info: dict, blocks: list[dict]) -> str:
    """Генерация package.json на основе tech stack из блоков."""
    slug = info["name"].lower()
    slug = re.sub(r'[^a-z0-9\-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')

    tech_stacks = []
    for b in blocks:
        ts = _get_field(b, "techStack") or _get_field(b, "blockTechStack") or ""
        if ts:
            tech_stacks.append(ts)
    all_tech = " ".join(tech_stacks).lower()

    deps: dict[str, str] = {}
    dev_deps: dict[str, str] = {
        "typescript": "^5.3.0",
        "@types/node": "^20.10.0",
        "ts-node": "^10.9.0",
        "nodemon": "^3.0.0",
        "jest": "^29.7.0",
        "ts-jest": "^29.1.0",
        "@types/jest": "^29.5.0",
        "eslint": "^8.56.0",
    }

    if "express" in all_tech:
        deps["express"] = "^4.18.0"
        dev_deps["@types/express"] = "^4.17.0"
    if "fastify" in all_tech:
        deps["fastify"] = "^4.25.0"
    if "react" in all_tech:
        deps["react"] = "^18.2.0"
        deps["react-dom"] = "^18.2.0"
        dev_deps["@types/react"] = "^18.2.0"
        dev_deps["@types/react-dom"] = "^18.2.0"
    if "next" in all_tech:
        deps["next"] = "^14.0.0"
    if "prisma" in all_tech:
        deps["@prisma/client"] = "^5.8.0"
        dev_deps["prisma"] = "^5.8.0"
    if "redis" in all_tech:
        deps["redis"] = "^4.6.0"
    if "mongoose" in all_tech or "mongodb" in all_tech:
        deps["mongoose"] = "^8.0.0"
    if "graphql" in all_tech:
        deps["graphql"] = "^16.8.0"
        deps["@apollo/server"] = "^4.10.0"
    if "websocket" in all_tech or "socket" in all_tech:
        deps["socket.io"] = "^4.7.0"
    if "zod" in all_tech:
        deps["zod"] = "^3.22.0"
    if "axios" in all_tech:
        deps["axios"] = "^1.6.0"
    if "winston" in all_tech or "log" in all_tech:
        deps["winston"] = "^3.11.0"

    if not deps:
        deps["dotenv"] = "^16.3.0"

    pkg = {
        "name": slug,
        "version": "0.1.0",
        "description": info["description"] or info["name"],
        "main": "dist/index.js",
        "scripts": {
            "start": "node dist/index.js",
            "dev": "nodemon --exec ts-node src/index.ts",
            "build": "tsc",
            "test": "jest --config jest.config.ts",
            "lint": "eslint src/ --ext .ts",
            "typecheck": "tsc --noEmit",
        },
        "dependencies": dict(sorted(deps.items())),
        "devDependencies": dict(sorted(dev_deps.items())),
    }

    return json.dumps(pkg, indent=2, ensure_ascii=False)


def _build_tsconfig() -> str:
    """Генерация tsconfig.json."""
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "commonjs",
            "lib": ["ES2022"],
            "outDir": "./dist",
            "rootDir": "./src",
            "strict": True,
            "esModuleInterop": True,
            "skipLibCheck": True,
            "forceConsistentCasingInFileNames": True,
            "resolveJsonModule": True,
            "declaration": True,
            "declarationMap": True,
            "sourceMap": True,
            "moduleResolution": "node",
            "baseUrl": ".",
            "paths": {
                "@/*": ["src/*"],
            },
        },
        "include": ["src/**/*"],
        "exclude": ["node_modules", "dist", "**/*.test.ts"],
    }
    return json.dumps(tsconfig, indent=2)


def _build_env_example(info: dict, blocks: list[dict]) -> str:
    """Генерация .env.example."""
    lines = [
        f"# {info['name']} — Environment Variables",
        "",
        "NODE_ENV=development",
        "PORT=3000",
        "LOG_LEVEL=debug",
        "",
    ]
    for b in blocks:
        bt = _get_field(b, "type") or ""
        nm = _get_field(b, "name") or "module"
        slug_nm = re.sub(r'^[a-z]\d+:\s*', '', nm).upper().replace(" ", "_").replace("-", "_")
        slug_nm = re.sub(r'[^A-Z0-9_]', '', slug_nm)

        if bt in ("database", "data", "storage"):
            lines.append(f"# {nm}")
            lines.append("DATABASE_URL=postgresql://user:pass@localhost:5432/db")
            lines.append("")
        elif bt in ("integration", "external"):
            lines.append(f"# {nm}")
            lines.append(f"{slug_nm}_API_KEY=your_api_key_here")
            lines.append(f"{slug_nm}_API_URL=https://api.example.com")
            lines.append("")
        elif bt in ("auth",):
            lines.append(f"# {nm}")
            lines.append("JWT_SECRET=your_secret_here")
            lines.append("SESSION_TTL=3600")
            lines.append("")

    return "\n".join(lines)


def _build_api_doc(info: dict, blocks: list[dict]) -> str:
    """Генерация docs/API.md."""
    lines = [
        f"# {info['name']} — API Документация",
        "",
    ]
    has_api = False
    for b in blocks:
        api = _get_field(b, "apiEndpoints")
        if api:
            has_api = True
            lines.append(f"## [{_get_field(b, 'label')}] {_get_field(b, 'name')}")
            lines.append("")
            lines.append(api)
            lines.append("")
            dm = _get_field(b, "dataModel")
            if dm:
                lines.append(f"### Модель данных\n{dm}")
                lines.append("")
            inp = _get_field(b, "inputData")
            out = _get_field(b, "outputData")
            if inp:
                lines.append(f"### Входные данные\n{inp}")
                lines.append("")
            if out:
                lines.append(f"### Выходные данные\n{out}")
                lines.append("")
    if not has_api:
        lines.append("API endpoints будут определены при реализации модулей.")
        lines.append("")
        lines.append("## Модули без явных API")
        lines.append("")
        for b in blocks:
            nm = _get_field(b, "name") or "?"
            desc = _get_field(b, "description") or ""
            lines.append(f"- **{nm}** — {desc}")
        lines.append("")
    return "\n".join(lines)


def _build_source_scaffold(
    info: dict,
    blocks: list[dict],
    connections: list[dict],
    block_idx: dict[str, dict],
) -> dict[str, str]:
    """Генерация scaffold-файлов src/ для каждого модуля."""
    files: dict[str, str] = {}
    sorted_blocks = _sorted_blocks_by_priority(blocks)

    module_slugs: list[tuple[str, str, dict]] = []
    for b in sorted_blocks:
        nm = _get_field(b, "name") or "module"
        slug = _slugify_module(nm)
        module_slugs.append((slug, nm, b))

    main_lines = [
        "/**",
        f" * {info['name']} — главная точка входа",
        " *",
    ]
    if info["description"]:
        main_lines.append(f" * {info['description']}")
    if info["goal"]:
        main_lines.append(f" * Цель: {info['goal']}")
    main_lines.extend([
        " */",
        "",
    ])

    for slug, nm, b in module_slugs:
        main_lines.append(f"import {{ init as init{_pascal_case(slug)} }} from './{slug}';")

    main_lines.extend([
        "",
        "async function main(): Promise<void> {",
        f"  console.log('[{info['name']}] Запуск...');",
        "",
    ])

    for slug, nm, b in module_slugs:
        main_lines.append(f"  await init{_pascal_case(slug)}();")
        main_lines.append(f"  console.log('  [{_get_field(b, 'label') or '?'}] {nm} — запущен');")

    main_lines.extend([
        "",
        f"  console.log('[{info['name']}] Все модули запущены.');",
        "}",
        "",
        "main().catch((err) => {",
        "  console.error('Критическая ошибка:', err);",
        "  process.exit(1);",
        "});",
        "",
    ])
    files["src/index.ts"] = "\n".join(main_lines)

    for slug, nm, b in module_slugs:
        bid = str(b.get("id", ""))
        label = _get_field(b, "label") or "?"
        desc = _get_field(b, "description") or ""
        btype = _get_field(b, "type") or "module"
        layer = _get_field(b, "layer") or ""
        impl = _get_field(b, "implementation") or ""
        tech = _get_field(b, "techStack") or _get_field(b, "blockTechStack") or ""
        api = _get_field(b, "apiEndpoints") or ""
        data_model = _get_field(b, "dataModel") or ""
        input_data = _get_field(b, "inputData") or ""
        output_data = _get_field(b, "outputData") or ""
        criteria = _get_field(b, "acceptanceCriteria") or ""
        user_flow = _get_field(b, "userFlow") or ""
        biz_value = _get_field(b, "businessValue") or ""
        problem = _get_field(b, "problemSolved") or ""
        ftc = _get_field(b, "filesToCreate") or ""

        block_conns = _build_connections_for_block(bid, connections, block_idx)

        mod_lines = [
            "/**",
            f" * [{label}] {nm}",
            f" * Тип: {btype} | Слой: {layer}",
        ]
        if desc:
            mod_lines.append(" *")
            for dl in desc.split("\n"):
                mod_lines.append(f" * {dl.strip()}")
        if problem:
            mod_lines.append(" *")
            mod_lines.append(f" * Проблема: {problem}")
        if biz_value:
            mod_lines.append(f" * Бизнес-ценность: {biz_value}")
        mod_lines.extend([
            " */",
            "",
        ])

        if block_conns:
            mod_lines.append("// Связи с другими модулями:")
            for cd in block_conns:
                mod_lines.append(f"//   {cd}")
            mod_lines.append("")

        if input_data:
            mod_lines.append(f"// Входные данные: {input_data[:200]}")
        if output_data:
            mod_lines.append(f"// Выходные данные: {output_data[:200]}")
        if input_data or output_data:
            mod_lines.append("")

        if tech:
            mod_lines.append(f"// Технологии: {tech}")
            mod_lines.append("")

        if criteria:
            mod_lines.append(f"// Критерии приёмки: {criteria[:300]}")
            mod_lines.append("")

        pascal = _pascal_case(slug)

        if api:
            mod_lines.append("// API endpoints:")
            for al in api.split("\n"):
                al = al.strip()
                if al:
                    mod_lines.append(f"//   {al}")
            mod_lines.append("")

        if data_model:
            mod_lines.append("// Модель данных:")
            for dl in data_model.split("\n"):
                dl = dl.strip()
                if dl:
                    mod_lines.append(f"//   {dl}")
            mod_lines.append("")

        mod_lines.extend([
            f"export interface {pascal}Config {{",
            "  // TODO: определить конфигурацию модуля на основе спецификации",
        ])
        if impl:
            mod_lines.append(f"  // Детали реализации: {impl[:200]}")
        mod_lines.extend([
            "}",
            "",
            "/**",
            f" * Инициализация модуля [{label}] {nm}.",
        ])
        if user_flow:
            mod_lines.append(f" * Пользовательский сценарий: {user_flow[:200]}")
        mod_lines.extend([
            " */",
            f"export async function init(config?: Partial<{pascal}Config>): Promise<void> {{",
            "  // TODO: Реализовать инициализацию модуля",
            "  // См. AGENTS.md и IMPLEMENTATION_PLAN.md для полной спецификации",
        ])
        if impl:
            for il in impl.split("\n")[:5]:
                il = il.strip()
                if il:
                    mod_lines.append(f"  // {il}")
        mod_lines.extend([
            "}",
            "",
        ])

        if ftc:
            existing_files = []
            for fline in ftc.split("\n"):
                fline = fline.strip().lstrip("-").strip()
                if fline and fline.endswith((".ts", ".tsx", ".js")):
                    existing_files.append(fline)

            if existing_files:
                for ef in existing_files:
                    ef_clean = ef.lstrip("./")
                    ef_path = f"src/{slug}/{ef_clean}"
                    if ef_path not in files and ef_clean != "index.ts":
                        _pascal_case(ef_clean.replace(".ts", "").replace(".tsx", "").replace(".js", ""))
                        files[ef_path] = (
                            f"/**\n"
                            f" * [{label}] {nm} — {ef_clean}\n"
                            f" * TODO: Реализовать согласно спецификации в AGENTS.md\n"
                            f" */\n\n"
                            f"export {{}};\n"
                        )

        files[f"src/{slug}/index.ts"] = "\n".join(mod_lines)

    return files


# ═══════════════════════════════════════════════════════════════
# CURSOR EXPORT
# ═══════════════════════════════════════════════════════════════

def build_cursor_export(
    project: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
) -> dict[str, str]:
    """Генерация файлов для Cursor IDE."""
    info = _extract_project_info(project)
    block_idx = _build_block_index(blocks)

    # --- .cursorrules ---
    cr = [
        f"# {info['name']} — Cursor Rules",
        "",
        "## О проекте",
        _build_project_header(info),
        "",
    ]

    narrative = _build_narrative_section(info)
    if narrative:
        cr.extend(["## Нарратив продукта", "", narrative, ""])

    cr.append(f"## Архитектура: {len(blocks)} модулей, {len(connections)} связей")
    cr.append("")

    CURSORRULES_FIELDS = [
        ("Описание", "description"),
        ("Какую проблему решает", "problemSolved"),
        ("Бизнес-ценность", "businessValue"),
        ("Польза пользователю", "userBenefit"),
        ("Что будет если убрать", "impactIfRemoved"),
        ("Ключевые метрики", "metrics"),
        ("Технологический стек", "techStack"),
        ("Детали реализации", "implementation"),
        ("Входные данные", "inputData"),
        ("Выходные данные", "outputData"),
        ("Критерии приёмки", "acceptanceCriteria"),
    ]

    blocks_by_type: dict[str, list] = {}
    for b in blocks:
        bt = _get_field(b, "type") or "other"
        blocks_by_type.setdefault(bt, []).append(b)

    for btype, blist in blocks_by_type.items():
        cr.append(f"### Блоки типа: {btype}")
        cr.append("")
        for b in blist:
            lbl = _get_field(b, "label") or "?"
            nm = _get_field(b, "name") or "?"
            layer = _get_field(b, "layer") or ""
            complexity = _get_field(b, "estimatedComplexity") or ""
            cr.append(f"#### [{lbl}] {nm}")
            meta_parts = []
            if layer:
                meta_parts.append(f"Слой: {layer}")
            if complexity:
                meta_parts.append(f"Сложность: {complexity}")
            if meta_parts:
                cr.append(f"> {' | '.join(meta_parts)}")
            cr.append("")
            for field_label, key in CURSORRULES_FIELDS:
                val = _get_field(b, key)
                if val:
                    cr.append(f"**{field_label}:** {val}")
                    cr.append("")
            cr.append("---")
            cr.append("")

    if connections:
        cr.append("## Связи между модулями")
        cr.append("")
        for c in connections:
            from_id = str(_get_field(c, "fromBlockId") or "")
            to_id = str(_get_field(c, "toBlockId") or "")
            fb = block_idx.get(from_id, {})
            tb = block_idx.get(to_id, {})
            fn = f'[{_get_field(fb, "label")}] {_get_field(fb, "name")}'
            tn = f'[{_get_field(tb, "label")}] {_get_field(tb, "name")}'
            clabel = _get_field(c, "label") or _get_field(c, "description") or ""
            data_desc = _get_field(c, "dataDescription") or ""
            biz = _get_field(c, "businessMeaning") or ""
            cr.append(f"- **{fn} -> {tn}**" + (f": {clabel}" if clabel else ""))
            if data_desc:
                cr.append(f"  Данные: {data_desc}")
            if biz:
                cr.append(f"  Бизнес-смысл: {biz}")
        cr.append("")

    if decisions:
        cr.append("## Архитектурные решения")
        cr.append("")
        for d in decisions:
            cr.append(f"- **{d.get('title','')}**: {d.get('description','')}")
            if d.get("reasoning"):
                cr.append(f"  Обоснование: {d['reasoning']}")
        cr.append("")

    cr.extend([
        "## Как работать с этим проектом",
        "",
        "1. Прочитай файл AGENTS.md — там полная спецификация каждого модуля",
        "2. Прочитай IMPLEMENTATION_PLAN.md — там пошаговый план с проверками",
        "3. Выполняй шаги из IMPLEMENTATION_PLAN.md строго последовательно",
        "4. После каждого шага проверяй результат по указанным критериям",
        "5. Если проверка не прошла — исправь перед переходом к следующему шагу",
        "",
        "## Соглашения",
        "- Код на TypeScript, строгая типизация",
        "- Каждый модуль — отдельная папка в src/",
        "- Используй данные из AGENTS.md как полную спецификацию",
        "- Реализуй ВСЕ модули и связи между ними",
        "- После реализации — запусти финальную проверку из IMPLEMENTATION_PLAN.md",
    ])

    # --- AGENTS.md ---
    spec = _build_full_spec(info, blocks, connections, decisions, block_idx)

    # --- IMPLEMENTATION_PLAN.md ---
    plan = _build_implementation_plan(info, blocks, connections, decisions, block_idx, "Cursor IDE")

    # --- README.md ---
    readme = _build_cursor_readme(info, blocks, connections, block_idx)

    # --- package.json ---
    pkg_json = _build_package_json(info, blocks)

    # --- tsconfig.json ---
    tsconfig = _build_tsconfig()

    # --- .env.example ---
    env_example = _build_env_example(info, blocks)

    # --- docs/ARCHITECTURE.md ---
    arch = _build_full_spec(info, blocks, connections, decisions, block_idx)

    # --- docs/API.md ---
    api_doc = _build_api_doc(info, blocks)

    files = {
        ".cursorrules": "\n".join(cr),
        "AGENTS.md": spec,
        "IMPLEMENTATION_PLAN.md": plan,
        "README.md": readme,
        "package.json": pkg_json,
        "tsconfig.json": tsconfig,
        ".env.example": env_example,
        "docs/ARCHITECTURE.md": arch,
        "docs/API.md": api_doc,
    }
    return files


# ═══════════════════════════════════════════════════════════════
# CLAUDE CODE EXPORT
# ═══════════════════════════════════════════════════════════════

def build_claude_code_export(
    project: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
) -> dict[str, str]:
    """Генерация файлов для Claude Code CLI."""
    info = _extract_project_info(project)
    block_idx = _build_block_index(blocks)

    # --- CLAUDE.md (главный файл инструкций для Claude Code) ---
    cl = [
        f"# {info['name']} — Инструкции для Claude Code",
        "",
        "Этот файл содержит полную спецификацию и инструкции для реализации проекта.",
        "Claude Code, ты должен выполнить ВСЕ шаги из этого документа.",
        "",
        "---",
        "",
        "## Роль и контекст",
        "",
        "Ты — Senior Full-Stack разработчик. Твоя задача — реализовать проект по спецификации ниже.",
        "Работай аккуратно, проверяй каждый шаг, не пропускай детали.",
        "",
        "---",
        "",
        "## Общая информация о проекте",
        "",
        _build_project_header(info),
        "",
    ]

    narrative = _build_narrative_section(info)
    if narrative:
        cl.extend(["## Нарратив продукта", "", narrative, ""])

    cl.extend([
        "---",
        "",
        "## Полная спецификация модулей",
        "",
    ])

    for b in blocks:
        cl.append(_serialize_block_full(b, block_idx))
        cl.append("")

    cl.extend([
        "## Связи между модулями",
        "",
    ])
    if connections:
        for c in connections:
            cl.append(_serialize_connection_full(c, block_idx))
            cl.append("")

    if decisions:
        cl.extend(["## Архитектурные решения", ""])
        for d in decisions:
            cl.append(f"### {d.get('title', 'Решение')}")
            if d.get("description"):
                cl.append(f"{d['description']}")
            if d.get("reasoning"):
                cl.append(f"Обоснование: {d['reasoning']}")
            cl.append("")

    cl.extend([
        "---",
        "",
        "## Файловая структура проекта",
        "",
        _build_file_tree(blocks),
        "",
        "---",
        "",
        "## Порядок работы",
        "",
        "1. **Инициализируй проект**: создай package.json, tsconfig.json, структуру папок",
        "2. **Реализуй модули последовательно** по плану из IMPLEMENTATION_PLAN.md",
        "3. **После каждого модуля проверяй**: `npx tsc --noEmit`",
        "4. **После всех модулей**: запусти `npm test` и `npm start`",
        "5. **Если есть ошибки** — исправь и проверь заново",
        "",
        "## Правила кода",
        "",
        "- TypeScript, strict mode",
        "- Все функции с типами аргументов и возвращаемого значения",
        "- Обязательная обработка ошибок (try-catch)",
        "- Логирование важных действий",
        "- Каждый модуль экспортирует ясный public API",
        "",
        "## Самопроверка перед завершением",
        "",
        "Перед тем как сказать, что работа завершена, проверь:",
        "- [ ] Все модули реализованы",
        "- [ ] `npx tsc --noEmit` — 0 ошибок",
        "- [ ] `npm start` — приложение запускается",
        "- [ ] Все связи между модулями работают",
        f"- [ ] Цель проекта достигнута: {info['goal']}" if info["goal"] else "",
        "",
    ])

    # --- IMPLEMENTATION_PLAN.md ---
    plan = _build_implementation_plan(info, blocks, connections, decisions, block_idx, "Claude Code CLI")

    return {
        "CLAUDE.md": "\n".join(cl),
        "IMPLEMENTATION_PLAN.md": plan,
    }


# ═══════════════════════════════════════════════════════════════
# LOVABLE EXPORT
# ═══════════════════════════════════════════════════════════════

def build_lovable_export(
    project: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
) -> dict[str, str]:
    """Генерация промптов для Lovable.dev."""
    info = _extract_project_info(project)
    block_idx = _build_block_index(blocks)
    sorted_blocks = _sorted_blocks_by_priority(blocks)

    # --- Основной промпт для Lovable ---
    prompt_lines = [
        f"# Создай приложение: {info['name']}",
        "",
    ]
    if info["description"]:
        prompt_lines.append(f"**Описание:** {info['description']}")
    if info["goal"]:
        prompt_lines.append(f"**Цель:** {info['goal']}")
    if info["audience"]:
        prompt_lines.append(f"**Для кого:** {info['audience']}")
    prompt_lines.append("")

    # Нарратив
    if info["narr_problem"]:
        prompt_lines.append("## Проблема, которую решаем")
        prompt_lines.append(info["narr_problem"])
        prompt_lines.append("")
    if info["narr_solution"]:
        prompt_lines.append("## Наше решение")
        prompt_lines.append(info["narr_solution"])
        prompt_lines.append("")

    # Страницы и компоненты
    prompt_lines.extend([
        "## Страницы и модули приложения",
        "",
    ])

    # Группируем блоки: page, feature, service, etc.
    ui_blocks = [b for b in blocks if _get_field(b, "type") in ("page", "feature", "ui", "component")]
    service_blocks = [b for b in blocks if _get_field(b, "type") in ("service", "api", "integration", "auth", "payment")]
    data_blocks = [b for b in blocks if _get_field(b, "type") in ("database", "data", "storage", "analytics")]
    other_blocks = [b for b in blocks if b not in ui_blocks and b not in service_blocks and b not in data_blocks]

    if ui_blocks:
        prompt_lines.append("### Страницы и UI:")
        for b in ui_blocks:
            prompt_lines.append(f"\n**{_get_field(b, 'name')}** [{_get_field(b, 'label')}]")
            if _get_field(b, "description"):
                prompt_lines.append(f"- Описание: {_get_field(b, 'description')}")
            if _get_field(b, "userFlow"):
                prompt_lines.append(f"- Пользовательский сценарий: {_get_field(b, 'userFlow')}")
            if _get_field(b, "inputData"):
                prompt_lines.append(f"- Входные данные: {_get_field(b, 'inputData')}")
            if _get_field(b, "outputData"):
                prompt_lines.append(f"- Выходные данные: {_get_field(b, 'outputData')}")
            if _get_field(b, "acceptanceCriteria"):
                prompt_lines.append(f"- Критерии: {_get_field(b, 'acceptanceCriteria')}")
        prompt_lines.append("")

    if service_blocks or other_blocks:
        prompt_lines.append("### Бизнес-логика и сервисы:")
        for b in (service_blocks + other_blocks):
            prompt_lines.append(f"\n**{_get_field(b, 'name')}** [{_get_field(b, 'label')}] (тип: {_get_field(b, 'type')})")
            if _get_field(b, "description"):
                prompt_lines.append(f"- Описание: {_get_field(b, 'description')}")
            if _get_field(b, "goal"):
                prompt_lines.append(f"- Цель: {_get_field(b, 'goal')}")
            if _get_field(b, "implementation"):
                prompt_lines.append(f"- Реализация: {_get_field(b, 'implementation')}")
            if _get_field(b, "apiEndpoints"):
                prompt_lines.append(f"- API: {_get_field(b, 'apiEndpoints')}")
        prompt_lines.append("")

    if data_blocks:
        prompt_lines.append("### Данные и хранение:")
        for b in data_blocks:
            prompt_lines.append(f"\n**{_get_field(b, 'name')}** [{_get_field(b, 'label')}]")
            if _get_field(b, "description"):
                prompt_lines.append(f"- Описание: {_get_field(b, 'description')}")
            if _get_field(b, "dataModel"):
                prompt_lines.append(f"- Модель данных: {_get_field(b, 'dataModel')}")
        prompt_lines.append("")

    # Связи как потоки данных
    if connections:
        prompt_lines.extend(["## Потоки данных между модулями", ""])
        for c in connections:
            from_id = str(_get_field(c, "fromBlockId") or "")
            to_id = str(_get_field(c, "toBlockId") or "")
            fb = block_idx.get(from_id, {})
            tb = block_idx.get(to_id, {})
            clabel = _get_field(c, "label") or _get_field(c, "description") or ""
            data_desc = _get_field(c, "dataDescription") or ""
            prompt_lines.append(
                f"- {_get_field(fb, 'name')} → {_get_field(tb, 'name')}" +
                (f": {clabel}" if clabel else "") +
                (f" (данные: {data_desc})" if data_desc else "")
            )
        prompt_lines.append("")

    prompt_lines.extend([
        "## Требования к дизайну",
        "",
        "- Современный, минималистичный UI",
        "- Адаптивный дизайн (мобильные + десктоп)",
        "- Используй shadcn/ui компоненты",
        "- Тёмная и светлая тема",
        "- Плавные анимации при переходах",
        "",
        "## Требования к реализации",
        "",
        "- React + TypeScript",
        "- Supabase для хранения данных",
        "- Tanstack Query для работы с данными",
        "- Все формы с валидацией",
        "- Обработка ошибок с понятными сообщениями",
        "",
    ])

    # --- Последовательные промпты для итерации ---
    followup_lines = [
        f"# {info['name']} — Последовательные промпты для Lovable",
        "",
        "Используй эти промпты один за другим после создания базового приложения.",
        "Каждый промпт — отдельное сообщение в Lovable.",
        "",
        "---",
        "",
    ]

    for step_num, b in enumerate(sorted_blocks, 1):
        name = _get_field(b, "name") or "?"
        label = _get_field(b, "label") or "?"
        desc = _get_field(b, "description") or ""
        impl = _get_field(b, "implementation") or ""
        user_flow = _get_field(b, "userFlow") or ""
        criteria = _get_field(b, "acceptanceCriteria") or ""
        edge_cases = _get_field(b, "edgeCases") or ""

        followup_lines.extend([
            f"## Промпт {step_num}: {name}",
            "",
            "```",
            f"Доработай модуль \"{name}\" [{label}].",
            "",
        ])
        if desc:
            followup_lines.append(f"Описание: {desc}")
        if impl:
            followup_lines.append(f"\nДетали реализации: {impl}")
        if user_flow:
            followup_lines.append(f"\nПользовательский сценарий: {user_flow}")

        followup_lines.append("")
        followup_lines.append("Проверь после реализации:")
        if criteria:
            followup_lines.append(f"- {criteria}")
        followup_lines.append("- Компонент отображается без ошибок")
        followup_lines.append("- Данные корректно загружаются и сохраняются")
        if edge_cases:
            followup_lines.append(f"- Граничные случаи: {edge_cases}")
        followup_lines.append("```")
        followup_lines.append("")

    # Финальный промпт
    followup_lines.extend([
        "## Финальный промпт: Проверка и полировка",
        "",
        "```",
        "Проведи финальную проверку всего приложения:",
        "",
        "1. Все страницы открываются без ошибок",
        "2. Навигация между страницами работает",
        "3. Данные сохраняются и загружаются корректно",
        "4. Все формы валидируются",
        "5. Мобильная версия выглядит хорошо",
        "6. Нет console.error в DevTools",
        "",
        "Если есть проблемы — исправь их. Затем добавь:",
        "- Loading состояния для всех запросов",
        "- Toast уведомления при ошибках",
        "- Плавные анимации переходов между страницами",
        "```",
    ])

    return {
        "LOVABLE_PROMPT.md": "\n".join(prompt_lines),
        "LOVABLE_FOLLOWUP.md": "\n".join(followup_lines),
    }


# ═══════════════════════════════════════════════════════════════
# GITHUB EXPORT
# ═══════════════════════════════════════════════════════════════

def build_github_export(
    project: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
) -> dict[str, str]:
    """Генерация файлов для GitHub репозитория."""
    info = _extract_project_info(project)
    block_idx = _build_block_index(blocks)
    sorted_blocks = _sorted_blocks_by_priority(blocks)

    slug = info["name"].lower().replace(" ", "-").replace("/", "-")

    # --- README.md ---
    readme = [
        f"# {info['name']}",
        "",
    ]
    if info["description"]:
        readme.append(f"> {info['description']}")
        readme.append("")
    if info["goal"]:
        readme.append(f"**Цель:** {info['goal']}")
        readme.append("")

    # Нарратив
    if info["narr_problem"] or info["narr_solution"]:
        readme.append("## О проекте")
        readme.append("")
        if info["narr_problem"]:
            readme.append(f"### Проблема\n{info['narr_problem']}")
            readme.append("")
        if info["narr_solution"]:
            readme.append(f"### Решение\n{info['narr_solution']}")
            readme.append("")

    # Архитектура
    readme.extend([
        f"## Архитектура ({len(blocks)} модулей)",
        "",
    ])
    for b in blocks:
        lbl = _get_field(b, "label") or "?"
        nm = _get_field(b, "name") or "?"
        desc = _get_field(b, "description") or ""
        readme.append(f"- **[{lbl}] {nm}** — {desc}")
    readme.append("")

    if connections:
        readme.append("## Связи")
        readme.append("")
        readme.append("```mermaid")
        readme.append("graph LR")
        for c in connections:
            from_id = str(_get_field(c, "fromBlockId") or "")
            to_id = str(_get_field(c, "toBlockId") or "")
            fb = block_idx.get(from_id, {})
            tb = block_idx.get(to_id, {})
            fn = _get_field(fb, "label") or "?"
            tn = _get_field(tb, "label") or "?"
            clabel = _get_field(c, "label") or ""
            if clabel:
                readme.append(f"    {fn}[{_get_field(fb, 'name')}] -->|{clabel}| {tn}[{_get_field(tb, 'name')}]")
            else:
                readme.append(f"    {fn}[{_get_field(fb, 'name')}] --> {tn}[{_get_field(tb, 'name')}]")
        readme.append("```")
        readme.append("")

    # Запуск
    readme.extend([
        "## Быстрый старт",
        "",
        "```bash",
        f"git clone https://github.com/your-org/{slug}.git",
        f"cd {slug}",
        "npm install",
        "npm run dev",
        "```",
        "",
        "## Разработка",
        "",
        "```bash",
        "npm run dev      # Режим разработки",
        "npm run build    # Сборка",
        "npm test         # Тесты",
        "npm run lint     # Линтер",
        "```",
        "",
    ])

    if info["audience"]:
        readme.append(f"## Целевая аудитория\n{info['audience']}")
        readme.append("")

    readme.extend([
        "## Документация",
        "",
        "- [Архитектура](docs/ARCHITECTURE.md)",
        "- [API](docs/API.md)",
        "- [План реализации](IMPLEMENTATION_PLAN.md)",
        "",
        "## Лицензия",
        "",
        "MIT",
    ])

    # --- docs/ARCHITECTURE.md ---
    arch = _build_full_spec(info, blocks, connections, decisions, block_idx)

    # --- docs/API.md ---
    api_lines = [
        f"# {info['name']} — API Документация",
        "",
    ]
    has_api = False
    for b in blocks:
        api = _get_field(b, "apiEndpoints")
        if api:
            has_api = True
            api_lines.append(f"## [{_get_field(b, 'label')}] {_get_field(b, 'name')}")
            api_lines.append("")
            api_lines.append(api)
            api_lines.append("")
            dm = _get_field(b, "dataModel")
            if dm:
                api_lines.append(f"### Модель данных\n{dm}")
                api_lines.append("")
    if not has_api:
        api_lines.append("API endpoints не определены в спецификации.")

    # --- .github/workflows/ci.yml ---
    ci_yml = """name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node-version: [18.x, 20.x]

    steps:
      - uses: actions/checkout@v4

      - name: Use Node.js ${{ matrix.node-version }}
        uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
          cache: 'npm'

      - name: Install dependencies
        run: npm ci

      - name: Type check
        run: npx tsc --noEmit

      - name: Lint
        run: npm run lint --if-present

      - name: Test
        run: npm test --if-present

      - name: Build
        run: npm run build --if-present
"""

    # --- CONTRIBUTING.md ---
    contrib = f"""# Contributing to {info['name']}

## Начало работы

1. Форкните репозиторий
2. Создайте ветку: `git checkout -b feature/my-feature`
3. Внесите изменения
4. Запустите тесты: `npm test`
5. Создайте PR

## Архитектура

Проект состоит из {len(blocks)} модулей. Подробнее — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Модули

"""
    for b in blocks:
        contrib += f"- **[{_get_field(b, 'label')}] {_get_field(b, 'name')}** — {_get_field(b, 'description') or 'нет описания'}\n"

    contrib += """
## Правила кода

- TypeScript с strict mode
- Каждый модуль — отдельная директория
- Обязательные тесты для бизнес-логики
- Осмысленные имена переменных и функций
- JSDoc для публичных функций

## Issues

Используйте GitHub Issues для багов и предложений.
Каждый Issue должен содержать:
- Описание проблемы / предложения
- Шаги воспроизведения (для багов)
- Ожидаемое поведение
"""

    # --- .env.example ---
    env_example = f"# {info['name']} — Environment Variables\n\n"
    env_example += "NODE_ENV=development\n"
    env_example += "PORT=3000\n"
    for b in blocks:
        bt = _get_field(b, "type")
        nm = _get_field(b, "name") or ""
        slug_nm = nm.upper().replace(" ", "_").replace("-", "_")
        if bt in ("database", "data", "storage"):
            env_example += f"# {nm}\nDATABASE_URL=postgresql://user:pass@localhost:5432/db\n"
        elif bt in ("integration", "external"):
            env_example += f"# {nm}\n{slug_nm}_API_KEY=your_api_key_here\n{slug_nm}_API_URL=https://api.example.com\n"
        elif bt in ("auth",):
            env_example += f"# {nm}\nJWT_SECRET=your_secret_here\nSESSION_TTL=3600\n"

    # --- IMPLEMENTATION_PLAN.md ---
    plan = _build_implementation_plan(info, blocks, connections, decisions, block_idx, "GitHub")

    # --- ISSUES.md (задачи для GitHub Issues) ---
    issues_lines = [
        f"# {info['name']} — GitHub Issues",
        "",
        "Создайте следующие Issues в репозитории для трекинга прогресса:",
        "",
    ]
    for i, b in enumerate(sorted_blocks, 1):
        label = _get_field(b, "label") or "?"
        name = _get_field(b, "name") or "?"
        desc = _get_field(b, "description") or "нет описания"
        layer = _get_field(b, "layer") or "mvp"
        complexity = _get_field(b, "estimatedComplexity") or "medium"
        criteria = _get_field(b, "acceptanceCriteria") or ""

        issues_lines.extend([
            f"## Issue #{i}: Реализация [{label}] {name}",
            "",
            f"**Labels:** `module`, `{layer}`, `complexity:{complexity}`",
            "",
            "### Описание",
            desc,
            "",
        ])
        if criteria:
            issues_lines.append(f"### Критерии приёмки\n{criteria}")
            issues_lines.append("")
        issues_lines.extend([
            "### Чеклист",
            "- [ ] Создать файлы модуля",
            "- [ ] Реализовать основную логику",
            "- [ ] Подключить связи с другими модулями",
            "- [ ] Написать тесты",
            "- [ ] Code review",
            "",
            "---",
            "",
        ])

    # --- package.json ---
    tech_stacks = [_get_field(b, "techStack") or _get_field(b, "blockTechStack") for b in blocks if (_get_field(b, "techStack") or _get_field(b, "blockTechStack"))]
    all_tech = " ".join(tech_stacks).lower()

    deps: dict[str, str] = {"typescript": "^5.0.0"}
    if "react" in all_tech:
        deps.update({"react": "^18.0.0", "react-dom": "^18.0.0"})
    if "express" in all_tech:
        deps["express"] = "^4.18.0"
    if "node" in all_tech or not tech_stacks:
        deps["ts-node"] = "^10.9.0"

    pkg = {
        "name": slug,
        "version": "1.0.0",
        "description": info["description"] or info["name"],
        "main": "src/index.ts",
        "scripts": {
            "start": "ts-node src/index.ts",
            "dev": "nodemon --exec ts-node src/index.ts",
            "build": "tsc",
            "test": "jest",
            "lint": "eslint src/ --ext .ts",
        },
        "dependencies": deps,
        "devDependencies": {
            "@types/node": "^20.0.0",
            "jest": "^29.0.0",
            "ts-jest": "^29.0.0",
            "nodemon": "^3.0.0",
            "eslint": "^8.0.0",
        },
    }

    # --- tsconfig.json ---
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "commonjs",
            "lib": ["ES2022"],
            "outDir": "./dist",
            "rootDir": "./src",
            "strict": True,
            "esModuleInterop": True,
            "skipLibCheck": True,
            "forceConsistentCasingInFileNames": True,
            "resolveJsonModule": True,
            "declaration": True,
            "declarationMap": True,
            "sourceMap": True,
        },
        "include": ["src/**/*"],
        "exclude": ["node_modules", "dist"],
    }

    return {
        "README.md": "\n".join(readme),
        "docs/ARCHITECTURE.md": arch,
        "docs/API.md": "\n".join(api_lines),
        ".github/workflows/ci.yml": ci_yml,
        "CONTRIBUTING.md": contrib,
        ".env.example": env_example,
        "IMPLEMENTATION_PLAN.md": plan,
        "ISSUES.md": "\n".join(issues_lines),
        "package.json": json.dumps(pkg, indent=2, ensure_ascii=False),
        "tsconfig.json": json.dumps(tsconfig, indent=2),
    }


# ═══════════════════════════════════════════════════════════════
# LLM-генерация связного описания ТЗ
# ═══════════════════════════════════════════════════════════════

_TZ_SYSTEM_PROMPT = """Ты — Senior Solutions Architect и технический писатель.
Твоя задача — на основе структурированных данных о проекте (модули, связи, бизнес-ценность, метрики)
написать связное, профессиональное ТЕХНИЧЕСКОЕ ЗАДАНИЕ (ТЗ) для разработки.

Правила:
- Пиши на русском языке
- Используй ВСЕ предоставленные данные — каждый модуль, каждую связь, каждую метрику
- Не выдумывай данных, которых нет — только переформулируй и структурируй то, что дано
- Текст должен быть связным и читаемым, а не списком — он должен объяснять систему целиком
- Включи: обзор системы, описание каждого модуля, взаимодействие модулей, бизнес-ценность
- Формат: Markdown с заголовками ##, ###, списками и выделением ключевых моментов
"""


async def _generate_tz_description(
    info: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
    block_idx: dict[str, dict],
) -> str:
    """Генерация связного описания ТЗ через LLM на основе данных из блоков."""
    try:
        from backend.core.llm.router import ModelTier, get_llm_router
        router = get_llm_router()
    except Exception as e:
        logger.warning("llm_router_unavailable", error=str(e))
        return ""

    prompt_parts = [
        f"# Проект: {info['name']}",
        f"Описание: {info['description']}" if info["description"] else "",
        f"Цель: {info['goal']}" if info["goal"] else "",
        f"Целевая аудитория: {info['audience']}" if info["audience"] else "",
        "",
        "## Модули системы:",
        "",
    ]

    for b in blocks:
        label = _get_field(b, "label") or "?"
        name = _get_field(b, "name") or "?"
        btype = _get_field(b, "type") or "?"
        layer = _get_field(b, "layer") or "?"
        prompt_parts.append(f"### [{label}] {name} (тип: {btype}, слой: {layer})")

        for field_label, key in BLOCK_FIELD_MAP:
            val = _get_field(b, key)
            if val:
                prompt_parts.append(f"  {field_label}: {val}")
        prompt_parts.append("")

    if connections:
        prompt_parts.append("## Связи между модулями:")
        prompt_parts.append("")
        for c in connections:
            from_id = str(_get_field(c, "fromBlockId") or "")
            to_id = str(_get_field(c, "toBlockId") or "")
            fb = block_idx.get(from_id, {})
            tb = block_idx.get(to_id, {})
            fn = _get_field(fb, "name") or "?"
            tn = _get_field(tb, "name") or "?"
            clabel = _get_field(c, "label") or ""
            data_desc = _get_field(c, "dataDescription") or ""
            biz = _get_field(c, "businessMeaning") or ""
            prompt_parts.append(f"  {fn} -> {tn}: {clabel}")
            if data_desc:
                prompt_parts.append(f"    Данные: {data_desc}")
            if biz:
                prompt_parts.append(f"    Бизнес-смысл: {biz}")
        prompt_parts.append("")

    if decisions:
        prompt_parts.append("## Архитектурные решения:")
        for d in decisions:
            prompt_parts.append(f"  - {d.get('title', '')}: {d.get('description', '')}")

    prompt = "\n".join(prompt_parts)

    try:
        tz_text = await router.generate(
            prompt=f"Вот структурированные данные проекта. Напиши на их основе полное связное Техническое Задание:\n\n{prompt}",
            system_prompt=_TZ_SYSTEM_PROMPT,
            temperature=0.4,
            max_tokens=8192,
            model_tier=ModelTier.STANDARD,
        )
        return tz_text.strip() if tz_text else ""
    except Exception as e:
        logger.error("tz_generation_failed", error=str(e))
        return ""


# ═══════════════════════════════════════════════════════════════
# Главная функция экспорта
# ═══════════════════════════════════════════════════════════════

async def export_project_for_platform(
    project: dict,
    blocks: list[dict],
    connections: list[dict],
    decisions: list[dict],
    target: str,
) -> dict:
    """
    Экспорт проекта SIMA для указанной платформы.

    Args:
        project: данные проекта
        blocks: блоки проекта
        connections: связи между блоками
        decisions: архитектурные решения
        target: платформа (cursor, claude-code, lovable, github)

    Returns:
        {"type": target, "files": {"filename": "content", ...}}
    """
    if target == "cursor":
        files = build_cursor_export(project, blocks, connections, decisions)
    elif target == "claude-code":
        files = build_claude_code_export(project, blocks, connections, decisions)
    elif target == "lovable":
        files = build_lovable_export(project, blocks, connections, decisions)
    elif target == "github":
        files = build_github_export(project, blocks, connections, decisions)
    else:
        return {"error": f"Неизвестная платформа: {target}. Доступные: cursor, claude-code, lovable, github"}

    info = _extract_project_info(project)
    block_idx = _build_block_index(blocks)
    tz_description = await _generate_tz_description(info, blocks, connections, decisions, block_idx)

    if tz_description:
        header = (
            f"# {info['name']} — Техническое задание\n\n"
            "> Этот документ сгенерирован автоматически системой СИМА на основе данных проекта.\n"
            "> Он содержит связное описание всей системы, каждого модуля, их взаимосвязей и бизнес-ценности.\n\n"
            "---\n\n"
        )
        files["TZ_DESCRIPTION.md"] = header + tz_description

    return {
        "type": target,
        "files": files,
    }
