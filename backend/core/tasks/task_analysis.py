# -*- coding: utf-8 -*-
"""
Task Analysis + Coding Handoff — анализ выполненных/невыполненных задач и
делегация доработки кодинг-агентам (Claude Code / Cursor / Codex) через CLI.

Запрос: «анализировать, какие задачи выполнены/нет (в т.ч. из задачников),
выполнять через ТЗ Тессента и потом подключаться через CLI к Claude Code/
Cursor/Codex, чтобы они доделали задачу».

Что делает (честно по слоям):
1. СБОР задач: из графа знаний (Task-узлы) + из НАСТРОЕННЫХ задачников
   (yougile/trello/jira) через универсальные meetflow-функции
   (get_configured_task_systems + list_tasks). Best-effort: если meetflow/
   ключи недоступны — берём только граф.
2. КЛАССИФИКАЦИЯ: done / in_progress / blocked / todo + сводка по владельцам.
3. ТЗ: для выбранной невыполненной задачи — генерация ТЗ существующим
   data-driven движком (task_specification) на основе контекста компании.
4. HANDOFF кодинг-агенту: ТЗ записывается в файл-промпт и возвращается
   ГОТОВАЯ команда CLI (`claude -p …` / `cursor-agent …` / `codex exec …`).
   Реальный запуск внешнего агента НЕ автоматизируется здесь: у него своя
   авторизация/песочница/доступ к репозиторию — это сознательная граница
   (оператор запускает команду там, где у агента есть доступ к коду).
   Опционально автозапуск за флагом enable_coding_handoff_exec (default OFF).
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DONE = {"done", "completed", "closed", "resolved", "finished"}
_BLOCKED = {"blocked", "stuck", "on_hold"}
_PROGRESS = {"in_progress", "doing", "active", "wip"}


def _graph_task(nid: Any, d: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "id": d.get("id") or nid,
        "title": d.get("title") or d.get("name") or "",
        "description": d.get("description") or "",
        "status": str(d.get("status") or d.get("state") or "todo").lower(),
        "assignee": d.get("assignee") or d.get("owner") or "",
        "deadline": d.get("deadline") or d.get("due_date") or "",
        "source": "graph",
    }
    if d.get("meeting_id"):
        out["meeting_id"] = d.get("meeting_id")
    return out


async def collect_tasks(user_id: str) -> List[Dict[str, Any]]:
    """Задачи из графа знаний (Task-узлы) — ОБЪЕДИНЁННЫЙ вид (личный ∪ орг).

    Раньше читался только личный networkx-граф: у org-пользователей задачи,
    извлечённые синком встреч, лежат в орг-графе — пульс показывал «открыто 0»,
    хотя встречи исправно порождали Task-узлы (тот же класс бага, что пустой
    список людей в «Партнёрах»). Neo4j-бэкенд тоже поддержан. Best-effort."""
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    except Exception as e:
        logger.warning(f"collect_tasks: merged view unavailable: {e}")
        return []
    tasks: List[Dict[str, Any]] = []
    try:
        nx_g = getattr(gb, "nx_graph", None)
        if nx_g is not None:
            for nid, d in nx_g.nodes(data=True):
                if str(d.get("_label") or d.get("label") or "").lower() != "task":
                    continue
                tasks.append(_graph_task(nid, d))
        else:
            # Neo4j: tenant_context (org_or_user) — как федеративное чтение
            # людей в get_all_people_profiles
            for d in (await gb.get_all_nodes_async(label="Task",
                                                   limit=2000) or []):
                if d.get("id") or d.get("title") or d.get("name"):
                    tasks.append(_graph_task(d.get("id"), d))
    except Exception as e:
        logger.warning(f"collect_tasks failed: {e}")
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass
    return tasks


async def collect_meeting_tasks(user_id: str,
                                meeting_id: str) -> List[Dict[str, Any]]:
    """Задачи КОНКРЕТНОЙ встречи из графа: Task-узлы с ребром CREATED_FROM
    в Meeting-узел этой встречи (так capture привязывает извлечённое).

    Для сценария «кофе»: встреча прошла → выпали задачи → человек выбирает,
    какие отправить в Vibe Tasking. Best-effort: встреча не обработана /
    графа нет → []."""
    try:
        # merged personal ∪ org: задачи из ОБЩИХ встреч живут в орг-графе —
        # личный файл отдавал пусто (тот же класс бага, что collect_tasks)
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=True)
        if not (gb.connected and gb.nx_graph):
            return []
        mid = str(meeting_id).strip()
        # Meeting-узел: по свойству meeting_id или по id узла
        meeting_node = None
        for nid, d in gb.nx_graph.nodes(data=True):
            if str(d.get("_label") or d.get("label") or "").lower() != "meeting":
                continue
            if str(d.get("meeting_id") or "") == mid or str(nid) == mid                     or str(d.get("id") or "") == mid:
                meeting_node = nid
                break
        if meeting_node is None:
            try:
                await gb.close(save=False)
            except Exception:
                pass
            return []
        tasks = []
        for u, v, ed in gb.nx_graph.in_edges(meeting_node, data=True):
            if (ed.get("_type") or ed.get("type")) != "CREATED_FROM":
                continue
            d = gb.nx_graph.nodes[u]
            if str(d.get("_label") or d.get("label") or "").lower() != "task":
                continue
            tasks.append({
                "id": d.get("task_id") or d.get("id") or u,
                "title": d.get("title") or d.get("name") or "",
                "description": d.get("description") or "",
                "status": str(d.get("status") or d.get("state") or "todo").lower(),
                "assignee": d.get("assignee") or d.get("assignee_name")
                            or d.get("owner") or "",
                "deadline": d.get("deadline") or d.get("due_date") or "",
                "source": "meeting",
            })
        try:
            await gb.close(save=False)
        except Exception:
            pass
        return tasks
    except Exception as e:
        logger.warning(f"collect_meeting_tasks failed: {e}")
        return []


def _parse_tasks_json(raw: str) -> List[Dict[str, Any]]:
    """Достать список задач из ответа LLM (JSON-массив, возможно в ```)."""
    import json
    import re
    if not raw:
        return []
    text = raw.strip()
    # снять markdown-обёртку ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        text = m.group(1).strip()
    # вычленить первый массив, если вокруг лишний текст
    if not text.lstrip().startswith("["):
        m2 = re.search(r"\[[\s\S]*\]", text)
        if m2:
            text = m2.group(0)
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(data):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("task") or it.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "id": f"extracted_{i}",
            "title": title[:300],
            "description": str(it.get("description") or "").strip(),
            "status": "todo",
            "assignee": str(it.get("assignee") or it.get("owner") or "").strip(),
            "deadline": str(it.get("deadline") or it.get("due_date") or "").strip(),
            "source": "extracted",
        })
    return out


async def extract_meeting_tasks(user_id: str, meeting_id: str) -> Dict[str, Any]:
    """Извлечь задачи из транскрипта встречи ПО ЗАПРОСУ (кнопка «Извлечь
    задачи», когда встреча ещё не обработана синхронизацией).

    Один LLM-вызов по транскрипту → список action items. Best-effort
    сохраняем как Task-узлы с рёбрами CREATED_FROM → Meeting, чтобы они нашлись
    в collect_meeting_tasks и ушли в Vibe Tasking. Возвращает
    {tasks, total, persisted, note}. Никогда не бросает — при ошибке отдаёт
    понятную note."""
    mid = str(meeting_id).strip()
    title, transcript = "", ""
    # 1. Транскрипт из Supabase (с проверкой владельца).
    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()
        rows = await supabase._request(
            "GET", "/rest/v1/meetings",
            params={"select": "id,title,transcription_text",
                    "id": f"eq.{mid}", "user_id": f"eq.{user_id}", "limit": "1"})
        if rows:
            title = rows[0].get("title") or ""
            transcript = rows[0].get("transcription_text") or ""
    except Exception as e:
        logger.warning(f"extract_meeting_tasks: transcript fetch failed: {e}")
        return {"tasks": [], "total": 0, "persisted": 0,
                "note": "не удалось получить транскрипт встречи"}
    if not (transcript or "").strip():
        return {"tasks": [], "total": 0, "persisted": 0,
                "note": "у встречи нет транскрипта — сначала расшифруйте её, "
                        "потом извлекайте задачи"}

    # 2. LLM извлекает action items (одна модель STANDARD, дёшево).
    system = ("Ты извлекаешь ЗАДАЧИ (action items) из стенограммы встречи. "
              "Верни ТОЛЬКО JSON-массив объектов с полями: title (кратко, "
              "императив), description (детали/контекст), assignee (кто, если "
              "названо), deadline (если названо). Никаких задач не выдумывай — "
              "только то, что реально прозвучало как поручение/договорённость. "
              "Если задач нет — верни [].")
    user_prompt = f"Встреча: {title}\n\nСтенограмма:\n{transcript[:24000]}"
    try:
        from backend.core.llm.router import LLMRouter, ModelTier
        router = LLMRouter()
        raw = await router.generate(prompt=user_prompt, system_prompt=system,
                                    model_tier=ModelTier.STANDARD, max_tokens=2000)
    except Exception as e:
        logger.warning(f"extract_meeting_tasks: LLM failed: {e}")
        return {"tasks": [], "total": 0, "persisted": 0,
                "note": "не удалось извлечь задачи (LLM недоступен)"}

    tasks = _parse_tasks_json(raw)
    if not tasks:
        return {"tasks": [], "total": 0, "persisted": 0,
                "note": "в стенограмме не нашлось явных задач"}

    # 3. Best-effort: сохранить как Task-узлы (чтобы нашлись и в след. раз).
    persisted = 0
    try:
        persisted = await _persist_extracted_tasks(user_id, mid, title, tasks)
    except Exception as e:
        logger.warning(f"extract_meeting_tasks: persist failed (tasks still returned): {e}")

    return {"tasks": tasks, "total": len(tasks), "persisted": persisted,
            "note": f"извлечено задач: {len(tasks)}"}


async def _persist_extracted_tasks(user_id: str, meeting_id: str, title: str,
                                   tasks: List[Dict[str, Any]]) -> int:
    """Сохранить извлечённые задачи как Task-узлы с CREATED_FROM → Meeting в
    графе пользователя (так их найдёт collect_meeting_tasks). Best-effort."""
    import uuid as _uuid

    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user
    gb = GraphBuilder(use_networkx=None,
                      graph_storage_path=graph_path_for_user(user_id))
    await gb.connect()
    if not gb.connected:
        return 0
    # Meeting-узел (создаём, если встречи ещё нет в графе).
    meeting_node = await gb._merge_node(
        "Meeting", "meeting_id", meeting_id,
        {"meeting_id": meeting_id, "title": title, "access_group": "public"})
    count = 0
    for t in tasks:
        tid = str(_uuid.uuid4())
        task_node = await gb._merge_node(
            "Task", "task_id", tid,
            {"task_id": tid, "title": t["title"],
             "description": t.get("description", ""),
             "status": "todo", "assignee": t.get("assignee", ""),
             "deadline": t.get("deadline", ""),
             "meeting_id": meeting_id, "access_group": "public",
             "origin": "extracted_on_demand"})
        if task_node and meeting_node:
            await gb._create_relationship(task_node, meeting_node,
                                          "CREATED_FROM", {})
            count += 1
    try:
        gb.save_graph()
    except Exception:
        pass
    try:
        await gb.close(save=True)
    except Exception:
        pass
    return count


async def collect_tasks_from_trackers(user_id: str) -> List[Dict[str, Any]]:
    """Задачи из НАСТРОЕННЫХ задачников (yougile/trello/jira) через meetflow.

    Переиспользует те же универсальные функции, что task_actions. Best-effort:
    нет meetflow/ключей → []. Нормализует к общему виду задачи."""
    import json as _json
    try:
        from backend.core.tasks.task_actions import _load_meetflow
        mf = _load_meetflow(user_id)
    except Exception as e:
        logger.debug(f"trackers: meetflow unavailable: {e}")
        return []
    out: List[Dict[str, Any]] = []
    try:
        raw = await mf.get_configured_task_systems()
        systems_data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        systems = [s.get("system") for s in (systems_data.get("systems") or [])
                   if s.get("system")]
    except Exception as e:
        logger.debug(f"trackers: get_configured_task_systems failed: {e}")
        return []
    for system in systems:
        try:
            raw = await mf.list_tasks(system=system, limit=100)
            data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            for t in (data.get("tasks") or []):
                if not isinstance(t, dict):
                    continue
                out.append({
                    "id": t.get("id") or t.get("task_id") or "",
                    "title": t.get("title") or t.get("name") or "",
                    "description": t.get("description") or "",
                    "status": str(t.get("status") or "todo").lower(),
                    "assignee": t.get("assignee") or t.get("owner") or "",
                    "deadline": t.get("deadline") or t.get("due_date") or "",
                    "source": system,
                    "tracker": system,
                    "tracker_task_id": t.get("id") or t.get("task_id") or "",
                })
        except Exception as e:
            logger.warning(f"trackers: list_tasks({system}) failed: {e}")
    return out


def _bucket(status: str) -> str:
    # Единый словарь (status_norm) — раньше здесь был свой, третий по счёту,
    # и «выполнена»/«готово» из русскоязычных трекеров считались открытыми.
    from backend.core.tasks.status_norm import normalize_status
    b = normalize_status(status)
    return "todo" if b == "deferred" else b


async def analyze_tasks(user_id: str) -> Dict[str, Any]:
    """Сводка: сколько выполнено/нет, по владельцам, что застряло.

    Источники: граф знаний + настроенные задачники (yougile/trello/jira)."""
    tasks = await collect_tasks(user_id)
    tasks += await collect_tasks_from_trackers(user_id)
    # История дедлайнов для детектора переносов («задача переносится и не
    # делается»): каждый анализ — замер. Best-effort, отчётов не ломает.
    try:
        from backend.core.reports.deadline_tracker import record_deadlines
        record_deadlines(user_id, tasks)
    except Exception:
        logger.debug("deadline history record skipped", exc_info=True)
    buckets: Dict[str, list] = {"done": [], "in_progress": [], "blocked": [], "todo": []}
    by_owner: Dict[str, Dict[str, int]] = {}
    for t in tasks:
        b = _bucket(t["status"])
        buckets[b].append(t)
        owner = t.get("assignee") or "—"
        by_owner.setdefault(owner, {"done": 0, "open": 0})
        if b == "done":
            by_owner[owner]["done"] += 1
        else:
            by_owner[owner]["open"] += 1
    total = len(tasks)
    done = len(buckets["done"])
    by_source: Dict[str, int] = {}
    for t in tasks:
        by_source[t.get("source", "graph")] = by_source.get(t.get("source", "graph"), 0) + 1
    return {
        "total": total,
        "done": done,
        "open": total - done,
        "completion_rate": round(100 * done / total) if total else 0,
        "counts": {k: len(v) for k, v in buckets.items()},
        "by_source": by_source,
        "blocked": buckets["blocked"],
        "by_owner": by_owner,
        "tasks": tasks,
    }


# ============================================================================
# ТЗ + handoff кодинг-агенту
# ============================================================================

# Базовые команды исполнителей. ТЗ передаём НЕ через `$(cat ...)` (это bash-изм:
# на Windows/cmd.exe нет ни `$(...)`, ни `cat`, ни POSIX-кавычек shlex — из-за
# чего запуск на Windows-сервере просто падал), а через stdin — тем же путём,
# что и рабочий ClaudeCodeCLIExecutor. Так handoff запускается и на Linux,
# и на Windows.
_AGENT_COMMANDS = {
    "claude": "claude -p",
    "cursor": "cursor-agent -p",
    "codex": "codex exec",
    # xAI Grok CLI (irm https://x.ai/cli/install.ps1 | iex) и Qwen Code —
    # headless-режим, ТЗ уходит в stdin тем же путём, что и остальные.
    "grok": "grok -p",
    "qwen": "qwen -p",
    # Kimi Code CLI (Moonshot, K3): headless print-режим, ТЗ в stdin.
    "kimi": "kimi -p",
}
# Исполнители БЕЗ правки кода: оператор добавляет/переопределяет команды
# через env, напр. TESSENT_AGENT_COMMANDS_JSON='{"grok": "grok --prompt",
# "aider": "aider --yes-always --message-file -"}'. Ключ — имя агента в UI,
# значение — headless-команда CLI (ТЗ подаётся в stdin).
try:
    _extra_cmds = os.getenv("TESSENT_AGENT_COMMANDS_JSON", "").strip()
    if _extra_cmds:
        import json as _json
        _AGENT_COMMANDS.update(
            {str(k): str(v) for k, v in _json.loads(_extra_cmds).items() if v})
except Exception:
    logger.warning("TESSENT_AGENT_COMMANDS_JSON не разобран — игнорирую",
                   exc_info=True)

# АВТОНОМНАЯ ЗАПИСЬ в headless. `claude -p` (и другие CLI) в неинтерактивном
# режиме не могут показать диалог «Allow», поэтому Write/Edit блокируются, и
# агент физически не может сохранить готовый результат (КП/финмодель) файлом —
# в этом причина «файлы не записаны». Гейт человека УЖЕ пройден (confirm перед
# запуском), а исполнение ограничено рабочей папкой (TESSENT_HANDOFF_REPO_ROOT),
# поэтому оператор может разрешить автономную запись. Дефолт пуст → прежнее
# поведение (спросит и в headless упрётся — ничего не ломаем). Значения:
#   acceptEdits       — авто-приём правок файлов (Write/Edit), команды по-прежнему
#                       гейтятся (достаточно для контентных ТЗ: сохранить .md/.docx);
#   bypassPermissions — авто и файлы, и команды (для кодовых ТЗ со сборкой/тестами).
# Для не-claude CLI флаг задаётся своим синтаксисом через TESSENT_AGENT_COMMANDS_JSON.
try:
    _perm_mode = (os.getenv("TESSENT_HANDOFF_PERMISSION_MODE", "") or "").strip()
    if _perm_mode and _AGENT_COMMANDS.get("claude") == "claude -p":
        _AGENT_COMMANDS["claude"] = f"claude -p --permission-mode {_perm_mode}"
        logger.info("handoff: claude в автономном режиме записи "
                    "(--permission-mode %s)", _perm_mode)
except Exception:
    logger.debug("handoff permission-mode setup skipped", exc_info=True)


async def generate_task_spec(user_id: str, task: Dict[str, Any]) -> str:
    """ТЗ по задаче через data-driven движок (на контексте компании).

    Падение движка → структурный фолбэк (само ТЗ из полей задачи), чтобы
    handoff всегда был возможен."""
    desc = (f"{task.get('title', '')}. {task.get('description', '')}").strip()
    # Self-Grown: принятые плейбуки компании — в контекст ТЗ, чтобы новые
    # задачи сразу учитывали выученное на прошлых доработках. Best-effort.
    try:
        from backend.core.tasks.lessons import playbooks_context_block
        _pb = playbooks_context_block(user_id)
        if _pb:
            desc = f"{desc}\n\n{_pb}"
    except Exception:
        logger.debug("playbooks context skipped", exc_info=True)
    try:
        # переиспользуем ту же per-user систему, что /tasks/process
        from litestar.datastructures import State

        from backend.api.routes.agents import get_task_spec_system
        system = await get_task_spec_system(State({}), user_id=user_id)
        # user_id ОБЯЗАТЕЛЕН в контексте: без него движок не активирует
        # premium-модель (tz_generation) — падал на flash-lite — и не подмешивал
        # числа из онтологии/поиск по документам. Раньше не передавался →
        # ТЗ считалось слабой моделью и «не находило» цифры.
        result = await system.process_task(
            desc, additional_context={"user_id": user_id})
        # Движок кладёт готовое ТЗ в stages.result.markdown (см.
        # DataDrivenTaskSystem._stage_6_generate_result). Прежние ключи
        # (specification/spec/content) в результате не существуют — из-за чего
        # handoff всегда падал в структурный фолбэк. Читаем правильный путь,
        # сохраняя обратную совместимость со старыми верхнеуровневыми ключами.
        spec = ""
        if isinstance(result, dict):
            stages = result.get("stages")
            if isinstance(stages, dict):
                stage_res = stages.get("result")
                if isinstance(stage_res, dict):
                    spec = stage_res.get("markdown") or ""
            if not spec:
                spec = (result.get("specification") or result.get("spec")
                        or result.get("content")
                        or result.get("task_specification") or "")
        if spec:
            return spec if isinstance(spec, str) else str(spec)
    except Exception as e:
        logger.warning(f"generate_task_spec: movement engine failed: {e}")
    # фолбэк
    return (f"# Задача: {task.get('title', '')}\n\n"
            f"{task.get('description', '')}\n\n"
            f"Исполнитель: {task.get('assignee', '—')}\n"
            f"Срок: {task.get('deadline', '—')}\n\n"
            "Доработай реализацию согласно описанию. Если нужны уточнения по "
            "контексту компании — задай вопросы.")


async def coding_handoff(user_id: str, task: Dict[str, Any], *,
                         agent: str = "claude",
                         repo_path: Optional[str] = None,
                         spec_text: Optional[str] = None,
                         source: Optional[Dict[str, Any]] = None,
                         artifact_mode: bool = False,
                         kanon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """ФАЗА 1: сгенерировать ТЗ + команду и создать PENDING-хэндофф.

    НИЧЕГО не исполняет. Запуск кодинг-агента происходит ТОЛЬКО после
    явного подтверждения пользователя — confirm_handoff(handoff_id)
    (плюс ops-рубильник enable_coding_handoff_exec). Возвращает запись с
    handoff_id, командой и requires_confirmation=true.

    spec_text — готовое ТЗ (например, generatedTZ из SIMA): генерация
    пропускается, текст уходит агенту как есть.

    kanon — метаданные Kanon контроль-лупа (SIMA P3): {project_id,
    block_id?, run_commands, iteration}. Если заданы — после прогона
    confirm_handoff сам верифицирует результат по контрактам и при провале
    готовит рефайн-handoff (см. core/sima/kanon_loop)."""
    if agent not in _AGENT_COMMANDS:
        raise ValueError(f"agent must be one of {list(_AGENT_COMMANDS)}")
    if not spec_text:
        spec_text = await generate_task_spec(user_id, task)

    d = tempfile.mkdtemp(prefix="tessent_handoff_")
    # Режим сборки артефакта БЕЗ репо: агент должен ПОЛОЖИТЬ файл-результат
    # (презентация/КП/таблица) в ТЕКУЩУЮ папку — иначе собирать нечего.
    # Директиву добавляем к ТЗ; папка исполнения = scratch d (см. work_dir).
    if artifact_mode and not repo_path:
        spec_text = spec_text.rstrip() + (
            "\n\n---\n## Формат сдачи (ВАЖНО)\n"
            "Ты работаешь в изолированной РАБОЧЕЙ ПАПКЕ. СОБЕРИ готовый файл-"
            "результат и СОХРАНИ его в ТЕКУЩУЮ папку (`./`): презентацию — как "
            "самодостаточный `presentation.html` (инлайн CSS/JS, без внешних "
            "ссылок), расчёты/КП — как `.xlsx`/`.csv` или `.html` с таблицей, "
            "документ — как `.html`/`.md`/`.pdf`. Не нужен git-репозиторий и "
            "прод-структура — только сам файл(ы) результата в текущей папке.")
    spec_file = os.path.join(d, "task_spec.md")
    with open(spec_file, "w", encoding="utf-8") as f:
        f.write(spec_text)

    exec_command = _AGENT_COMMANDS[agent]  # ТЗ уйдёт в stdin при запуске
    # work_dir исполнения: для artifact_mode — scratch-папка d (агент пишет
    # результат туда, мы его собираем); для репо-режима — сам repo_path.
    work_dir = d if (artifact_mode and not repo_path) else None
    # Человекочитаемая команда для ручного запуска. `< spec_file` (stdin из
    # файла) работает и в bash, и в cmd.exe, и в PowerShell — в отличие от
    # прежнего `$(cat ...)`.
    _run_dir = repo_path or work_dir
    display_command = (f"{exec_command} < {spec_file}" if not _run_dir
                       else f"cd {_run_dir} && {exec_command} < {spec_file}")

    from backend.core.tasks.handoff_store import HandoffStore
    rec = HandoffStore(user_id).create({
        "agent": agent,
        "task_title": task.get("title", ""),
        "tracker": task.get("tracker") or task.get("system"),
        "tracker_task_id": task.get("tracker_task_id") or task.get("external_id"),
        # владелец задачи — в отчёт задачника («чья задача выполнена»)
        "assignee": task.get("assignee") or task.get("owner") or "",
        "spec_text": spec_text,
        "spec_file": spec_file,
        "command": display_command,
        "exec_command": exec_command,
        "repo_path": repo_path,
        "artifact_mode": bool(artifact_mode and not repo_path),
        "work_dir": work_dir,
        "kanon": kanon or None,  # P3 контроль-луп: метаданные для авто-verify
        # происхождение задачи (метка «откуда»): meeting/manual/… — для очереди
        "source": source if isinstance(source, dict) and source else None,
    })
    rec["requires_confirmation"] = True
    rec["how_to_confirm"] = (
        "POST /task-analysis/handoff/{id}/confirm — запуск; "
        "/reject — отклонить. CLI: tessent handoff-confirm <id>.")
    return rec


async def _verify_handoff_result(user_id: str, spec_text: str, repo_path: str,
                                 exec_res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Авто-проверка результата исполнения против ТЗ (замыкание vibe tasking).

    Evidence: git-диф в рабочей папке (что реально изменилось) + хвост вывода
    исполнителя. LLM-судья (маршрут тенанта, premium через workload_policy,
    с Google-страховкой) → {"verdict": "done"|"needs_work", "summary", "missing"}.
    Never-raise; нет ключей/LLM → None (цикл живёт без судьи).
    Авто-рефайна НЕТ — гейт человека сохраняется, вердикт лишь подсвечивается."""
    import asyncio as _aio

    async def _git(*args: str) -> str:
        try:
            p = await _aio.create_subprocess_exec(
                "git", "-C", repo_path, *args,
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL)
            out, _ = await _aio.wait_for(p.communicate(), timeout=20)
            return (out or b"").decode("utf-8", "replace")[:3000]
        except Exception:
            return ""

    diff_stat = await _git("diff", "--stat", "HEAD")
    status = await _git("status", "--porcelain")
    output_tail = str(exec_res.get("output") or "")[-2000:]

    prompt = (
        "Ты — проверяющий результат работы кодинг-агента по ТЗ. Оцени, ВЫПОЛНЕНО ли "
        "ТЗ, по фактическим изменениям и выводу. Верни СТРОГО JSON: "
        '{"verdict": "done"|"needs_work", "summary": "1-2 предложения по-русски: '
        'что сделано", "missing": ["чего не хватает (пусто, если всё сделано)"]}\n\n'
        f"=== ТЗ (фрагмент) ===\n{(spec_text or '')[:4000]}\n\n"
        f"=== GIT DIFF --STAT ===\n{diff_stat or '(diff пуст)'}\n\n"
        f"=== GIT STATUS ===\n{status or '(чисто)'}\n\n"
        f"=== ХВОСТ ВЫВОДА ИСПОЛНИТЕЛЯ ===\n{output_tail}\n")
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        raw = await generate_for_workload(user_id, "tz_generation", prompt)
        if not raw:
            return None
        from backend.core.capture.tiered_extraction import parse_json_robust
        data = parse_json_robust(raw)
        verdict = str(data.get("verdict") or "").lower()
        if verdict not in ("done", "needs_work"):
            return None
        return {"verdict": verdict,
                "summary": str(data.get("summary") or "")[:500],
                "missing": [str(m)[:200] for m in (data.get("missing") or [])[:10]],
                "diff_stat": diff_stat[-800:]}
    except Exception:
        logger.debug("handoff verify LLM skipped", exc_info=True)
        return None


# Ссылки на фоновые таски исполнения — чтобы их не собрал GC до завершения
# (asyncio держит только weakref). Снимается в done-callback.
_BG_HANDOFF_TASKS: set = set()


# Директива-обёртка для исполнителя (claude -p / OpenHands / artifact). Раньше
# в stdin уходил «голый» ТЗ/документ, и агент, получив, например, готовую
# финмодель, отвечал «а что с этим сделать?» и задавал уточняющие вопросы —
# вместо готового результата. Обёртка кадрирует ввод как ЗАДАЧУ на исполнение.
# Generic (без привязки к конкретной задаче), идемпотентна.
_EXECUTOR_DIRECTIVE = (
    "Ниже — ТЗ/задача. Выполни её ДО КОНЦА и создай ГОТОВЫЙ ФИНАЛЬНЫЙ АРТЕФАКТ "
    "— такой, который человек может сразу отдать клиенту/руководителю без "
    "доработки. НЕ задавай уточняющих вопросов и НЕ проси инструкций: при "
    "нехватке данных прими разумные допущения и явно пометь их как [допущение]. "
    "НЕ пересказывай задачу и НЕ пиши план — сразу делай.\n"
    "\n"
    "КРИТИЧНО — ФОРМАТ И КАЧЕСТВО АРТЕФАКТА. Сырой markdown/plain-text — НЕ "
    "финальный артефакт (кроме случая, когда ТЗ явно просит markdown). Сам "
    "определи по задаче правильный формат и СОЗДАЙ его на уровне готового "
    "продукта:\n"
    "- КП, лендинг, отчёт, документ для клиента → self-contained HTML с "
    "  продуманным дизайном: инлайн-CSS, типографика, палитра, сетка, "
    "  оформленные таблицы, шапка/подвал, print-CSS (для сохранения в PDF).\n"
    "- Презентация → настоящие слайды: собери .pptx кодом (python-pptx: "
    "  титульный, единая тема, таблицы, крупные тезисы — не полотно текста) "
    "  и/или HTML-слайды (полноэкранные секции). Не «текст, переведённый в "
    "  формат», а спроектированные слайды.\n"
    "- Финмодель, расчёт → .xlsx с реальными таблицами и ФОРМУЛАМИ (openpyxl) "
    "  + пояснительный документ (.docx через python-docx или styled HTML).\n"
    "- Код → работающий код в репозитории/папке: файлы, README с запуском, "
    "  зависимости; если просили архив — собери архив.\n"
    "- Диаграмма/схема → рабочая диаграмма: mermaid/SVG/graphviz, "
    "  отрендеренная или готовая к рендеру, не текстовое описание.\n"
    "Нужных библиотек нет — установи (pip install ...) или сгенерируй формат "
    "напрямую (xml/ooxml), но НЕ подменяй формат заглушкой из markdown.\n"
    "\n"
    "Все файлы СОХРАНИ в рабочей папке с говорящими именами. В конце перечисли "
    "созданные файлы и что в каждом. Дизайн — сдержанный и профессиональный: "
    "одна акцентная палитра, выравнивание, без пестроты.\n"
    "\n"
    "ОТПРАВКА. Если задача включает отправку результата (письмо/КП/сообщение) — "
    "сам НЕ отправляй: это необратимое внешнее действие. Если доступен почтовый "
    "инструмент и адресат ЯВНО указан в ТЗ — создай ЧЕРНОВИК (draft) с темой, "
    "текстом письма и упоминанием вложений, и укажи это в итоге. Адресата нет — "
    "приложи готовый текст письма отдельным файлом (email_draft.md) для "
    "отправки человеком.\n"
    "\n===== ТЗ =====\n"
)


def _task_report_context(running: Dict[str, Any]) -> str:
    """Шапка отчёта в задачник: ЧЬЯ задача и ОТКУДА она пришла.

    Комментарий в трекере раньше был безликим («Выполнено кодинг-агентом»)
    — по нему не видно, к какой задаче/встрече/владельцу относится работа.
    Собираем контекст из handoff-записи; пустые поля пропускаем."""
    lines = []
    title = (running.get("task_title") or "").strip()
    if title:
        lines.append(f"Задача: {title}")
    assignee = (running.get("assignee") or "").strip()
    if assignee:
        lines.append(f"Исполнитель/владелец: {assignee}")
    src = running.get("source") or {}
    if isinstance(src, dict) and src.get("kind") == "meeting":
        mt = (src.get("meeting_title") or "").strip()
        lines.append(f"Источник: встреча «{mt}»" if mt else "Источник: встреча")
    agent = (running.get("agent") or "").strip()
    if agent:
        lines.append(f"Агент: {agent}")
    return ("\n".join(lines) + "\n\n") if lines else ""


async def notify_external_webhook(user_id: str, event: str,
                                  payload: Dict[str, Any]) -> None:
    """Исходящий webhook для внешних систем (Minitest): события пульта.

    Env TESSENT_HANDOFF_WEBHOOK_URL → POST JSON {event, user_id, ...payload}.
    Опциональный TESSENT_HANDOFF_WEBHOOK_TOKEN уходит заголовком
    X-Tessbrain-Token (получатель сверяет). Пусто → no-op. Best-effort/never-
    raise: недоступность получателя не влияет на основной поток.
    События: handoff_completed (итог исполнения), meeting_tasks_ready
    (встреча синкнута, задачи извлечены — можно показывать пульт)."""
    url = (os.getenv("TESSENT_HANDOFF_WEBHOOK_URL", "") or "").strip()
    if not url:
        return
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        token = (os.getenv("TESSENT_HANDOFF_WEBHOOK_TOKEN", "") or "").strip()
        if token:
            headers["X-Tessbrain-Token"] = token
        body = {"event": event, "user_id": user_id, **payload}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                logger.warning("external webhook %s (%s) → HTTP %s",
                               url, event, r.status_code)
    except Exception as e:
        logger.warning(f"external webhook ({event}) failed: {e}")


async def _notify_completion_webhook(user_id: str, payload: Dict[str, Any]) -> None:
    """Совместимость: рапорт о завершении handoff (см. notify_external_webhook)."""
    await notify_external_webhook(user_id, "handoff_completed", payload)


async def _drive_links_note(user_id: str, paths: List[str]) -> str:
    """Внешний диск: загрузить готовые файлы на Google Drive пользователя и
    вернуть блок ссылок для комментария в задаче. За гейтом
    TESSENT_TRACKER_DRIVE_LINKS=on (дефолт OFF: у readonly-токена каждый DONE
    сыпал бы 403). Best-effort: сбой → пустая строка, финализацию не трогаем."""
    if (os.getenv("TESSENT_TRACKER_DRIVE_LINKS", "") or "").lower() not in (
            "on", "true", "1"):
        return ""
    if not paths:
        return ""
    try:
        from backend.integrations.google_drive_integration import (
            upload_files_to_drive,
        )
        res = await upload_files_to_drive(user_id, paths)
        if not res.get("links"):
            if res.get("message") or res.get("errors"):
                logger.warning("drive links skipped: %s",
                               res.get("message") or res.get("errors"))
            return ""
        lines = ["", "☁️ Google Drive:"]
        for l in res["links"]:
            lines.append(f"- {l['file']}: {l['url']}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"drive links failed: {e}")
        return ""


def _artifact_links_note(handoff_id: str, artifacts: List[Dict[str, Any]]) -> str:
    """Ссылки на скачивание артефактов через НАШ бэкенд (per-user изоляция в
    самом роуте) — если оператор задал публичный адрес TESSENT_PUBLIC_BASE_URL.
    Это и есть «загрузить на диск и дать ссылку», только диском выступает сам
    мозг: работает для ВСЕХ задачников (в т.ч. YouGile без file-API). Пусто →
    без ссылок (поведение прежнее)."""
    base = (os.getenv("TESSENT_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base or not artifacts:
        return ""
    lines = ["", "🔗 Скачать:"]
    for i, a in enumerate(artifacts):
        lines.append(
            f"- {a.get('name')}: {base}/api/v1/task-analysis/handoff/"
            f"{handoff_id}/artifact/{i}")
    return "\n".join(lines)


def _wrap_spec_for_executor(spec_text: str) -> str:
    """Кадрировать ТЗ как задачу на исполнение. Идемпотентно (не дублируем при
    повторных прогонах/рефайнах). Пустой ввод не трогаем."""
    st = (spec_text or "").strip()
    if not st or "===== ТЗ =====" in st:
        return st
    return _EXECUTOR_DIRECTIVE + st


async def _run_handoff_execution(user_id: str, handoff_id: str,
                                 running: Dict[str, Any], rp: str,
                                 exec_command: str, spec_text: str,
                                 env_overrides: Dict[str, str],
                                 store: Any) -> Dict[str, Any]:
    """Собственно прогон исполнителя + финализация (verify/lessons/WS/attach).

    Выделено из confirm_handoff, чтобы UI-путь мог гонять это в фоне
    (asyncio-таск), а внутренний kanon-путь — синхронно (await). Тело не
    изменено — только вынесено. Never-raise наружу для фонового режима:
    все ветки best-effort, статус пишется в store."""
    from backend.core.tasks.handoff_store import DONE, FAILED, RUNNING
    import time as _time
    _run_started = _time.time()
    res = await _exec_handoff(exec_command, rp, env_overrides=env_overrides,
                              stdin_text=_wrap_spec_for_executor(spec_text) or None)
    final = DONE if res.get("rc") == 0 else FAILED
    store.transition(handoff_id, RUNNING, final,
                     rc=res.get("rc"), output_tail=res.get("output", ""))

    out = {"status": "success" if final == DONE else "failed",
           "handoff_id": handoff_id, **res}

    # ─── Замыкание цикла vibe tasking: авто-проверка + доставка ───
    # (1) verify: rc==0 ≠ «ТЗ выполнено» — LLM-судья сверяет git-диф и вывод
    #     с ТЗ, вердикт пишется в запись (авто-рефайна нет, гейт человека).
    _verify = None
    if final == DONE:
        try:
            _verify = await _verify_handoff_result(user_id, spec_text, rp, res)
            if _verify:
                store.update(handoff_id,
                             verify_verdict=_verify["verdict"],
                             verify_summary=_verify["summary"],
                             verify_missing=_verify["missing"])
                out["verify"] = _verify
        except Exception as e:
            logger.warning(f"handoff verify skipped: {e}")

    # урок Self-Grown (перенос OpenOPC): исход атрибутируется исполнителю,
    # накопленные уроки кристаллизуются в плейбуки (lessons.py). Best-effort.
    try:
        from backend.core.tasks.lessons import append_lesson
        append_lesson(user_id, task_title=running.get("task_title") or "",
                      agent=running.get("agent") or "",
                      verdict=(_verify or {}).get("verdict")
                      or ("done" if final == DONE else "failed"),
                      missing=(_verify or {}).get("missing") or [],
                      summary=(_verify or {}).get("summary") or "",
                      attempt=int(running.get("attempt") or 1),
                      handoff_id=handoff_id, kind="code")
    except Exception:
        logger.debug("handoff lesson skipped", exc_info=True)

    # (2) deliver: сигнал в UI (signals WS) — пользователь видит завершение и
    #     вердикт сразу, без поллинга очереди. Fire-and-forget, never-raise.
    try:
        from backend.api.websocket.signals_ws import publish_signal
        publish_signal("handoff_completed", {
            "handoff_id": handoff_id,
            "status": final,
            "rc": res.get("rc"),
            "title": running.get("task_title") or "",
            "verdict": (_verify or {}).get("verdict"),
            "summary": ((_verify or {}).get("summary")
                        or (res.get("output") or "")[-400:]),
        }, tenant_id=user_id)
    except Exception:
        logger.debug("handoff completion signal skipped", exc_info=True)

    # (2b) внешние системы (Minitest): webhook о завершении с итогами —
    #      статус, вердикт, задачник, владелец, встреча-источник.
    await _notify_completion_webhook(user_id, {
        "handoff_id": handoff_id,
        "status": final,
        "rc": res.get("rc"),
        "title": running.get("task_title") or "",
        "assignee": running.get("assignee") or "",
        "verdict": (_verify or {}).get("verdict"),
        "summary": ((_verify or {}).get("summary")
                    or (res.get("output") or "")[-400:]),
        "tracker": running.get("tracker") or "",
        "tracker_task_id": running.get("tracker_task_id") or "",
        "source": running.get("source") or None,
        "executed_as": "code",
    })

    # P3 Kanon контроль-луп: если это SIMA-handoff с контрактами — сразу
    # верифицируем результат агента и при провале готовим рефайн (гейт
    # человека сохраняется, если KANON_AUTO_REFINE не включён). Never-raise.
    if final == DONE and running.get("kanon"):
        try:
            from backend.core.sima.kanon_loop import verify_after_run
            kanon_result = await verify_after_run(user_id, running, rp)
            if kanon_result:
                out["kanon"] = kanon_result
        except Exception as e:
            logger.warning(f"handoff kanon verify failed: {e}")

    # результат — обратно в задачу задачника (недеструктивно)
    tracker, tracker_id = running.get("tracker"), running.get("tracker_task_id")
    if final == DONE and tracker and tracker_id:
        # ГОТОВЫЕ ФАЙЛЫ — в задачу задачника. Деливераблы = «презентабельные»
        # файлы (html/pdf/pptx/xlsx/docx/…), созданные/изменённые ЗА ЭТОТ прогон
        # (min_mtime), а не весь репозиторий. trello/jira — настоящие вложения;
        # yougile file-API не имеет → файлы перечисляются текстом в комментарии.
        # Kill-switch: TESSENT_TRACKER_ATTACH_FILES=off. Best-effort/never-raise.
        _files_note = ""
        _deliverables: List[Dict[str, Any]] = []
        try:
            if (os.getenv("TESSENT_TRACKER_ATTACH_FILES", "on") or "").lower() \
                    not in ("off", "false", "0"):
                _deliverables = [
                    a for a in _collect_artifacts(rp, max_files=8,
                                                  min_mtime=_run_started)
                    if not a["name"].lower().endswith(".json")][:5]
                if _deliverables:
                    from backend.core.tasks.task_actions import attach_files
                    att = await attach_files(
                        user_id, tracker, tracker_id,
                        [a["path"] for a in _deliverables])
                    out["task_files"] = att
                    if att.get("attached"):
                        _files_note = "\n\n📎 Приложены файлы: " + ", ".join(
                            f["file"] for f in att["attached"])
                    elif att.get("files_unsupported"):
                        _files_note = ("\n\n📎 Готовые файлы (в задачнике "
                                       "вложения не поддержаны API): "
                                       + ", ".join(a["name"]
                                                   for a in _deliverables))
                    _files_note += await _drive_links_note(
                        user_id, [a["path"] for a in _deliverables])
        except Exception as e:
            logger.warning(f"handoff attach_files failed: {e}")
        try:
            from backend.core.tasks.task_actions import attach_result
            out["task_attach"] = await attach_result(
                user_id, tracker, tracker_id,
                result_text=_task_report_context(running)
                + (res.get("output") or "")[-3000:] + _files_note,
                title="Выполнено кодинг-агентом (по подтверждению)")
        except Exception as e:
            logger.warning(f"handoff attach_result failed: {e}")
    else:
        # Без привязки к трекеру: авто-доставка по маршруту по умолчанию
        # (если пользователь его сохранил; иначе — прежнее поведение).
        from backend.core.tasks.handoff_store import HandoffStore as _HS
        _auto = await _auto_route_result(user_id, handoff_id,
                                         _HS(user_id).get(handoff_id) or {})
        if _auto:
            out["auto_delivery"] = _auto
    return out


async def confirm_handoff(user_id: str, handoff_id: str, *,
                          repo_path: Optional[str] = None,
                          background: bool = False) -> Dict[str, Any]:
    """ФАЗА 2: ЯВНОЕ подтверждение пользователя → реальный запуск агента.

    Требует: запись в статусе pending_confirmation (двойной confirm
    блокируется атомарным переходом) + ops-флаг enable_coding_handoff_exec
    + repo_path (из записи или аргумента). Результат пишется в запись;
    при успехе — attach_result обратно в задачу задачника.

    background=True (UI-путь): после перехода в RUNNING исполнитель гоняется
    в ФОНЕ (asyncio-таск), а ответ возвращается сразу {status: running}. Иначе
    HTTP-запрос висел бы до 30 мин (синхронный `claude -p`), и фронт «терял»
    карточку. Статус/дифф подтягиваются по завершении (WS-сигнал
    handoff_completed + перечитывание очереди). Внутренние вызовы (kanon-луп)
    оставляют background=False — им нужен результат синхронно."""
    from backend.core.tasks.handoff_store import (
        DONE,
        FAILED,
        PENDING,
        RUNNING,
        HandoffStore,
    )
    store = HandoffStore(user_id)
    rec = store.get(handoff_id)
    if not rec:
        return {"status": "error", "message": f"handoff {handoff_id} не найден"}

    try:
        from backend.core.config.feature_flags import get_feature_flags
        allowed = bool(get_feature_flags().enable_coding_handoff_exec)
    except Exception:
        allowed = False
    if not allowed:
        return {"status": "error", "handoff_id": handoff_id,
                "message": ("исполнение выключено оператором "
                            "(enable_coding_handoff_exec=false) — запусти "
                            "команду вручную: " + rec.get("command", ""))}

    # Режим сборки артефакта БЕЗ репо: агент запускается в изолированной
    # scratch-папке и создаёт файл-результат, который мы собираем и отдаём.
    # Отдельный рубильник (свой профиль риска) — гейт человека сохранён.
    if bool(rec.get("artifact_mode")) and not (repo_path or rec.get("repo_path")):
        return await _confirm_artifact_handoff(user_id, handoff_id, rec, store)

    rp = repo_path or rec.get("repo_path")
    if not rp:
        return {"status": "error", "handoff_id": handoff_id,
                "message": ("Это контентное ТЗ без репозитория — кодинг-исполнитель "
                            "(Claude Code/Codex) не запускается. Документ уже готов: "
                            "нажмите «Принять результат», чтобы закрыть задачу (ТЗ = "
                            "результат), или «Показать ТЗ» для просмотра. Для КОДОВОГО "
                            "ТЗ укажите существующий путь к git-репозиторию на сервере "
                            "в поле «рабочая папка».")}

    # SaaS-защита: repo_path — путь на СЕРВЕРЕ, и без ограничения любой
    # пользователь мог бы натравить исполнителя на чужую папку (другого
    # тенанта, системную директорию). Оператор задаёт разрешённый корень
    # TESSENT_HANDOFF_REPO_ROOT — исполнение только внутри него. Пусто →
    # прежнее поведение (локальная/односерверная установка).
    # Единая проверка зоны исполнения (см. handoff_path_violation): строгий
    # allowlist при заданном TESSENT_HANDOFF_REPO_ROOT, иначе базовый запрет
    # системных/секретных каталогов. Здесь — чтобы человек увидел причину сразу,
    # ДО перехода в running; в _exec_handoff стоит дубль-заслон для всех путей.
    _violation = handoff_path_violation(rp)
    if _violation:
        return {"status": "error", "handoff_id": handoff_id, "message": _violation}

    # атомарный переход pending → running (защита от двойного confirm)
    running = store.transition(handoff_id, PENDING, RUNNING,
                               repo_path=rp, confirmed_by=user_id)
    if running is None:
        cur = (store.get(handoff_id) or {}).get("status")
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"handoff уже в статусе {cur} (подтверждать можно "
                           f"только pending_confirmation)"}

    # Что реально запускаем: базовая команда исполнителя (без bash-подстановки),
    # ТЗ уходит в stdin — кросс-платформенно. Старые записи без exec_command
    # падают на command (обратная совместимость).
    exec_command = running.get("exec_command") or running.get("command", "")
    spec_text = running.get("spec_text") or ""
    if not spec_text and running.get("spec_file"):
        try:
            with open(running["spec_file"], "r", encoding="utf-8") as _f:
                spec_text = _f.read()
        except Exception:
            logger.debug("spec_file read skipped", exc_info=True)
    # Автономный путь (SaaS): если оператор включил registry-backend OpenHands
    # (executor_backend=openhands) — отдаём ТЗ ему, он решает задачу целиком в
    # СВОЕЙ песочнице (безопасно для multi-user). Иначе — прежний локальный
    # CLI-путь ниже (гейт: у кого OpenHands не настроен, ничего не меняется).
    try:
        from backend.config import get_settings
        _active_backend = (getattr(get_settings(), "executor_backend", "") or "").strip().lower()
    except Exception:
        _active_backend = ""
    if _active_backend == "openhands":
        try:
            from backend.core.executors.base import TaskSubmission
            from backend.core.executors.registry import get_active_backend
            oh = get_active_backend()
            handle = await oh.submit(TaskSubmission(
                tz_markdown=_wrap_spec_for_executor(spec_text) or exec_command,
                task_type=running.get("task_type"),
                working_dir=rp or None,
                metadata={"user_id": user_id, "handoff_id": handoff_id,
                          "agent": running.get("agent")},
                timeout_seconds=3600))
            # OpenHands решает АСИНХРОННО — оставляем статус RUNNING, возвращаем
            # ссылку на conversation. Финализацию (DONE/FAILED + результат)
            # подтянет опрос /executor/{id} или webhook — следующий инкремент.
            conv = str(handle.backend_ref or "")
            logger.info("handoff %s → OpenHands conversation=%s", handoff_id, conv)
            return {"status": "running", "handoff_id": handoff_id,
                    "executor": "openhands", "conversation_id": conv,
                    "message": ("ТЗ отправлено автономному исполнителю OpenHands — "
                                "решается в песочнице, статус подтянется")}
        except Exception as e:
            logger.error("OpenHands dispatch failed, fallback to CLI: %s", e)
            # падаем в обычный CLI-путь ниже (не роняем подтверждение)

    # BYO-ключ: если пользователь привязал СВОЙ аккаунт помощника — сборка
    # идёт на его ключ/биллинг; иначе серверный дефолт (env/`claude login`).
    env_overrides = {}
    try:
        from backend.core.executors.user_credentials import resolve_env_for_build
        env_overrides = await resolve_env_for_build(user_id, agent=running.get("agent"))
    except Exception:
        logger.debug("BYO key resolve skipped", exc_info=True)
    # cwd локального исполнителя ДОЛЖЕН существовать — иначе subprocess падает
    # (NotADirectoryError / WinError 267 на Windows, ENOENT на *nix). Это же
    # ловит fallback OpenHands→CLI (когда OpenHands недоступен по сети). Не
    # роняем confirm 500-й: переводим handoff в FAILED с понятным сообщением.
    if not os.path.isdir(rp):
        store.transition(handoff_id, RUNNING, FAILED, rc=-1,
                         output_tail=f"repo_path не найден: {rp}")
        return {"status": "failed", "handoff_id": handoff_id, "rc": -1,
                "message": (f"Рабочая папка не найдена на сервере: {rp!r}. Укажите "
                            "существующий путь к git-репозиторию. Для контентных ТЗ "
                            "(презентация, статьи, брошюра) кодинг-исполнитель не нужен — "
                            "результат уже готов как документ (кнопка «Показать ТЗ» / "
                            "раздел Результаты).")}
    # Долгий синхронный `claude -p` (до 30 мин) блокировал HTTP-запрос и фронт
    # «терял» карточку. background=True (UI-путь) → гоним исполнитель в ФОНЕ и
    # возвращаем сразу {status: running}; финализацию (DONE/FAILED + verify +
    # attach + WS-сигнал) делает фоновый таск. Синхронный путь (kanon-луп) — как
    # раньше.
    if background:
        import asyncio as _aio
        _t = _aio.create_task(_run_handoff_execution(
            user_id, handoff_id, running, rp, exec_command, spec_text,
            env_overrides, store))
        _BG_HANDOFF_TASKS.add(_t)
        _t.add_done_callback(_BG_HANDOFF_TASKS.discard)
        return {"status": "running", "handoff_id": handoff_id, "executor": "cli",
                "message": ("Запущено — исполнитель работает в фоне. Статус и дифф "
                            "подтянутся в очередь по завершении (обновите список "
                            "или дождитесь уведомления).")}
    return await _run_handoff_execution(
        user_id, handoff_id, running, rp, exec_command, spec_text,
        env_overrides, store)


async def _confirm_artifact_handoff(user_id: str, handoff_id: str,
                                    rec: Dict[str, Any], store) -> Dict[str, Any]:
    """ФАЗА 2 (artifact_mode): запуск CLI-агента в scratch-папке → сбор файлов.

    Отдельный от репо-пути обработчик: агент собирает файл-результат
    (презентация/КП/таблица) в изолированной временной папке, мы забираем эти
    файлы и возвращаем список артефактов. Гейт человека (confirm) уже пройден;
    здесь ещё отдельный ops-рубильник enable_handoff_artifact_mode. Never-raise
    наружу — все сбои переводят запись в FAILED с понятным сообщением."""
    from backend.core.tasks.handoff_store import (
        DONE,
        FAILED,
        PENDING,
        RUNNING,
        HandoffStore,
    )
    assert isinstance(store, HandoffStore)

    try:
        from backend.core.config.feature_flags import get_feature_flags
        art_allowed = bool(get_feature_flags().enable_handoff_artifact_mode)
    except Exception:
        art_allowed = False
    if not art_allowed:
        return {"status": "error", "handoff_id": handoff_id,
                "message": ("Сборка артефакта кодом выключена оператором "
                            "(enable_handoff_artifact_mode=false). Включите "
                            "TESSENT_ENABLE_HANDOFF_ARTIFACT_MODE=true или "
                            "запустите команду вручную: " + rec.get("command", ""))}

    work_dir = rec.get("work_dir")
    # Self-heal: scratch-папку (mkdtemp) заводят на ФАЗЕ 1 (подготовка), а
    # используют здесь, на ФАЗЕ 2 (confirm). Между ними /tmp мог быть очищен, или
    # контейнер/процесс перезапущен → папки нет, и раньше это был жёсткий error.
    # Папка — изолированный throwaway, а spec_text перечитывается из записи, так
    # что пустую пересозданную папку агент наполнит заново. Happy-path не меняется
    # (makedirs — no-op, если папка есть). Только для artifact-mode (гейт выше).
    if work_dir:
        try:
            os.makedirs(work_dir, exist_ok=True)
        except Exception:
            logger.debug("artifact work_dir recreate skipped", exc_info=True)
    if not work_dir or not os.path.isdir(work_dir):
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"scratch-папка сборки не найдена: {work_dir!r}"}

    running = store.transition(handoff_id, PENDING, RUNNING,
                               confirmed_by=user_id, executed_as="artifact")
    if running is None:
        cur = (store.get(handoff_id) or {}).get("status")
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"handoff уже в статусе {cur} (подтверждать можно "
                           f"только pending_confirmation)"}

    exec_command = running.get("exec_command") or running.get("command", "")
    spec_text = running.get("spec_text") or ""
    if not spec_text and running.get("spec_file"):
        try:
            with open(running["spec_file"], "r", encoding="utf-8") as _f:
                spec_text = _f.read()
        except Exception:
            logger.debug("spec_file read skipped", exc_info=True)

    env_overrides = {}
    try:
        from backend.core.executors.user_credentials import resolve_env_for_build
        env_overrides = await resolve_env_for_build(user_id, agent=running.get("agent"))
    except Exception:
        logger.debug("BYO key resolve skipped", exc_info=True)

    res = await _exec_handoff(exec_command, work_dir, env_overrides=env_overrides,
                              stdin_text=_wrap_spec_for_executor(spec_text) or None)
    artifacts = _collect_artifacts(work_dir)
    # Успех = процесс не упал И собрали хотя бы один файл-результат: пустой
    # выхлоп при rc=0 (агент ничего не создал) — это не «готово».
    ok = res.get("rc") == 0 and bool(artifacts)
    final = DONE if ok else FAILED
    store.transition(handoff_id, RUNNING, final,
                     rc=res.get("rc"), output_tail=res.get("output", ""),
                     artifacts=[{"name": a["name"], "rel": a["rel"],
                                 "size": a["size"]} for a in artifacts])

    out = {"status": "success" if ok else "failed",
           "handoff_id": handoff_id, "artifacts": artifacts, **res}
    if not artifacts and res.get("rc") == 0:
        out["message"] = ("агент отработал, но файл-результат не найден в рабочей "
                          "папке — уточните в ТЗ, что нужно СОХРАНИТЬ файл в ./")

    # Готовые файлы + отчёт — в задачу задачника (как в кодовом пути: trello/
    # jira — вложения, yougile — списком в тексте). Kill-switch тот же.
    _tracker, _tracker_id = running.get("tracker"), running.get("tracker_task_id")
    if ok and _tracker and _tracker_id:
        _files_note = ""
        try:
            if (os.getenv("TESSENT_TRACKER_ATTACH_FILES", "on") or "").lower() \
                    not in ("off", "false", "0"):
                from backend.core.tasks.task_actions import attach_files
                att = await attach_files(user_id, _tracker, _tracker_id,
                                         [a["path"] for a in artifacts[:5]])
                out["task_files"] = att
                if att.get("attached"):
                    _files_note = "\n\n📎 Приложены файлы: " + ", ".join(
                        f["file"] for f in att["attached"])
                elif att.get("files_unsupported"):
                    _files_note = ("\n\n📎 Готовые файлы (вложения в этом "
                                   "задачнике не поддержаны API): "
                                   + ", ".join(a["name"] for a in artifacts[:5]))
                # ссылки на скачивание через наш бэкенд (если задан публичный
                # адрес) — покрывает задачники без file-API (YouGile)
                _files_note += _artifact_links_note(handoff_id, artifacts[:5])
                _files_note += await _drive_links_note(
                    user_id, [a["path"] for a in artifacts[:5]])
        except Exception as e:
            logger.warning(f"artifact attach_files failed: {e}")
        try:
            from backend.core.tasks.task_actions import attach_result
            out["task_attach"] = await attach_result(
                user_id, _tracker, _tracker_id,
                result_text=_task_report_context(running)
                + (res.get("output") or "")[-3000:] + _files_note,
                title="Артефакт собран агентом (по подтверждению)")
        except Exception as e:
            logger.warning(f"artifact attach_result failed: {e}")
    elif ok:
        # Без привязки к трекеру: авто-доставка по маршруту по умолчанию.
        from backend.core.tasks.handoff_store import HandoffStore as _HS
        _auto = await _auto_route_result(user_id, handoff_id,
                                         _HS(user_id).get(handoff_id) or {})
        if _auto:
            out["auto_delivery"] = _auto

    # Урок Self-Grown (как в кодовом пути) — best-effort.
    try:
        from backend.core.tasks.lessons import append_lesson
        append_lesson(user_id, task_title=running.get("task_title") or "",
                      agent=running.get("agent") or "",
                      verdict="done" if ok else "failed",
                      missing=[], summary=(res.get("output") or "")[-400:],
                      attempt=int(running.get("attempt") or 1),
                      handoff_id=handoff_id, kind="artifact")
    except Exception:
        logger.debug("artifact lesson skipped", exc_info=True)

    # Сигнал в UI — завершение видно сразу, без поллинга. Fire-and-forget.
    try:
        from backend.api.websocket.signals_ws import publish_signal
        publish_signal("handoff_completed", {
            "handoff_id": handoff_id, "status": final, "rc": res.get("rc"),
            "title": running.get("task_title") or "",
            "artifacts": [a["name"] for a in artifacts],
        }, tenant_id=user_id)
    except Exception:
        logger.debug("artifact completion signal skipped", exc_info=True)

    # Внешние системы (Minitest): webhook с итогами и списком файлов.
    await _notify_completion_webhook(user_id, {
        "handoff_id": handoff_id,
        "status": final,
        "rc": res.get("rc"),
        "title": running.get("task_title") or "",
        "assignee": running.get("assignee") or "",
        "artifacts": [{"name": a["name"], "size": a["size"],
                       "download": f"/api/v1/task-analysis/handoff/"
                                   f"{handoff_id}/artifact/{i}"}
                      for i, a in enumerate(artifacts)],
        "tracker": running.get("tracker") or "",
        "tracker_task_id": running.get("tracker_task_id") or "",
        "source": running.get("source") or None,
        "executed_as": "artifact",
    })

    return out


async def execute_content_handoff(user_id: str, handoff_id: str) -> Dict[str, Any]:
    """ИСПОЛНИТЬ контентное ТЗ: LLM генерирует ГОТОВЫЙ документ (КП/статью/
    брошюру), а не ТЗ на него. Полный цикл как у кодового пути: RUNNING →
    генерация premium-моделью тенанта (workload_policy, Google-страховка) →
    авто-проверка судьёй → DONE + attach в трекер + сигнал доставки.

    ТЗ из data-driven конвейера уже несёт факты компании — этого хватает для
    финального документа. Нет LLM/ключей → откат в pending (можно повторить),
    не FAILED. Never-raise наружу."""
    from backend.core.tasks.handoff_store import DONE, PENDING, RUNNING, HandoffStore
    store = HandoffStore(user_id)
    running = store.transition(handoff_id, PENDING, RUNNING, executed_as="content")
    if running is None:
        cur = (store.get(handoff_id) or {}).get("status")
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"handoff не найден или не pending (сейчас: {cur})"}

    spec_text = running.get("spec_text") or ""
    if not spec_text and running.get("spec_file"):
        try:
            with open(running["spec_file"], "r", encoding="utf-8") as _f:
                spec_text = _f.read()
        except Exception:
            logger.debug("spec_file read skipped", exc_info=True)
    if not spec_text.strip():
        store.transition(handoff_id, RUNNING, PENDING)
        return {"status": "error", "handoff_id": handoff_id,
                "message": "у записи нет текста ТЗ — генерировать не из чего"}

    # сохранённые ссылки компании (сайт/прайс/шаблоны) — в контекст документа
    _res_block = ""
    try:
        from backend.core.store.user_resources import resources_context_block
        _res_block = resources_context_block(user_id)
    except Exception:
        pass
    prompt = (
        "Ты — исполнитель. Ниже — задача (ТЗ) с фактами компании. Собери "
        "ГОТОВЫЙ, ЗАКОНЧЕННЫЙ РЕЗУЛЬТАТ этой задачи — такой, чтобы пользователь "
        "мог сразу его использовать/сдать как выполненную работу, БЕЗ доработки. "
        "Форма — какую требует задача: документ, коммерческое предложение, "
        "письмо, расчёт/таблица, статья, план, тексты и т.п. НЕ пересказывай "
        "ТЗ, НЕ пиши план действий, НЕ задавай уточняющих вопросов — сразу "
        "выдай сам результат. Пиши по-русски, в markdown (таблицы для чисел). "
        "Используй ТОЛЬКО факты из ТЗ — не выдумывай цифры; чего в данных нет — "
        "явная пометка «[уточнить]».\n\n"
        f"=== ЗАДАЧА (ТЗ) ===\n{spec_text[:12000]}\n"
        + (f"\n{_res_block}\n" if _res_block else ""))
    document = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        document = await generate_for_workload(user_id, "tz_generation", prompt)
    except Exception as e:
        logger.warning(f"content execute LLM failed: {e}")
    if not document or not document.strip():
        store.transition(handoff_id, RUNNING, PENDING)
        return {"status": "error", "handoff_id": handoff_id,
                "message": ("LLM недоступен (нет ключей/сети) — задача возвращена "
                            "в очередь, попробуйте позже")}
    document = document.strip()

    # авто-проверка тем же судьёй (git-улик нет — судит документ против ТЗ)
    _verify = None
    try:
        _verify = await _verify_handoff_result(user_id, spec_text, "",
                                               {"output": document})
    except Exception:
        logger.debug("content verify skipped", exc_info=True)

    store.transition(handoff_id, RUNNING, DONE,
                     rc=0, result_document=document[:60000],
                     verify_verdict=(_verify or {}).get("verdict") or "done",
                     verify_summary=((_verify or {}).get("summary")
                                     or "Документ сгенерирован по ТЗ"),
                     verify_missing=(_verify or {}).get("missing") or [])

    # урок Self-Grown (контентный путь)
    try:
        from backend.core.tasks.lessons import append_lesson
        append_lesson(user_id, task_title=running.get("task_title") or "",
                      agent=running.get("agent") or "llm",
                      verdict=(_verify or {}).get("verdict") or "done",
                      missing=(_verify or {}).get("missing") or [],
                      summary=(_verify or {}).get("summary") or "",
                      attempt=int(running.get("attempt") or 1),
                      handoff_id=handoff_id, kind="content")
    except Exception:
        logger.debug("content lesson skipped", exc_info=True)

    out: Dict[str, Any] = {"status": "success", "handoff_id": handoff_id,
                           "result_document": document,
                           "verify": _verify}

    tracker, tracker_id = running.get("tracker"), running.get("tracker_task_id")
    if tracker and tracker_id:
        try:
            from backend.core.tasks.task_actions import attach_result
            out["task_attach"] = await attach_result(
                user_id, tracker, tracker_id,
                result_text=document[-3000:],
                title="Готовый документ (сгенерирован по контентному ТЗ)")
        except Exception as e:
            logger.warning(f"content execute attach_result failed: {e}")
    else:
        _auto = await _auto_route_result(user_id, handoff_id,
                                         store.get(handoff_id) or {})
        if _auto:
            out["auto_delivery"] = _auto

    try:
        from backend.api.websocket.signals_ws import publish_signal
        publish_signal("handoff_completed", {
            "handoff_id": handoff_id, "status": DONE, "rc": 0,
            "title": running.get("task_title") or "",
            "verdict": (_verify or {}).get("verdict") or "done",
            "summary": ((_verify or {}).get("summary")
                        or "Готовый документ сгенерирован"),
        }, tenant_id=user_id)
    except Exception:
        logger.debug("content execute signal skipped", exc_info=True)

    # Внешние системы (Minitest): итог + ссылки на скачивание документа.
    await _notify_completion_webhook(user_id, {
        "handoff_id": handoff_id,
        "status": DONE,
        "rc": 0,
        "title": running.get("task_title") or "",
        "assignee": running.get("assignee") or "",
        "verdict": (_verify or {}).get("verdict") or "done",
        "summary": ((_verify or {}).get("summary")
                    or "Готовый документ сгенерирован"),
        "document_render": {
            fmt: f"/api/v1/task-analysis/handoff/{handoff_id}/render/{fmt}"
            for fmt in ("html", "pdf", "docx", "xlsx", "pptx")},
        "tracker": running.get("tracker") or "",
        "tracker_task_id": running.get("tracker_task_id") or "",
        "source": running.get("source") or None,
        "executed_as": "content",
    })
    return out


async def accept_handoff_result(user_id: str, handoff_id: str,
                                note: str = "") -> Dict[str, Any]:
    """Принять КОНТЕНТНОЕ ТЗ как готовый результат (без кодинг-исполнителя).

    UX-тупик, который закрывает: контентное ТЗ (КП, статья, брошюра — без
    репозитория) висело в «Ждёт подтверждения» вечно — «Подтвердить» требовал
    repo_path и отказывал, хотя документ-результат уже готов (само ТЗ).
    Отдельная явная кнопка вместо угадайки в confirm: кодовое ТЗ с забытой
    папкой случайно не закроется. Завершает цикл: DONE + attach в трекер +
    сигнал доставки."""
    from backend.core.tasks.handoff_store import DONE, PENDING, HandoffStore
    store = HandoffStore(user_id)
    rec = store.transition(handoff_id, PENDING, DONE,
                           accepted_as_content=True,
                           accept_note=(note or "")[:300],
                           verify_verdict="done",
                           verify_summary="Принято пользователем как готовый "
                                          "контентный документ (ТЗ = результат)")
    if rec is None:
        cur = (store.get(handoff_id) or {}).get("status")
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"handoff не найден или не pending (сейчас: {cur})"}

    out: Dict[str, Any] = {"status": "success", "handoff_id": handoff_id,
                           "accepted": True}

    # результат — в задачу трекера (как у кодового пути), недеструктивно
    tracker, tracker_id = rec.get("tracker"), rec.get("tracker_task_id")
    if tracker and tracker_id:
        try:
            from backend.core.tasks.task_actions import attach_result
            spec_text = rec.get("spec_text") or ""
            out["task_attach"] = await attach_result(
                user_id, tracker, tracker_id,
                result_text=spec_text[-3000:] or "Контентный документ принят",
                title="Готовый документ (контентное ТЗ, принято пользователем)")
        except Exception as e:
            logger.warning(f"accept attach_result failed: {e}")
    else:
        _auto = await _auto_route_result(user_id, handoff_id, rec)
        if _auto:
            out["auto_delivery"] = _auto

    # доставка: сигнал в UI
    try:
        from backend.api.websocket.signals_ws import publish_signal
        publish_signal("handoff_completed", {
            "handoff_id": handoff_id, "status": DONE, "rc": 0,
            "title": rec.get("task_title") or "",
            "verdict": "done",
            "summary": "Контентный документ принят как результат",
        }, tenant_id=user_id)
    except Exception:
        logger.debug("accept completion signal skipped", exc_info=True)
    return out


async def _auto_route_result(user_id: str, handoff_id: str,
                             rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Авто-доставка по маршруту по умолчанию (delivery_prefs).

    Срабатывает ТОЛЬКО для задач БЕЗ привязки к трекеру (привязанные уже
    доставлены автоматикой attach_result) и только если пользователь явно
    сохранил маршрут. Never-raise."""
    try:
        if rec.get("tracker") and rec.get("tracker_task_id"):
            return None
        from backend.core.tasks.delivery_prefs import get_route
        target = get_route(user_id)
        if not target:
            return None
        res = await deliver_handoff_result(user_id, handoff_id, target)
        if res.get("status") != "success":
            logger.warning("auto-route delivery failed: %s", res.get("message"))
        return res
    except Exception:
        logger.debug("auto-route delivery skipped", exc_info=True)
        return None


async def deliver_handoff_result(user_id: str, handoff_id: str,
                                 target: Dict[str, Any]) -> Dict[str, Any]:
    """Отправить РЕЗУЛЬТАТ завершённого handoff'а в задачник или CRM по кнопке.

    Автоматика уже есть для задач, ПРИШЕДШИХ из трекера (attach_result/
    attach_files на завершении). Эта функция закрывает обратный случай:
    задача из встречи или руками, а результат нужно положить в YouGile/
    Trello/Jira (в существующую или НОВУЮ задачу) или в CRM. Недеструктивно;
    CRM — за тем же env-гейтом ENABLE_CRM_WRITEBACK, что и везде.

    target:
      {"kind":"tracker","system":"yougile|trello|jira","task_id":"…"}   — в существующую
      {"kind":"tracker","system":"…","column_id":"…"}                    — создать новую
      {"kind":"crm","provider":"amocrm|bitrix24|hubspot|pipedrive",
       "op":"note|create","entity_id":"…","name":"…","value":…}
    """
    import os as _os
    from datetime import datetime, timezone

    from backend.core.tasks.handoff_store import DONE, HandoffStore
    store = HandoffStore(user_id)
    rec = store.get(handoff_id)
    if not rec:
        return {"status": "error", "message": f"handoff {handoff_id} не найден"}
    if rec.get("status") != DONE:
        return {"status": "error",
                "message": f"результат ещё не готов (статус: {rec.get('status')})"}

    title = str(rec.get("task_title") or "Задача").strip()
    result_text = str(rec.get("result_document") or rec.get("output_tail")
                      or rec.get("verify_summary") or "").strip()
    if not result_text:
        result_text = "Задача выполнена (см. вложения)."

    # Файлы-артефакты (artifact_mode): абсолютные пути строго внутри work_dir.
    file_paths: List[str] = []
    wd = rec.get("work_dir")
    if wd and _os.path.isdir(wd):
        root = _os.path.realpath(wd)
        for a in (rec.get("artifacts") or [])[:5]:
            fp = _os.path.realpath(_os.path.join(root, str(a.get("rel") or "")))
            if fp.startswith(root + _os.sep) and _os.path.isfile(fp):
                file_paths.append(fp)

    kind = str((target or {}).get("kind") or "tracker").strip().lower()
    delivery: Dict[str, Any] = {
        "kind": kind, "at": datetime.now(timezone.utc).isoformat()}
    files_res: Optional[Dict[str, Any]] = None

    if kind == "tracker":
        system = str(target.get("system") or "").strip().lower()
        task_id = str(target.get("task_id") or "").strip()
        from backend.core.tasks.task_actions import (
            attach_files,
            attach_or_describe,
            create_and_prepare_task,
        )
        # Ссылки на Диск (если включено) — сразу в текст: покрывает YouGile,
        # где file-API нет.
        drive_note = ""
        if file_paths:
            try:
                drive_note = await _drive_links_note(user_id, file_paths)
            except Exception:
                logger.debug("deliver drive links skipped", exc_info=True)
        body = result_text[-6000:] + (drive_note or "")

        if not task_id:
            created = await create_and_prepare_task(
                user_id, system, title=title, tz_text=body,
                column_id=str(target.get("column_id") or ""),
                description=f"Результат Vibe Tasking: {title}")
            if created.get("status") == "error":
                return {"status": "error",
                        "message": created.get("message") or "создание задачи не удалось",
                        "result": created}
            task_id = str(created.get("task_id") or "")
            delivery["created"] = True
            if created.get("warnings"):
                delivery["warnings"] = created["warnings"]
        else:
            att = await attach_or_describe(user_id, system, task_id,
                                           text=body, title="Результат выполнения")
            if att.get("status") == "error":
                return {"status": "error",
                        "message": att.get("message") or "не удалось приложить результат"}
        if file_paths and task_id:
            files_res = await attach_files(user_id, system, task_id, file_paths)
        delivery.update(system=system, task_id=task_id)

    elif kind == "crm":
        from backend.core.ontology.crm_writeback import (
            get_writer,
            writeback_enabled,
        )
        if not writeback_enabled():
            return {"status": "error",
                    "message": "Запись в CRM выключена (включите ENABLE_CRM_WRITEBACK на сервере)"}
        provider = str(target.get("provider") or "").strip().lower()
        op = str(target.get("op") or "note").strip().lower()
        writer = get_writer(provider)
        if not writer:
            return {"status": "error",
                    "message": f"CRM-провайдер не поддержан для записи: {provider or '—'}"}
        if (env_err := writer.env_check()):
            return {"status": "error", "message": f"CRM {provider}: {env_err}"}
        fields = {
            "name": str(target.get("name") or title).strip(),
            "value": target.get("value"),
            "entity_id": str(target.get("entity_id") or "").strip(),
            "text": f"✅ Результат «{title}» (Vibe Tasking):\n\n" + result_text[-3000:],
        }
        try:
            res = await writer.write(op, fields)
        except Exception as e:
            return {"status": "error", "message": f"запись в CRM: {e}"}
        delivery.update(provider=provider, op=op,
                        crm_id=(res or {}).get("id"),
                        url=(res or {}).get("url") or "")
    else:
        return {"status": "error", "message": f"неизвестная цель доставки: {kind}"}

    # Факт доставки — на запись (чипы в UI: «✓ trello #123», «✓ amocrm»).
    deliveries = list(rec.get("deliveries") or [])
    deliveries.append(delivery)
    store.update(handoff_id, deliveries=deliveries)
    return {"status": "success", "handoff_id": handoff_id,
            "delivery": delivery,
            **({"files": files_res} if files_res is not None else {})}


async def reject_handoff(user_id: str, handoff_id: str,
                         reason: str = "") -> Dict[str, Any]:
    """Пользователь отклонил делегацию — ничего не запускаем."""
    from backend.core.tasks.handoff_store import PENDING, REJECTED, HandoffStore
    rec = HandoffStore(user_id).transition(handoff_id, PENDING, REJECTED,
                                           reject_reason=reason)
    if rec is None:
        return {"status": "error",
                "message": f"handoff {handoff_id} не найден или не pending"}
    return {"status": "success", "handoff_id": handoff_id, "rejected": True}


async def rework_handoff(user_id: str, handoff_id: str,
                         note: str = "") -> Dict[str, Any]:
    """«Вернуть в доработку» (состояние rework из work-item машины OpenOPC).

    Завершённый/проваленный handoff → НОВАЯ pending-запись: исходное ТЗ +
    блок «Доработка» с замечаниями LLM-судьи (verify_missing) и комментарием
    заказчика. Исполнение — тем же путём с явным подтверждением (гейт
    человека сохраняется), попытка нумеруется, записи связаны parent-ссылкой.
    Замечаний нет вовсе → просим сформулировать, что исправить (переделка
    «просто так» — не переделка)."""
    from backend.core.tasks.handoff_store import DONE, FAILED, HandoffStore
    store = HandoffStore(user_id)
    rec = store.get(handoff_id)
    if not rec:
        return {"status": "error", "message": f"handoff {handoff_id} не найден"}
    if rec.get("status") not in (DONE, FAILED):
        return {"status": "error",
                "message": (f"в доработку возвращается завершённый/проваленный "
                            f"handoff (сейчас: {rec.get('status')})")}

    missing = [str(m) for m in (rec.get("verify_missing") or []) if str(m).strip()]
    note = (note or "").strip()
    if not missing and not note:
        return {"status": "error",
                "message": ("замечаний нет: судья счёл ТЗ выполненным — опишите "
                            "в note, что именно исправить")}

    spec_text = rec.get("spec_text") or ""
    attempt = int(rec.get("attempt") or 1) + 1
    remarks = "\n".join(f"- {m}" for m in missing) or "- (замечаний судьи нет)"
    rework_block = (
        f"\n\n=== ДОРАБОТКА · попытка {attempt} ===\n"
        f"Предыдущий результат: {str(rec.get('verify_summary') or '')[:400]}\n"
        f"Замечания проверки (устранить в первую очередь):\n{remarks}\n"
        + (f"Комментарий заказчика: {note[:600]}\n" if note else "")
        + "Сохрани сделанное ранее, исправь только указанное.")
    new_spec = (spec_text + rework_block).strip()

    # свежий spec-файл (прежний мог жить во временной папке прошлой попытки)
    d = tempfile.mkdtemp(prefix="tessent_handoff_")
    spec_file = os.path.join(d, "task_spec.md")
    try:
        with open(spec_file, "w", encoding="utf-8") as f:
            f.write(new_spec)
    except Exception:
        logger.debug("rework spec file write skipped", exc_info=True)

    exec_command = rec.get("exec_command") or _AGENT_COMMANDS.get(
        rec.get("agent") or "claude", _AGENT_COMMANDS["claude"])
    repo_path = rec.get("repo_path")
    display_command = (f"{exec_command} < {spec_file}" if not repo_path
                       else f"cd {repo_path} && {exec_command} < {spec_file}")

    new_rec = store.create({
        "agent": rec.get("agent"),
        "task_title": (rec.get("task_title") or "") + f" · доработка {attempt}",
        "tracker": rec.get("tracker"),
        "tracker_task_id": rec.get("tracker_task_id"),
        "spec_text": new_spec,
        "spec_file": spec_file,
        "command": display_command,
        "exec_command": exec_command,
        "repo_path": repo_path,
        "kanon": rec.get("kanon") or None,
        "parent_handoff_id": handoff_id,
        "attempt": attempt,
        "rework_note": note[:600],
    })
    store.update(handoff_id, rework_child_id=new_rec["id"])

    # урок Self-Grown: доработка = сигнал, чему учиться
    try:
        from backend.core.tasks.lessons import append_lesson
        append_lesson(user_id, task_title=rec.get("task_title") or "",
                      agent=rec.get("agent") or "", verdict="rework",
                      missing=missing, summary=note or "возврат в доработку",
                      attempt=attempt, handoff_id=handoff_id)
    except Exception:
        logger.debug("rework lesson skipped", exc_info=True)

    new_rec["requires_confirmation"] = True
    return {"status": "success", "handoff_id": new_rec["id"],
            "parent_handoff_id": handoff_id, "attempt": attempt,
            "record": {k: v for k, v in new_rec.items() if k != "spec_text"}}


# ============================================================================
# Web-only исполнители (Lovable/v0/Bolt/Replit/Claude-web/ChatGPT) — отдельный
# трек: готовый бриф + ссылка-запуск, человек выполняет в браузере, результат
# (URL) фиксирует обратно. НЕ автономно (у web-тулов своя сессия в браузере).
# ============================================================================

async def web_handoff(user_id: str, task: Dict[str, Any], *,
                      tool: str, launch_url: str, spec_text: str,
                      prefilled: bool = False, note: str = "") -> Dict[str, Any]:
    """Создать PENDING web-хэндофф (ссылка-запуск + ТЗ). Ничего не исполняет.

    Человек открывает launch_url, делает работу в инструменте и фиксирует
    результат: POST /task-analysis/handoff/{id}/web-result {result_url}."""
    from backend.core.tasks.handoff_store import HandoffStore
    rec = HandoffStore(user_id).create({
        "kind": "web",
        "agent": f"web:{tool}",
        "tool": tool,
        "task_title": task.get("title", ""),
        "launch_url": launch_url,
        "prefilled": bool(prefilled),
        "note": note,
        "spec_text": spec_text,
        "tracker": task.get("tracker") or task.get("system"),
        "tracker_task_id": task.get("tracker_task_id") or task.get("external_id"),
    })
    rec["requires_manual_run"] = True
    rec["how_to_run"] = (
        "Открой launch_url, выполни в инструменте, затем зафиксируй результат: "
        "POST /task-analysis/handoff/{id}/web-result {result_url}.")
    return rec


async def submit_web_result(user_id: str, handoff_id: str,
                            result_url: str) -> Dict[str, Any]:
    """Зафиксировать результат web-хэндоффа (URL артефакта) → DONE.

    Опц. прикладывает результат в трекер, если задача была там создана."""
    from backend.core.tasks.handoff_store import DONE, PENDING, HandoffStore
    store = HandoffStore(user_id)
    rec = store.get(handoff_id)
    if not rec:
        return {"status": "error", "message": f"handoff {handoff_id} не найден"}
    if rec.get("kind") != "web":
        return {"status": "error",
                "message": "это не web-хэндофф (используй confirm/reject)"}
    updated = store.transition(handoff_id, PENDING, DONE,
                               result_url=result_url, done_via="web")
    if updated is None:
        cur = (store.get(handoff_id) or {}).get("status")
        return {"status": "error", "handoff_id": handoff_id,
                "message": f"handoff уже в статусе {cur} (фиксировать можно только pending)"}
    tracker, tid = updated.get("tracker"), updated.get("tracker_task_id")
    attach = None
    if tracker and tid and result_url:
        try:
            from backend.core.tasks.task_actions import attach_or_describe
            attach = await attach_or_describe(
                user_id, str(tracker), str(tid),
                text=f"Результат (web): {result_url}", title="Результат")
        except Exception as e:
            logger.warning(f"web-result attach failed: {e}")
    return {"status": "success", "handoff_id": handoff_id,
            "result_url": result_url, "task_attach": attach}


_ARTIFACT_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv",
                       "venv", ".cache", "dist", "build", ".next"}
_ARTIFACT_SKIP_NAMES = {"task_spec.md"}


def _collect_artifacts(work_dir: str, *, max_files: int = 20,
                       max_bytes: int = 25 * 1024 * 1024,
                       min_mtime: Optional[float] = None) -> List[Dict[str, Any]]:
    """Собрать файлы-результаты из scratch-папки исполнения (artifact_mode).

    Берём обычные файлы (кроме служебного task_spec.md, скрытых и типичного
    мусора вроде node_modules/.git). Каждый — {name, path, rel, size}. Кап по
    числу файлов и размеру (крупнее max_bytes — пропускаем, не тащим гигабайты).
    Сортировка: сначала «презентабельные» (html/pdf/pptx/xlsx/docx/csv/md/png),
    потом по времени изменения (свежие раньше). Pure/never-raise → [] при сбое.

    min_mtime — брать только файлы, изменённые ПОСЛЕ этого момента (unix time).
    Нужен для репо-режима: репозиторий полон старых файлов, а деливераблы —
    ровно то, что исполнитель создал/изменил за прогон."""
    _PREF = (".html", ".htm", ".pdf", ".pptx", ".xlsx", ".docx",
             ".csv", ".md", ".png", ".svg", ".json")
    out: List[Dict[str, Any]] = []
    try:
        if not work_dir or not os.path.isdir(work_dir):
            return []
        root_abs = os.path.realpath(work_dir)
        for dirpath, dirnames, filenames in os.walk(root_abs):
            # не спускаемся в мусорные/скрытые директории
            dirnames[:] = [dd for dd in dirnames
                           if dd not in _ARTIFACT_SKIP_DIRS and not dd.startswith(".")]
            for fn in filenames:
                if fn in _ARTIFACT_SKIP_NAMES or fn.startswith("."):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if not os.path.isfile(fp) or os.path.islink(fp):
                        continue
                    size = os.path.getsize(fp)
                    if size <= 0 or size > max_bytes:
                        continue
                    mtime = os.path.getmtime(fp)
                    if min_mtime is not None and mtime < min_mtime:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(fp, root_abs)
                out.append({"name": fn, "path": fp, "rel": rel,
                            "size": size, "_mtime": mtime,
                            "_pref": 0 if fn.lower().endswith(_PREF) else 1})
    except Exception:
        logger.debug("collect artifacts failed", exc_info=True)
        return []
    out.sort(key=lambda a: (a["_pref"], -a["_mtime"]))
    for a in out:
        a.pop("_mtime", None)
        a.pop("_pref", None)
    return out[:max_files]


# ── Где исполнителю МОЖНО работать (анти-RCE по рабочей папке) ──────────────
# repo_path — путь на СЕРВЕРЕ. Оператор задаёт TESSENT_HANDOFF_REPO_ROOT →
# исполнение строго внутри него (жёсткий allowlist, поведение не меняется).
# Но ПО УМОЛЧАНИЮ корень пуст, и раньше это значило «любая папка сервера»:
# /etc, ~/.ssh, каталог data/ самого Tessbrain (там токены, ключи, mcp_tokens.json).
# Базовый deny-list закрывает именно худший случай и НЕ мешает обычной работе:
# рабочий git-репозиторий не лежит в /etc или ~/.ssh. Это НЕ песочница, а нижняя
# планка — настоящая изоляция включается заданием TESSENT_HANDOFF_REPO_ROOT.
_DENY_POSIX_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64",
                     "/proc", "/sys", "/dev", "/root", "/var/lib", "/var/run")
_DENY_HOME_SUBDIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config")
_DENY_WIN_MARKERS = ("\\windows", "\\program files", "\\programdata")
_HANDOFF_ROOT_WARNED = False


def handoff_path_violation(repo_path: str) -> str:
    """Причина, по которой в этой папке исполнять НЕЛЬЗЯ ("" — можно).

    1) Задан TESSENT_HANDOFF_REPO_ROOT → разрешён только он и его подпапки.
    2) Не задан → базовый deny-list (системные каталоги, домашние секреты,
       собственные data/config Tessbrain) + предупреждение оператору в лог.
    Never-raise: при любой ошибке разбора пути молча разрешаем — задача функции
    закрыть явно опасное, а не ломать рабочие сценарии."""
    global _HANDOFF_ROOT_WARNED
    try:
        rp_abs = os.path.realpath(str(repo_path or "").strip())
        if not rp_abs:
            return ""
        root = (os.getenv("TESSENT_HANDOFF_REPO_ROOT", "") or "").strip()
        if root:
            root_abs = os.path.realpath(root)
            if rp_abs != root_abs and not rp_abs.startswith(root_abs + os.sep):
                return (f"Рабочая папка вне разрешённой зоны исполнения. Допустимы "
                        f"только пути внутри {root!r} — обратитесь к администратору.")
            return ""

        if not _HANDOFF_ROOT_WARNED:
            _HANDOFF_ROOT_WARNED = True
            logger.warning(
                "⚠️ TESSENT_HANDOFF_REPO_ROOT не задан — кодинг-исполнитель может "
                "работать в любой папке сервера (кроме системных и секретных). "
                "Задайте корень в .env, чтобы ограничить зону исполнения.")

        low = rp_abs.replace("/", os.sep).lower()
        # корень файловой системы целиком
        if rp_abs in ("/", os.path.splitdrive(rp_abs)[0] + os.sep):
            return "Нельзя исполнять в корне файловой системы."
        for d in _DENY_POSIX_ROOTS:
            if rp_abs == d or rp_abs.startswith(d + os.sep):
                return f"Системный каталог {d} — исполнение запрещено."
        for marker in _DENY_WIN_MARKERS:
            if marker in low:
                return "Системный каталог Windows — исполнение запрещено."
        home = os.path.realpath(os.path.expanduser("~"))
        for sub in _DENY_HOME_SUBDIRS:
            secret = os.path.join(home, sub)
            if rp_abs == secret or rp_abs.startswith(secret + os.sep):
                return f"Каталог с секретами ({sub}) — исполнение запрещено."
        # собственные данные Tessbrain: токены, ключи, стор снапшотов
        own = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        for sub in ("data", "config"):
            protected = os.path.join(own, sub)
            if rp_abs == protected or rp_abs.startswith(protected + os.sep):
                return (f"Служебный каталог Tessbrain ({sub}) — исполнение запрещено "
                        "(там ключи и токены).")
        return ""
    except Exception:
        logger.debug("handoff path check skipped", exc_info=True)
        return ""


async def _exec_handoff(command: str, repo_path: str,
                        env_overrides: Optional[Dict[str, str]] = None,
                        stdin_text: Optional[str] = None) -> Dict[str, Any]:
    """Запустить команду делегации (за флагом). Возвращает rc + хвост вывода.

    stdin_text — ТЗ, которое скармливаем исполнителю через stdin (кросс-
    платформенно, без bash-подстановки `$(cat ...)`). None → stdin не открываем.

    env_overrides — BYO-ключ пользователя (ANTHROPIC_API_KEY/OPENAI_API_KEY):
    накладывается на копию окружения только для этого subprocess, в лог не
    попадает."""
    import asyncio
    # cwd должен быть реальным каталогом — иначе create_subprocess_shell бросает
    # NotADirectoryError (WinError 267) ещё до запуска. Защита на уровне запуска,
    # чтобы ни один вызывающий не ронял процесс 500-м.
    if not repo_path or not os.path.isdir(repo_path):
        return {"executed": False, "rc": -1,
                "output": f"(рабочая папка не найдена: {repo_path!r})"}
    # Единая точка запуска ВСЕХ путей исполнения (UI-confirm, /dispatch от
    # Minitest, узел доски, kanon-луп) — заслон ставим здесь, чтобы ни один
    # вызывающий не мог его обойти, даже если появится новый.
    _violation = handoff_path_violation(repo_path)
    if _violation:
        logger.warning("handoff отклонён: %s (path=%r)", _violation, repo_path)
        return {"executed": False, "rc": -1, "output": f"(отказано: {_violation})"}
    env = None
    if env_overrides:
        env = os.environ.copy()
        # Непустое значение — задаём; ПУСТОЕ — удаляем переменную из окружения
        # subprocess. Пустое значение приходит из подписочного режима как сигнал
        # «убери серверный ANTHROPIC_API_KEY», иначе CLI берёт платный API-ключ
        # вместо подписки (ошибка «Credit balance is too low»). Раньше пустые
        # значения просто игнорировались — обратная совместимость сохранена
        # (реальные BYO-ключи всегда непустые).
        for k, v in env_overrides.items():
            if v:
                env[k] = v
            else:
                env.pop(k, None)
    stdin = asyncio.subprocess.PIPE if stdin_text is not None else None
    proc = await asyncio.create_subprocess_shell(
        command, cwd=repo_path, env=env, stdin=stdin,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    inp = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input=inp), timeout=1800)
    except asyncio.TimeoutError:
        proc.kill()
        return {"executed": True, "rc": -1, "output": "(timeout 30m)"}
    text = (out or b"").decode("utf-8", errors="replace")
    return {"executed": True, "rc": proc.returncode, "output": text[-4000:]}
