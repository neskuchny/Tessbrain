# -*- coding: utf-8 -*-
"""Процесс из естественного языка на доске (F enterprise-роадмапа).

Пользователь описывает нужный процесс обычными словами → система:
  1. ПОНИМАЕТ логику (декомпозиция задачи — premium-моделью: раскладка
     произвольного запроса в корректный граф — задача планирования, на
     лёгкой модели схемы выходят кривыми);
  2. МАПИТ на наши функции — существующие типы блоков доски (каталог ниже);
  3. СТРОИТ схему: блоки, промпты, связи, настройки, расписание;
  4. ОБЪЯСНЯЕТ: комментарий «что/зачем» на каждом блоке + note-узел со
     сводкой «как это работает» — пользователь понимает замысел, а не
     получает чёрный ящик.

Архитектура «LLM предлагает — КОД валидирует» (тот же принцип, что в
analysis/planning.py и artifacts/composer.py): планировщик возвращает план
как ДАННЫЕ, валидатор в коде проверяет типы блоков, связность, отсутствие
циклов, обязательные поля и промпты; невалидное чинится дефолтами или
отбрасывается С WARNING (не молча). Позиции узлов — автолейаут по слоям
топологической сортировки. Результат — готовый workflow в формате галереи
шаблонов ({version, name, nodes, edges, edgeStyle}), фронт кладёт его в
редактор тем же путём, что шаблон.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Каталог блоков: единственный источник правды для промпта и валидатора ──
# required: поля, без которых блок не работает (чиним дефолтом или дропаем);
# defaults: что подставить, если LLM не заполнил; doc — для промпта планировщика.
BLOCK_CATALOG: Dict[str, Dict[str, Any]] = {
    # поля сверены с process_engine._process_handler и triggers.schedule_spec
    "trigger": {
        "doc": ("старт процесса. Расписание: trigger_type='schedule' + "
                "schedule_kind='interval'(+interval_min, минуты, мин.5)|"
                "'daily'(+daily_time HH:MM UTC)|'weekly'(+weekday 0-6, 0=пн, "
                "+daily_time). Событие: trigger_type='event'+event_type "
                "('meeting_ended'). Ручной: trigger_type='manual'. "
                "payload — стартовый текст в поток."),
        "required": ["trigger_type"],
        "defaults": {"trigger_type": "manual", "label": "Старт"},
    },
    "ask_brain": {
        "doc": ("вопрос к мозгу компании (встречи/документы/граф/числа). "
                "data.prompt — что собрать/спросить; результат уходит дальше."),
        "required": ["prompt"],
        "defaults": {"label": "Вопрос к мозгу"},
    },
    "generate": {
        "doc": ("LLM-генерация текста. data.prompt — инструкция; {{input}} "
                "внутри = текст предыдущего блока."),
        "required": ["prompt"],
        "defaults": {"label": "Генерация текста"},
    },
    "condition": {
        "doc": ("ветвление по входному тексту: data.op = contains|equals|"
                "not_contains|regex|gt|lt|is_empty, data.contains — искомое "
                "значение/паттерн/число. Исходящие связи ОБЯЗАНЫ иметь "
                "sourceHandle 'true' или 'false'."),
        "required": [],
        "defaults": {"label": "Условие", "op": "contains"},
    },
    "task": {
        "doc": ("создать задачу в задачнике из входа. data.title — заголовок "
                "задачи (иначе первые 80 симв. входа)."),
        "required": [],
        "defaults": {"label": "Задача"},
    },
    "action": {
        "doc": ("действие во внешней интеграции. data.tool_name (например "
                "'slack_send_message') + data.params (объект; {{input}} в "
                "строках = вход). Использовать только если пользователь явно "
                "назвал внешнюю систему (slack/notion/jira/…)."),
        "required": ["tool_name"],
        "defaults": {"label": "Действие", "params": {}},
    },
    "report": {
        "doc": ("отчёт по данным мозга за период. data.report_type = "
                "summary|tasks|decisions (дефолт summary), data.days_back "
                "(дефолт 30)."),
        "required": [],
        "defaults": {"label": "Отчёт", "report_type": "summary"},
    },
    "notify": {
        "doc": ("доставка результата. data.channel='telegram' (или 'email'), "
                "data.text — сообщение/подпись (иначе вход), data.subject. "
                "Если на входе картинка — уйдёт фото."),
        "required": ["channel"],
        "defaults": {"channel": "telegram", "label": "Уведомление"},
    },
    "note": {
        "doc": ("комментарий на холсте (не исполняется): data.label — заголовок, "
                "data.text — пояснение. Использовать для объяснения схемы."),
        "required": ["text"],
        "defaults": {"label": "Заметка"},
    },
    # ── креативная цепочка (генерация изображений) ──
    "prompt": {
        "doc": ("промпт картинки: data.prompt — описание изображения; "
                "{{input}} внутри = текст предыдущего блока."),
        "required": ["prompt"],
        "defaults": {"label": "Промпт картинки"},
    },
    "textCombiner": {
        "doc": ("склейка текстов нескольких входов: data.template с "
                "{{input1}}..{{inputN}} (без template — простая склейка)."),
        "required": [],
        "defaults": {"label": "Склейка текста"},
    },
    "llmGenerate": {
        "doc": ("LLM-шаг внутри креативной цепочки (переписать/усилить промпт). "
                "data.inputPrompt — инструкция (иначе вход)."),
        "required": ["inputPrompt"],
        "defaults": {"label": "LLM-шаг"},
    },
    "nanoBanana": {
        "doc": ("генерация изображения из текста предыдущего блока. "
                "data.model='nano-banana' (Gemini) или 'gpt-image-1' (OpenAI); "
                "data.aspectRatio '1:1'|'16:9'|'9:16'."),
        "required": [],
        "defaults": {"label": "Картинка", "model": "nano-banana",
                     "aspectRatio": "1:1", "resolution": "1K",
                     "inputImages": [], "inputPrompt": None,
                     "outputImage": None, "useGoogleSearch": False,
                     "gptImageSize": "1024x1024", "gptImageQuality": "medium",
                     "status": "idle", "error": None},
    },
    "output": {
        "doc": "терминальный узел результата (показывает текст/картинку на доске).",
        "required": [],
        "defaults": {"label": "Результат", "image": None},
    },
}

_SCHEDULE_KINDS = ("interval", "daily", "weekly")
_MAX_NODES = 24


# ── Промпт планировщика ────────────────────────────────────────────────────

def _catalog_block() -> str:
    lines = []
    for t, spec in BLOCK_CATALOG.items():
        req = ", ".join(spec["required"]) or "—"
        lines.append(f"- {t}: {spec['doc']} Обязательные поля data: {req}")
    return "\n".join(lines)


_FEW_SHOT = """
ПРИМЕР (запрос: «каждое утро собери дайджест за вчера и пришли картинкой в телеграм»):
{
 "title": "Утренний дайджест картинкой",
 "summary": "Каждое утро в 06:00 мозг собирает вчерашний дайджест, креативная цепочка рисует одну картинку-визуализацию, фото приходит в Telegram.",
 "nodes": [
  {"id": "t1", "type": "trigger", "comment": "Запускает процесс каждое утро без участия человека",
   "data": {"label": "Ежедневно в 06:00 UTC", "trigger_type": "schedule", "schedule_kind": "daily", "daily_time": "06:00", "payload": "утренний дайджест за вчера"}},
  {"id": "b1", "type": "ask_brain", "comment": "Мозг достаёт реальные события вчера — только факты, без выдумок",
   "data": {"label": "Дайджест за вчера", "prompt": "Собери короткий дайджест за вчера по данным мозга: главные события, решения, продвижение по целям. 3-5 пунктов одной строкой, только реальные факты. Нет данных — так и напиши."}},
  {"id": "p1", "type": "prompt", "comment": "Превращает текст дайджеста в задание для картинки",
   "data": {"label": "Промпт картинки", "prompt": "Минималистичная деловая инфографика утреннего дайджеста: иконки-пункты, короткие русские подписи СТРОГО из содержания ниже, светлый фон. Содержание:\\n{{input}}"}},
  {"id": "img1", "type": "nanoBanana", "comment": "Сервер сам рисует изображение — браузер не нужен",
   "data": {"label": "Картинка", "model": "nano-banana", "aspectRatio": "1:1"}},
  {"id": "n1", "type": "notify", "comment": "Готовое фото уходит в ваш Telegram",
   "data": {"label": "Фото в Telegram", "channel": "telegram", "text": "☀️ Утренний дайджест"}}
 ],
 "edges": [
  {"source": "t1", "target": "b1"},
  {"source": "b1", "target": "p1"},
  {"source": "p1", "target": "img1"},
  {"source": "img1", "target": "n1"}
 ]
}
"""


_RULES = (
    "ПРАВИЛА:\n"
    "- process-блоки соединяются в цепочку/ветвления; картинки делает "
    "ТОЛЬКО связка prompt → nanoBanana (текстовый вход приходит в prompt "
    "как {{input}});\n"
    "- начинай с trigger (расписание — если пользователь назвал "
    "периодичность, иначе manual);\n"
    "- доставка результата — notify (telegram по умолчанию);\n"
    "- НЕ выдумывай типы блоков и поля вне каталога; не делай больше "
    f"{_MAX_NODES} блоков; каждый блок участвует в связях;\n"
    "- summary: 2-3 предложения «как это работает» человеческим языком.\n"
)

_JSON_CONTRACT = (
    "Верни СТРОГО JSON:\n"
    '{"title": "...", "summary": "...", '
    '"nodes": [{"id","type","comment","data":{...}}], '
    '"edges": [{"source","target"}]}\n'
)


def _current_graph_block(workflow: Dict[str, Any]) -> str:
    """Компактное представление текущей схемы для правки (без стилей/позиций)."""
    lines: List[str] = []
    for n in (workflow.get("nodes") or []):
        if n.get("type") == "note":
            continue
        d = n.get("data") or {}
        keys = {k: v for k, v in d.items()
                if k in ("label", "prompt", "trigger_type", "schedule_kind",
                         "daily_time", "interval_min", "weekday", "channel",
                         "text", "model", "op", "contains", "tool_name",
                         "report_type", "inputPrompt")}
        lines.append(f'  {{"id": "{n.get("id")}", "type": "{n.get("type")}", '
                     f'"data": {json.dumps(keys, ensure_ascii=False)}}}')
    edges = ", ".join(
        f'{e.get("source")}→{e.get("target")}'
        + (f'[{e["sourceHandle"]}]' if e.get("sourceHandle") else "")
        for e in (workflow.get("edges") or []))
    return "БЛОКИ:\n" + "\n".join(lines) + f"\nСВЯЗИ: {edges or '(нет)'}"


def _planner_prompt(request_text: str,
                    current: Optional[Dict[str, Any]] = None) -> str:
    if current is not None:
        return (
            "Ты — архитектор процессов на визуальной доске Tessbrain. У "
            "пользователя УЖЕ есть готовая схема (ниже), и он просит её "
            "ИЗМЕНИТЬ своими словами. Внеси именно запрошенную правку, "
            "сохранив остальное:\n"
            "- сохраняй id существующих блоков, которые НЕ меняешь (чтобы "
            "их расположение на холсте не сбилось);\n"
            "- добавляй/удаляй/переподключай блоки только по сути запроса;\n"
            "- у новых/изменённых блоков заполни prompt/настройки и comment "
            "«что/зачем»;\n"
            "- верни ПОЛНУЮ обновлённую схему целиком (не дифф).\n\n"
            f"ДОСТУПНЫЕ ТИПЫ БЛОКОВ:\n{_catalog_block()}\n\n"
            f"{_RULES}\n"
            f"ТЕКУЩАЯ СХЕМА:\n{_current_graph_block(current)}\n\n"
            f"{_JSON_CONTRACT}\n"
            f"ЧТО ИЗМЕНИТЬ:\n{request_text[:4000]}"
        )
    return (
        "Ты — архитектор процессов на визуальной доске Tessbrain. Пользователь "
        "описал желаемый процесс СВОИМИ СЛОВАМИ. Твоя работа в три шага:\n"
        "1) пойми ЛОГИКУ: что должно происходить, в каком порядке, из каких "
        "данных, с каким результатом;\n"
        "2) реализуй её ТОЛЬКО из доступных типов блоков (каталог ниже) — "
        "подбери минимальную цепочку, которая реально даст результат;\n"
        "3) заполни КАЖДЫЙ блок: качественный русский prompt (конкретный, с "
        "фактической дисциплиной — «только из данных, не выдумывай»), настройки, "
        "расписание. К каждому блоку — comment: одна фраза ЧТО он делает и "
        "ЗАЧЕМ (для человека без технического бэкграунда).\n\n"
        f"ДОСТУПНЫЕ ТИПЫ БЛОКОВ:\n{_catalog_block()}\n\n"
        f"{_RULES}\n"
        f"{_FEW_SHOT}\n"
        f"{_JSON_CONTRACT}\n"
        f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{request_text[:4000]}"
    )


# ── Валидатор в коде: LLM предлагает, код проверяет ────────────────────────

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")


def _validate_and_fix(plan: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Проверить план и привести к исполнимому виду. Возвращает
    (чистый план, warnings). Невалидное чинится дефолтами или удаляется —
    каждый случай попадает в warnings (не молча)."""
    warnings: List[str] = []
    if not isinstance(plan, dict):
        raise ValueError("планировщик вернул не объект")

    raw_nodes = plan.get("nodes")
    raw_edges = plan.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("в плане нет блоков")
    if not isinstance(raw_edges, list):
        raw_edges = []

    nodes: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for i, n in enumerate(raw_nodes[:_MAX_NODES]):
        if not isinstance(n, dict):
            warnings.append(f"блок #{i} не объект — пропущен")
            continue
        ntype = str(n.get("type") or "")
        spec = BLOCK_CATALOG.get(ntype)
        if spec is None:
            warnings.append(f"блок «{n.get('id') or i}»: неизвестный тип "
                            f"«{ntype}» — пропущен")
            continue
        nid = str(n.get("id") or f"n{i}")
        if not _ID_RE.match(nid) or nid in seen_ids:
            old = nid
            nid = f"n{i}"
            warnings.append(f"блок «{old}»: некорректный/дублирующий id — заменён на {nid}")
        seen_ids.add(nid)
        data = dict(n.get("data") or {})
        # дефолты каталога — недостающие настройки не валят блок
        for k, v in spec["defaults"].items():
            data.setdefault(k, v)
        # обязательные поля: чиним из дефолтов, иначе дроп с warning
        missing = [f for f in spec["required"] if not data.get(f)]
        if missing:
            warnings.append(f"блок «{data.get('label') or nid}» ({ntype}): "
                            f"нет обязательных полей {missing} — пропущен")
            continue
        # промпт-поля не должны быть пустыми/односложными
        prompt_fields = [f for f in spec["required"]
                         if f in ("prompt", "inputPrompt")]
        if any(len(str(data.get(f) or "").strip()) < 10 for f in prompt_fields):
            warnings.append(f"блок «{data.get('label') or nid}»: промпт слишком "
                            "короткий — пропущен")
            continue
        nodes.append({"id": nid, "type": ntype, "data": data,
                      "comment": str(n.get("comment") or "").strip()[:200]})

    if not nodes:
        raise ValueError("после валидации не осталось ни одного блока")

    ids = {n["id"] for n in nodes}
    type_of = {n["id"]: n["type"] for n in nodes}
    edges: List[Dict[str, str]] = []
    seen_e: set = set()
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s not in ids or t not in ids or s == t:
            warnings.append(f"связь {s}→{t}: указывает на несуществующий блок — удалена")
            continue
        key = (s, t)
        if key in seen_e:
            continue
        seen_e.add(key)
        edge: Dict[str, str] = {"source": s, "target": t}
        # рёбра из condition обязаны нести sourceHandle true/false — иначе
        # исполнение не активирует ветку
        if type_of.get(s) == "condition":
            sh = str(e.get("sourceHandle") or "").lower()
            if sh not in ("true", "false"):
                sh = "true"
                warnings.append(f"связь {s}→{t}: ветка условия не указана — "
                                "принята «да» (true)")
            edge["sourceHandle"] = sh
        edges.append(edge)

    # циклы запрещены (процесс — DAG): рвём ребро, замыкающее цикл
    edges, cycle_warns = _break_cycles(nodes, edges)
    warnings.extend(cycle_warns)

    # блоки-сироты (кроме note): предупреждаем — исполнение их не увидит
    linked = {e["source"] for e in edges} | {e["target"] for e in edges}
    for n in nodes:
        if n["type"] != "note" and n["id"] not in linked and len(nodes) > 1:
            warnings.append(f"блок «{n['data'].get('label') or n['id']}» не связан "
                            "с процессом — проверьте связи")

    # валидация расписания триггера
    for n in nodes:
        if n["type"] != "trigger":
            continue
        d = n["data"]
        if d.get("trigger_type") == "schedule":
            kind = d.get("schedule_kind")
            if kind not in _SCHEDULE_KINDS:
                d["trigger_type"] = "manual"
                warnings.append("расписание триггера не распознано — переключён "
                                "на ручной запуск")
                continue
            if kind == "interval":
                # движок читает interval_min (см. triggers.schedule_spec)
                try:
                    d["interval_min"] = max(5, int(
                        d.get("interval_min") or d.pop("interval_minutes", None) or 60))
                except (TypeError, ValueError):
                    d["interval_min"] = 60
            else:
                t = str(d.get("daily_time") or "09:00")
                if not re.match(r"^\d{1,2}:\d{2}$", t):
                    d["daily_time"] = "09:00"
                    warnings.append("время триггера не распознано — 09:00 UTC")
                if kind == "weekly":
                    # движок читает weekday 0-6 (0=пн)
                    try:
                        d["weekday"] = int(
                            d.get("weekday") if d.get("weekday") is not None
                            else d.pop("weekly_day", 0)) % 7
                    except (TypeError, ValueError):
                        d["weekday"] = 0

    return {"title": str(plan.get("title") or "Процесс").strip()[:120],
            "summary": str(plan.get("summary") or "").strip()[:1000],
            "nodes": nodes, "edges": edges}, warnings


def _break_cycles(nodes: List[dict], edges: List[Dict[str, str]],
                  ) -> Tuple[List[Dict[str, str]], List[str]]:
    """DFS: удалить back-edges (процесс должен быть DAG)."""
    adj: Dict[str, List[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], []).append(e["target"])
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n["id"]: WHITE for n in nodes}
    bad: set = set()

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in adj.get(u, []):
            if (u, v) in bad:
                continue
            if color.get(v) == GRAY:
                bad.add((u, v))
            elif color.get(v) == WHITE:
                dfs(v)
        color[u] = BLACK

    for n in nodes:
        if color[n["id"]] == WHITE:
            dfs(n["id"])
    if not bad:
        return edges, []
    kept = [e for e in edges if (e["source"], e["target"]) not in bad]
    return kept, [f"связь {s}→{t} создавала цикл — удалена" for s, t in sorted(bad)]


# ── Автолейаут: слои топологической сортировки слева направо ───────────────

def _layout(nodes: List[dict], edges: List[Dict[str, str]]) -> Dict[str, dict]:
    indeg: Dict[str, int] = {n["id"]: 0 for n in nodes}
    adj: Dict[str, List[str]] = {}
    for e in edges:
        indeg[e["target"]] += 1
        adj.setdefault(e["source"], []).append(e["target"])
    layer: Dict[str, int] = {}
    queue = [nid for nid, d in indeg.items() if d == 0]
    for nid in queue:
        layer[nid] = 0
    while queue:
        u = queue.pop(0)
        for v in adj.get(u, []):
            layer[v] = max(layer.get(v, 0), layer[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    for n in nodes:  # изолированные/циклические остатки — слой 0
        layer.setdefault(n["id"], 0)
    # Раскладка с РЕАЛЬНЫМИ габаритами узлов: прежние шаги 290×190 были
    # меньше размеров карточек (до 320×380) — узлы ложились друг на друга
    # («карточки одна на другой, ничего не понятно»). Ширина колонки —
    # максимум по типам + зазор; Y — накопительно по высоте узла + зазор.
    _H = {"imageInput": 280, "annotation": 280, "prompt": 220,
          "textCombiner": 380, "nanoBanana": 300, "llmGenerate": 360,
          "infographic": 280, "audio": 220, "output": 320, "note": 160,
          "trigger": 150, "ask_brain": 200, "report": 160, "notify": 210,
          "task": 160, "action": 240, "generate": 200, "condition": 180,
          "wait_reply": 220, "meeting_data": 200, "meeting_share": 210,
          "document": 200, "crm_data": 180, "web_search": 150,
          "coding_agent": 190, "report_xlsx": 180, "translate": 140,
          "doc_edit": 150, "crm_write": 210}
    X0, Y0, COL_W, GAP_X, GAP_Y = 40, 60, 320, 70, 50
    y_cursor: Dict[int, int] = {}
    pos: Dict[str, dict] = {}
    for n in nodes:
        l = layer[n["id"]]
        y = y_cursor.get(l, Y0)
        pos[n["id"]] = {"x": X0 + l * (COL_W + GAP_X), "y": y}
        y_cursor[l] = y + _H.get(str(n.get("type")), 220) + GAP_Y
    return pos


# ── Сборка workflow в формате галереи шаблонов ─────────────────────────────

def _to_workflow(plan: Dict[str, Any],
                 keep_positions: Optional[Dict[str, dict]] = None) -> Dict[str, Any]:
    pos = _layout(plan["nodes"], plan["edges"])
    # правка: сохранить позиции блоков, чьи id остались (холст не сбивается);
    # новые блоки — по автолейауту, но со сдвигом вниз, чтобы не наехать.
    if keep_positions:
        max_y = max((p.get("y", 0) for p in keep_positions.values()), default=0)
        for n in plan["nodes"]:
            if n["id"] in keep_positions:
                pos[n["id"]] = keep_positions[n["id"]]
            else:
                pos[n["id"]] = {"x": pos[n["id"]]["x"], "y": pos[n["id"]]["y"] + max_y + 120}
    out_nodes: List[dict] = []
    # note-сводка «как это работает» — объяснение схемы прямо на холсте
    if plan.get("summary"):
        out_nodes.append({
            "id": "note-nl-summary", "type": "note",
            "position": {"x": 40, "y": 40},
            "data": {"label": "Как это работает", "text": plan["summary"]},
            "style": {"width": 340, "height": 130},
        })
    for n in plan["nodes"]:
        data = dict(n["data"])
        # комментарий «что/зачем» кладём в data.comment — виден в инспекторе
        if n.get("comment"):
            data["comment"] = n["comment"]
        out_nodes.append({"id": n["id"], "type": n["type"],
                          "position": pos[n["id"]], "data": data})
    out_edges = []
    for e in plan["edges"]:
        oe: Dict[str, Any] = {"id": f"e-{e['source']}-{e['target']}",
                              "source": e["source"], "target": e["target"]}
        if e.get("sourceHandle"):
            oe["sourceHandle"] = e["sourceHandle"]
        out_edges.append(oe)
    return {"version": 1, "name": plan["title"],
            "nodes": out_nodes, "edges": out_edges, "edgeStyle": "curved"}


# ── Точка входа ────────────────────────────────────────────────────────────

def _positions_of(workflow: Optional[Dict[str, Any]]) -> Dict[str, dict]:
    """id → position существующих узлов (для сохранения расклада при правке)."""
    out: Dict[str, dict] = {}
    for n in (workflow or {}).get("nodes", []) or []:
        p = n.get("position")
        if n.get("id") and isinstance(p, dict):
            out[str(n["id"])] = {"x": p.get("x", 0), "y": p.get("y", 0)}
    return out


async def design_process(user_id: str, request_text: str,
                         llm: Any = None,
                         current_workflow: Optional[Dict[str, Any]] = None,
                         ) -> Dict[str, Any]:
    """NL-запрос → готовый workflow доски + объяснения + warnings.

    current_workflow — если передан, это ПРАВКА существующей схемы («добавь
    ещё уведомление в почту»): планировщик видит текущий граф, вносит правку,
    позиции неизменных блоков сохраняются.

    Планировщик — PREMIUM-модель (декомпозиция в корректный граф — сложное
    планирование). Возвращает {success, workflow, summary, blocks, warnings}
    или {success: False, error}."""
    request_text = (request_text or "").strip()
    is_edit = bool(current_workflow and (current_workflow.get("nodes")))
    if len(request_text) < (4 if is_edit else 10):
        return {"success": False,
                "error": ("что изменить?" if is_edit else
                          "опишите процесс подробнее (что, из каких данных, "
                          "куда доставить результат)")}
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    try:
        from backend.core.llm.router import ModelTier
        tier_kw = {"model_tier": ModelTier.PREMIUM}
    except Exception:
        tier_kw = {}
    try:
        raw = await llm.generate_json(
            _planner_prompt(request_text, current=current_workflow if is_edit else None),
            system_prompt=("Ты проектируешь исполнимые процессы из готовых "
                           "блоков. Только JSON по контракту."),
            temperature=0.2, max_tokens=8192, **tier_kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("nl_designer: планировщик упал: %s", e)
        return {"success": False, "error": f"планировщик недоступен: {e}"}

    try:
        plan, warnings = _validate_and_fix(raw)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    keep = _positions_of(current_workflow) if is_edit else None
    workflow = _to_workflow(plan, keep_positions=keep)
    blocks = [{"id": n["id"], "type": n["type"],
               "label": n["data"].get("label") or n["id"],
               "comment": n.get("comment") or ""}
              for n in plan["nodes"]]
    return {"success": True, "workflow": workflow,
            "title": plan["title"], "summary": plan["summary"],
            "blocks": blocks, "warnings": warnings, "edited": is_edit}


__all__ = ["design_process", "BLOCK_CATALOG"]
