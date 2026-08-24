# -*- coding: utf-8 -*-
"""
Kanon Protocol для SIMA — контракт-первая разработка (docs/SIMA_KANON.md).

Перенос методологии из sima_atlas (kanon-protocol-spec-v0.1) на блоки SIMA:

1) КОНТРАКТ блока выводится из уже заполняемых полей SIMA (goal/businessValue
   → mission; metrics → kpi; acceptanceCriteria → acceptance; связи →
   depends_on/provides). Ничего не надо заполнять заново.
2) TRI-STATE приёмка: pass / fail / inconclusive — никогда «тихий зелёный».
   Детерминированные коллекторы (file/contains/cmd) первичны; нет
   доказательств → inconclusive, НЕ pass.
3) CASCADE-верификация: после pass у блока X перепроверяется замыкание
   обратных зависимостей; сломанные помечаются desync СРАЗУ.
4) KANON-WORKSPACE: экспорт проекта в структуру blocks/<id>/contract/*
   (mission.md, acceptance.md, depends_on.json, provides.json, narrative.md)
   + AGENTS.md/CLAUDE.md — кодинг-агент (Claude Code/Cursor/Codex) получает
   контракт, а не «море контекста».

Скелет: чистый stdlib, без LLM и БД — все функции принимают dict-проект
(тот же формат, что возвращает sima_client.get_project). Флаг:
enable_sima_kanon (default OFF).
"""
from __future__ import annotations

import datetime
import glob as _glob
import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"

# Типы связей, образующие зависимость «to-блок зависит от from-блока»
_DEP_EDGE_TYPES = {"dependency", "data_flow", "trigger"}

_CMD_TIMEOUT_SEC = 120

# ── Безопасное исполнение cmd-проверок ────────────────────────────────
# acceptanceCriteria блока — пользовательский контент («cmd: <строка>»).
# Раньше строка шла в subprocess.run(shell=True) → любой автор блока с
# runCommands=true исполнял произвольную команду на сервере (RCE). Теперь:
# (1) shell=False всегда; (2) разбор через shlex, отказ при любых shell-
# метасимволах; (3) argv[0] (+подкоманда) обязан быть в allowlist известных
# инструментов проверки. Что не в allowlist → inconclusive («вне allowlist»),
# НЕ исполняется. Allowlist серверный — пользователь его не расширяет.
_CMD_SHELL_METACHARS = set(";|&$><`\n\r\\!*?(){}[]~")
# Разрешённые (программа, подкоманда|None). None-подкоманда = любая (или без).
_CMD_ALLOWLIST: set = {
    ("npm", "test"), ("npm", "run"), ("npm", "ci"), ("npm", "install"),
    ("pnpm", "test"), ("pnpm", "run"), ("pnpm", "install"),
    ("yarn", "test"), ("yarn", "run"), ("yarn", "install"),
    ("pytest", None),
    ("tsc", None),
    ("eslint", None),
    ("ruff", None), ("ruff", "check"),
    ("mypy", None),
    ("go", "test"), ("go", "build"), ("go", "vet"),
    ("cargo", "test"), ("cargo", "build"), ("cargo", "check"),
    ("make", None),
}


def _safe_cmd_argv(target: str) -> Optional[List[str]]:
    """Разобрать cmd-target в argv, если он безопасен и в allowlist. Иначе None.

    Безопасен = нет shell-метасимволов и (программа, подкоманда) в allowlist.
    Возвращает argv для subprocess.run(shell=False) либо None (не исполнять).
    """
    import shlex
    if not target or any(c in _CMD_SHELL_METACHARS for c in target):
        return None
    try:
        argv = shlex.split(target)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    sub = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None
    if (prog, sub) in _CMD_ALLOWLIST or (prog, None) in _CMD_ALLOWLIST:
        # нормализуем argv[0] к basename — не даём абсолютные/относительные пути
        return [prog, *argv[1:]]
    return None


def _g(d: dict, *keys: str, default: Any = None) -> Any:
    """camelCase/snake_case-устойчивое чтение поля блока."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────────────
# 1. Контракт блока (деривация из полей SIMA)
# ──────────────────────────────────────────────────────────────────────

def parse_acceptance(raw: Any) -> List[dict]:
    """Разбор acceptanceCriteria в структурированные проверки.

    Поддерживается JSON-массив строк ИЛИ markdown-строки. Детерминированные
    маркеры (документированы в docs/SIMA_KANON.md):
      file: <glob>                — файл существует в workspace
      contains: <path> :: <regex> — файл содержит паттерн
      cmd: <shell-команда>        — exit code 0 в workspace
    Всё остальное — kind="manual" (человек/LLM-judge, в вердикт pass
    самостоятельно НЕ превращается)."""
    items: List[str] = []
    if not raw:
        return []
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        text = str(raw).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = [x if isinstance(x, str) else json.dumps(
                    x, ensure_ascii=False) for x in parsed]
        except (ValueError, TypeError):
            pass
        if not items:
            for line in text.splitlines():
                line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line)
                line = re.sub(r"^\[[ xX]?\]\s*", "", line).strip()
                if line:
                    items.append(line)

    checks: List[dict] = []
    for it in items:
        m = re.match(r"^file:\s*(.+)$", it, re.IGNORECASE)
        if m:
            checks.append({"kind": "file", "target": m.group(1).strip(),
                           "raw": it})
            continue
        m = re.match(r"^contains:\s*(.+?)\s*::\s*(.+)$", it, re.IGNORECASE)
        if m:
            checks.append({"kind": "contains", "target": m.group(1).strip(),
                           "pattern": m.group(2).strip(), "raw": it})
            continue
        m = re.match(r"^cmd:\s*(.+)$", it, re.IGNORECASE)
        if m:
            checks.append({"kind": "cmd", "target": m.group(1).strip(),
                           "raw": it})
            continue
        checks.append({"kind": "manual", "raw": it})
    return checks


def block_slug(block: dict) -> str:
    """Стабильный slug блока для путей workspace: label + имя."""
    label = str(_g(block, "label", default="")).strip()
    name = str(_g(block, "name", default="block")).strip()
    base = f"{label}-{name}" if label else name
    slug = re.sub(r"[^a-z0-9а-яё]+", "-", base.lower()).strip("-")
    return slug[:60] or str(block.get("id", "block"))[:8]


def derive_contract(block: dict, connections: List[dict],
                    block_idx: Dict[str, dict]) -> dict:
    """Kanon-контракт из существующих полей SIMA-блока.

    depends_on: from-блоки входящих рёбер (data_flow/trigger/dependency) —
    «этот блок потребляет». provides: to-блоки исходящих рёбер."""
    bid = str(block.get("id", ""))
    depends_on, provides = [], []
    for c in connections or []:
        ctype = str(_g(c, "type", default="data_flow"))
        if ctype not in _DEP_EDGE_TYPES:
            continue
        frm = str(_g(c, "fromBlockId", "from_block_id", default=""))
        to = str(_g(c, "toBlockId", "to_block_id", default=""))
        if to == bid and frm:
            src = block_idx.get(frm, {})
            depends_on.append({
                "block_id": frm, "name": _g(src, "name", default="?"),
                "type": ctype,
                "what": _g(c, "dataDescription", "data_description",
                           default=_g(c, "label", default=""))})
        elif frm == bid and to:
            dst = block_idx.get(to, {})
            provides.append({
                "block_id": to, "name": _g(dst, "name", default="?"),
                "type": ctype,
                "what": _g(c, "dataDescription", "data_description",
                           default=_g(c, "label", default=""))})

    mission_parts = [
        _g(block, "goal"), _g(block, "businessValue", "business_value"),
        _g(block, "problemSolved", "problem_solved"),
        _g(block, "description")]
    mission = "\n\n".join(str(p) for p in mission_parts if p)

    return {
        "block_id": bid,
        "slug": block_slug(block),
        "name": _g(block, "name", default="?"),
        "mission": mission,
        "kpi": _g(block, "metrics", default="") or "",
        "acceptance": parse_acceptance(
            _g(block, "acceptanceCriteria", "acceptance_criteria")),
        "depends_on": depends_on,
        "provides": provides,
    }


def implementation_status(contract: dict, block: dict) -> dict:
    """Атласовская панель Implementation Status: contract-vs-reality одной
    строкой на аспект (ok / partial / missing) + готовность к handoff.

    ready_for_handoff = mission заполнена И есть ≥1 ДЕТЕРМИНИРОВАННАЯ
    проверка приёмки (требование Kanon Level 1: блок без проверяемого
    контракта агенту не отдаём)."""
    def _grade(val: Any, min_len: int = 20) -> str:
        s = str(val or "").strip()
        if not s:
            return "missing"
        return "ok" if len(s) >= min_len else "partial"

    acc = contract.get("acceptance") or []
    deterministic = [c for c in acc if c["kind"] != "manual"]
    acceptance_grade = ("ok" if deterministic
                        else ("partial" if acc else "missing"))
    rows = {
        "mission": _grade(contract.get("mission")),
        "kpi": _grade(contract.get("kpi"), min_len=5),
        "acceptance": acceptance_grade,
        "tasks": _grade(_g(block, "userFlow", "user_flow")
                        or _g(block, "implementation")),
        "files": _grade(_g(block, "filesToCreate", "files_to_create"),
                        min_len=5),
        "narrative": _grade(_g(block, "blockNarrative", "block_narrative")),
    }
    return {
        "block_id": contract["block_id"],
        "name": contract["name"],
        "rows": rows,
        "deterministic_checks": len(deterministic),
        "manual_checks": len(acc) - len(deterministic),
        "ready_for_handoff": rows["mission"] == "ok" and bool(deterministic),
    }


def project_kanon_status(project: dict) -> dict:
    """Сводный Kanon-статус проекта: контракт+готовность по каждому блоку."""
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}
    statuses = []
    for b in blocks:
        contract = derive_contract(b, connections, idx)
        statuses.append(implementation_status(contract, b))
    ready = sum(1 for s in statuses if s["ready_for_handoff"])
    return {
        "project_id": str(project.get("id", "")),
        "blocks_total": len(statuses),
        "blocks_ready_for_handoff": ready,
        "blocks": statuses,
    }


# ──────────────────────────────────────────────────────────────────────
# 2. Tri-state верификация (детерминированные коллекторы)
# ──────────────────────────────────────────────────────────────────────

def _collect(check: dict, workspace: str, *, run_commands: bool) -> dict:
    """Один коллектор → evidence {verdict, detail}. Ошибки коллектора —
    inconclusive (не знаем ≠ провал, но и не успех)."""
    kind = check.get("kind")
    try:
        if kind == "file":
            matches = _glob.glob(os.path.join(workspace, check["target"]),
                                 recursive=True)
            return {"verdict": PASS if matches else FAIL,
                    "detail": f"{len(matches)} файл(ов) по {check['target']}"}
        if kind == "contains":
            path = os.path.join(workspace, check["target"])
            if not os.path.isfile(path):
                return {"verdict": FAIL, "detail": f"нет файла {check['target']}"}
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            found = re.search(check["pattern"], content) is not None
            return {"verdict": PASS if found else FAIL,
                    "detail": f"паттерн {'найден' if found else 'не найден'}"}
        if kind == "cmd":
            if not run_commands:
                return {"verdict": INCONCLUSIVE,
                        "detail": "cmd-проверки выключены (run_commands=false)"}
            argv = _safe_cmd_argv(check.get("target", ""))
            if argv is None:
                # Не в allowlist / есть shell-метасимволы → НЕ исполняем.
                return {"verdict": INCONCLUSIVE,
                        "detail": ("команда вне allowlist проверок "
                                   "(разрешены только известные test/build-"
                                   "инструменты без shell-конструкций)")}
            proc = subprocess.run(
                argv, shell=False, cwd=workspace,
                capture_output=True, text=True, timeout=_CMD_TIMEOUT_SEC)
            tail = (proc.stdout + proc.stderr)[-400:]
            return {"verdict": PASS if proc.returncode == 0 else FAIL,
                    "detail": f"rc={proc.returncode}; {tail}"}
        # manual: сам по себе не доказательство
        return {"verdict": INCONCLUSIVE, "detail": "ручная проверка"}
    except Exception as e:
        return {"verdict": INCONCLUSIVE, "detail": f"ошибка коллектора: {e}"}


def verify_block(block: dict, connections: List[dict],
                 block_idx: Dict[str, dict], workspace: str, *,
                 run_commands: bool = False) -> dict:
    """Tri-state вердикт блока против workspace (папки реализации).

    Агрегация по спеке Kanon: любой fail → fail; иначе хоть один
    детерминированный pass → pass; иначе inconclusive. Нет ни одной
    проверки → inconclusive (отсутствие контракта ≠ зелёный)."""
    contract = derive_contract(block, connections, block_idx)
    checks = contract["acceptance"]
    evidence = []
    for c in checks:
        ev = _collect(c, workspace, run_commands=run_commands)
        evidence.append({**c, **ev})

    verdicts = [e["verdict"] for e in evidence]
    if FAIL in verdicts:
        verdict = FAIL
    elif any(v == PASS for v in verdicts):
        verdict = PASS
    else:
        verdict = INCONCLUSIVE
    return {
        "block_id": contract["block_id"],
        "name": contract["name"],
        "verdict": verdict,
        "checked_at": _now_iso(),
        "workspace": workspace,
        "evidence": evidence,
    }


# ──────────────────────────────────────────────────────────────────────
# 2b. Семантический LLM-судья (Kanon R-7.94, sima_atlas) — ОПЦИОНАЛЬНЫЙ слой
# ПОВЕРХ детерминированных коллекторов. Спека Kanon: llm_judge НЕ может быть
# единственным основанием для pass (mock/no-key → inconclusive, никогда не
# ложный pass). Мы используем судью, чтобы (а) ловить «детерминированно
# прошло, но по смыслу неверно» (напр., regex там, где нужен LLM) и (б) дать
# todo_to_pass для рефайна — но КАНОНИЧЕСКИЙ verdict остаётся детерминированным.
# ──────────────────────────────────────────────────────────────────────

_JUDGE_DIMENSIONS = [
    "mission_alignment",       # соответствие смыслу mission + user_story
    "kpi_satisfaction",        # удовлетворяет KPI / замыслу приёмки
    "methodology_compliance",  # соблюдена методология (не regex где нужен LLM)
    "functional_correctness",  # заработает как описано
    "connectivity_consistency",  # связи согласованы с соседями
]

_JUDGE_SYS = (
    "Ты — строгий контракт-арбитр Kanon. Оцениваешь реализацию против "
    "контракта блока по 5 измерениям, каждое tri-state (pass/fail/"
    "inconclusive). НИКОГДА не выдавай pass без явных оснований в коде — "
    "если данных не хватает, ставь inconclusive. Заглушки/mock/нет ключа = "
    "inconclusive, не pass."
)


def semantic_judge_enabled() -> bool:
    """Семантический судья включается env-флагом (по умолчанию ВЫКЛ — это
    доп. LLM-стоимость и он опционален поверх детерминированной приёмки)."""
    return os.getenv("KANON_SEMANTIC_JUDGE", "").strip().lower() in ("on", "1", "true", "yes")


def _gather_impl_excerpt(workspace: str, contract: dict,
                         limit_files: int = 5, limit_chars: int = 6000) -> str:
    """Ограниченный срез кода реализации для судьи: сперва файлы из
    acceptance (file:/contains:), иначе — поверхностный скан исходников."""
    files: List[str] = []
    for c in contract.get("acceptance", []):
        if c.get("kind") in ("file", "contains") and c.get("target"):
            try:
                files += [m for m in _glob.glob(
                    os.path.join(workspace, c["target"]), recursive=True)
                    if os.path.isfile(m)]
            except Exception:
                pass
    if not files:
        _skip = {".git", "node_modules", "atlas", ".venv", "__pycache__", "dist", "build"}
        for root, dirs, fs in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in _skip]
            for f in fs:
                if f.endswith((".ts", ".tsx", ".js", ".jsx", ".py", ".go",
                               ".rs", ".java", ".rb", ".php", ".md")):
                    files.append(os.path.join(root, f))
            if len(files) > 40:
                break
    seen: set = set()
    out: List[str] = []
    total = 0
    for p in files[: limit_files * 3]:
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                content = fh.read(limit_chars)
        except Exception:
            continue
        rel = os.path.relpath(p, workspace)
        chunk = f"### {rel}\n{content}\n"
        out.append(chunk)
        total += len(chunk)
        if total >= limit_chars or len(out) >= limit_files:
            break
    return "\n".join(out)[:limit_chars]


async def semantic_judge(block: dict, connections: List[dict],
                         block_idx: Dict[str, dict], workspace: str) -> dict:
    """LLM-судья по 5 измерениям (Kanon R-7.94). Возвращает
    {verdict, dimensions, todo_to_pass, summary}. Never-raise; при любой
    невозможности оценить — verdict=inconclusive (НЕ pass)."""
    try:
        contract = derive_contract(block, connections, block_idx)
        impl = _gather_impl_excerpt(workspace, contract)
        if not impl.strip():
            return {"verdict": INCONCLUSIVE, "dimensions": {}, "todo_to_pass": [],
                    "summary": "нет кода для оценки"}
        neighbors = "; ".join(
            f"{d['name']}({d['type']})"
            for d in (contract.get("depends_on", []) + contract.get("provides", []))
        ) or "—"
        user_story = str(_g(block, "userFlow", "user_flow", default="") or "")
        acc = [c.get("raw") for c in contract.get("acceptance", [])][:12]
        prompt = (
            f"Контракт блока «{contract['name']}»:\n"
            f"mission: {contract['mission'][:1500]}\n"
            f"KPI: {str(contract['kpi'])[:500]}\n"
            f"acceptance: {acc}\n"
            f"user_story: {user_story[:800]}\n"
            f"связи (соседи): {neighbors}\n\n"
            f"Реализация (фрагменты кода из workspace):\n{impl}\n\n"
            "Оцени по 5 измерениям (pass/fail/inconclusive): mission_alignment, "
            "kpi_satisfaction, methodology_compliance, functional_correctness, "
            "connectivity_consistency. Верни строгий JSON: "
            '{"dimensions": {"mission_alignment": "...", "kpi_satisfaction": "...", '
            '"methodology_compliance": "...", "functional_correctness": "...", '
            '"connectivity_consistency": "..."}, "todo_to_pass": ["что изменить, '
            'чтобы честно выполнить контракт"], "summary": "1-2 фразы"}.'
        )
        from backend.core.llm.router import ModelTier, TaskType, get_llm_router
        res = await get_llm_router().generate_json(
            prompt=prompt, system_prompt=_JUDGE_SYS,
            task_type=TaskType.EXTRACTION, model_tier=ModelTier.STANDARD,
            temperature=0.2)
    except Exception as e:
        return {"verdict": INCONCLUSIVE, "dimensions": {}, "todo_to_pass": [],
                "summary": f"судья недоступен: {e}"}

    raw_dims = res.get("dimensions") if isinstance(res, dict) else {}
    dims: Dict[str, str] = {}
    for k in _JUDGE_DIMENSIONS:
        v = str((raw_dims or {}).get(k, INCONCLUSIVE)).strip().lower()
        dims[k] = v if v in (PASS, FAIL, INCONCLUSIVE) else INCONCLUSIVE
    todo = res.get("todo_to_pass") if isinstance(res, dict) else []
    todo = [str(x) for x in (todo or [])][:12]
    vals = list(dims.values())
    if FAIL in vals:
        verdict = FAIL
    elif vals and all(v == PASS for v in vals):
        verdict = PASS  # НЕ используется для флипа детерминированного verdict
    else:
        verdict = INCONCLUSIVE
    return {"verdict": verdict, "dimensions": dims, "todo_to_pass": todo,
            "summary": str(res.get("summary", ""))[:400] if isinstance(res, dict) else ""}


def reverse_dependents(block_id: str, connections: List[dict]) -> List[str]:
    """Замыкание обратных зависимостей: все блоки, которые (транзитивно)
    потребляют block_id (BFS по исходящим dep-рёбрам)."""
    adj: Dict[str, List[str]] = {}
    for c in connections or []:
        if str(_g(c, "type", default="data_flow")) not in _DEP_EDGE_TYPES:
            continue
        frm = str(_g(c, "fromBlockId", "from_block_id", default=""))
        to = str(_g(c, "toBlockId", "to_block_id", default=""))
        if frm and to:
            adj.setdefault(frm, []).append(to)
    seen, queue, out = {block_id}, [block_id], []
    while queue:
        cur = queue.pop(0)
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                out.append(nxt)
                queue.append(nxt)
    return out


def cascade_verify(block_id: str, project: dict, workspace: str, *,
                   run_commands: bool = False,
                   verify_fn: Optional[Callable[..., dict]] = None) -> dict:
    """Kanon cascade: после изменения/прогона блока X перепроверить ВСЕХ
    его (транзитивных) потребителей. fail → desync (сразу, не «утром»);
    pass → green; inconclusive → unknown."""
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}
    vf = verify_fn or verify_block

    results, desync = [], []
    for dep_id in reverse_dependents(block_id, connections):
        dep = idx.get(dep_id)
        if not dep:
            continue
        res = vf(dep, connections, idx, workspace,
                 run_commands=run_commands)
        state = {PASS: "green", FAIL: "desync"}.get(res["verdict"], "unknown")
        res["cascade_state"] = state
        if state == "desync":
            desync.append(dep_id)
        results.append(res)
    return {"source_block": block_id, "checked": len(results),
            "desync": desync, "results": results}


# ──────────────────────────────────────────────────────────────────────
# 3. Kanon-workspace (экспорт контрактов для кодинг-агента)
# ──────────────────────────────────────────────────────────────────────

_AGENTS_MD = """# Навигация для кодинг-агента (Kanon Protocol)

Порядок чтения (НЕ читай весь репозиторий — только это):
1. atlas/project.md — что за продукт и зачем.
2. atlas/architecture_decisions.md — принятые решения (append-only, НЕ
   отменяй молча: хочешь изменить — допиши новое решение с обоснованием).
3. Контракт целевого блока: atlas/blocks/<id>/contract/mission.md →
   acceptance.md → depends_on.json → provides.json.
4. atlas/blocks/<id>/narrative.md — что уже пробовали (append-only,
   дописывай результат своего прогона в конец).

Правила:
- Контракт — арбитр: код противоречит контракту → меняется код.
- Блок считается готовым только если acceptance-проверки проходят
  (pass), а не «выглядит сделанным».
- atlas/operator_profile.md (если есть): правила оператора. Правила
  «НЕ использовать» с пометкой [hard] нарушать НЕЛЬЗЯ — прогон будет
  завален drift-сканером.
- ТЗ целиком: atlas/auto_tz.md.
"""


def _contract_files(contract: dict, block: dict) -> Dict[str, str]:
    base = f"atlas/blocks/{contract['slug']}"
    acc_lines = []
    for c in contract["acceptance"]:
        marker = "" if c["kind"] == "manual" else f" `[{c['kind']}]`"
        acc_lines.append(f"- [ ] {c['raw']}{marker}")
    if not acc_lines:
        acc_lines = ["- [ ] (критерии приёмки не заданы — блок нельзя "
                     "считать готовым; вердикт всегда inconclusive)"]
    kpi = contract.get("kpi") or ""
    mission_md = (f"# {contract['name']}\n\n{contract['mission'] or '—'}\n"
                  + (f"\n## KPI\n{kpi}\n" if kpi else ""))
    impl = _g(block, "implementation")
    tech = _g(block, "blockTechStack", "block_tech_stack")
    api = _g(block, "apiEndpoints", "api_endpoints")
    how = "".join([
        f"\n## Реализация\n{impl}\n" if impl else "",
        f"\n## Технологии\n{tech}\n" if tech else "",
        f"\n## API\n{api}\n" if api else "",
    ])
    return {
        f"{base}/contract/mission.md": mission_md + how,
        f"{base}/contract/acceptance.md":
            "# Критерии приёмки (tri-state: pass/fail/inconclusive)\n\n"
            + "\n".join(acc_lines) + "\n",
        f"{base}/contract/depends_on.json": json.dumps(
            contract["depends_on"], ensure_ascii=False, indent=1),
        f"{base}/contract/provides.json": json.dumps(
            contract["provides"], ensure_ascii=False, indent=1),
        f"{base}/narrative.md":
            f"# Narrative: {contract['name']} (append-only)\n\n"
            f"- {_now_iso()} workspace создан из SIMA-проекта\n",
    }


def operator_profile_md(profile: dict) -> str:
    """operator_profile → markdown для workspace (типизированная память
    Kanon §5: lessons / dont_use / always_use, hard→fail soft→warn)."""
    lines = ["# Профиль оператора (типизированная память)", ""]
    dont = profile.get("dont_use") or []
    always = profile.get("always_use") or []
    lessons = profile.get("lessons") or []
    if dont:
        lines += ["## НЕ использовать", ""]
        for r in dont:
            sev = r.get("severity", "soft")
            lines.append(f"- [{sev}] {r.get('rule', '')}"
                         + (f" — {r['reason']}" if r.get("reason") else ""))
        lines.append("")
    if always:
        lines += ["## Всегда использовать", ""]
        for r in always:
            lines.append(f"- {r.get('rule', '')}"
                         + (f" — {r['reason']}" if r.get("reason") else ""))
        lines.append("")
    if lessons:
        lines += ["## Уроки прошлых прогонов", ""]
        for r in lessons:
            lines.append(f"- {r.get('rule', '')}")
        lines.append("")
    if len(lines) == 2:
        lines.append("(профиль пуст)")
    return "\n".join(lines) + "\n"


def build_kanon_workspace(project: dict,
                          decisions: Optional[List[dict]] = None,
                          operator_profile: Optional[dict] = None
                          ) -> Dict[str, str]:
    """Файлы Kanon-workspace (path → content) из SIMA-проекта.

    Контракты per-block + project.md + architecture_decisions.md (из
    SIMA Decisions) + operator_profile.md (типизированная память
    оператора) + AGENTS.md/CLAUDE.md + auto_tz.md (generatedTZ)."""
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}

    files: Dict[str, str] = {"AGENTS.md": _AGENTS_MD, "CLAUDE.md": _AGENTS_MD}
    if operator_profile and any(operator_profile.get(k)
                                for k in ("dont_use", "always_use", "lessons")):
        files["atlas/operator_profile.md"] = operator_profile_md(operator_profile)

    info = [f"# {project.get('name', 'Проект')}", ""]
    for label, key in (("Описание", "description"), ("Цель", "goal"),
                       ("Аудитория", "targetAudience"),
                       ("MVP", "mvpDescription"),
                       ("Проблема", "narrativeProblem"),
                       ("Решение", "narrativeSolution"),
                       ("Как работает", "narrativeHowItWorks")):
        v = _g(project, key, re.sub(r"([A-Z])", r"_\1", key).lower())
        if v:
            info.append(f"**{label}:** {v}\n")
    files["atlas/project.md"] = "\n".join(info)

    dec_lines = ["# Архитектурные решения (append-only)",
                 "", "Не отменяются молча: новое решение дописывается ниже "
                 "со ссылкой на старое.", ""]
    for d in decisions or []:
        dec_lines.append(
            f"- **{_g(d, 'title', default='решение')}** — "
            f"{_g(d, 'description', default='')}"
            f" _(обоснование: {_g(d, 'reasoning', default='—')})_")
    files["atlas/architecture_decisions.md"] = "\n".join(dec_lines) + "\n"

    tz = _g(project, "generatedTZ", "generated_tz")
    if tz:
        files["atlas/auto_tz.md"] = str(tz)

    graph = {
        "blocks": [{"id": str(b.get("id")), "slug": block_slug(b),
                    "name": _g(b, "name"), "type": _g(b, "type"),
                    "layer": _g(b, "layer")} for b in blocks],
        "edges": [{"from": str(_g(c, "fromBlockId", "from_block_id")),
                   "to": str(_g(c, "toBlockId", "to_block_id")),
                   "type": _g(c, "type")} for c in connections],
    }
    files["atlas/graph.json"] = json.dumps(graph, ensure_ascii=False, indent=1)

    for b in blocks:
        contract = derive_contract(b, connections, idx)
        files.update(_contract_files(contract, b))

    # документация — проекция графа (перегенерируется, не пишется вручную)
    files["atlas/WIKI.md"] = generate_wiki(project, decisions)
    for slug, doc in generate_user_docs(project).items():
        files[f"atlas/docs/end-user/{slug}.md"] = doc
    return files


def write_workspace(files: Dict[str, str], folder: str) -> List[str]:
    """Записать файлы workspace на диск. narrative.md и
    architecture_decisions.md append-only — существующие НЕ перезаписываются."""
    written = []
    for rel, content in files.items():
        path = os.path.join(folder, rel)
        if (os.path.exists(path)
                and (rel.endswith("narrative.md")
                     or rel.endswith("architecture_decisions.md"))):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
    return written


def append_narrative(folder: str, slug: str, line: str) -> bool:
    """Дописать строку в narrative.md блока (append-only память прогонов)."""
    path = os.path.join(folder, "atlas", "blocks", slug, "narrative.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {_now_iso()} {line}\n")
        return True
    except OSError:
        return False


# ──────────────────────────────────────────────────────────────────────
# 3b. Документация — проекция графа (Kanon §7, atlas generate_wiki/
#     generate_user_docs): WIKI и туториалы НЕ пишутся вручную, а
#     генерируются из контрактов. Детерминированно, без LLM —
#     одинаковый проект → байт-в-байт одинаковый документ.
# ──────────────────────────────────────────────────────────────────────

_VERDICT_MARKS = {PASS: "✅", FAIL: "❌", INCONCLUSIVE: "❔"}


def generate_wiki(project: dict,
                  decisions: Optional[List[dict]] = None,
                  verdicts: Optional[dict] = None) -> str:
    """WIKI проекта из контрактов: обзор, блоки по слоям (миссия/KPI/
    приёмка/зависимости/вердикт), mermaid-граф, решения.

    verdicts: {block_id: {"verdict": ...}} из KanonStore — статусная
    колонка честная (нет вердикта → «не проверялся», не зелёный)."""
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}
    verdicts = verdicts or {}

    out = [f"# WIKI: {project.get('name', 'Проект')}",
           "", "> Сгенерировано из контрактов блоков — не редактировать "
           "вручную (перегенерируется).", ""]
    for label, keys in (("Цель", ("goal",)),
                        ("Аудитория", ("targetAudience", "target_audience")),
                        ("Проблема", ("narrativeProblem", "narrative_problem")),
                        ("Решение", ("narrativeSolution", "narrative_solution"))):
        v = _g(project, *keys)
        if v:
            out.append(f"**{label}:** {v}\n")

    # mermaid-граф зависимостей (по лейблам блоков)
    if blocks:
        out += ["## Карта продукта", "", "```mermaid", "graph TD"]
        for b in blocks:
            bid = str(b.get("id"))
            name = str(_g(b, "name", default="?")).replace('"', "'")
            out.append(f'  {_mermaid_id(bid)}["{name}"]')
        for c in connections:
            frm = str(_g(c, "fromBlockId", "from_block_id", default=""))
            to = str(_g(c, "toBlockId", "to_block_id", default=""))
            if frm in idx and to in idx:
                lbl = str(_g(c, "label", default="") or _g(c, "type", default=""))
                arrow = f' -->|"{lbl}"| ' if lbl else " --> "
                out.append(f"  {_mermaid_id(frm)}{arrow}{_mermaid_id(to)}")
        out += ["```", ""]

    # блоки по слоям
    by_layer: Dict[str, List[dict]] = {}
    for b in blocks:
        by_layer.setdefault(str(_g(b, "layer", default="mvp")), []).append(b)
    for layer in sorted(by_layer):
        out.append(f"## Слой: {layer}")
        out.append("")
        for b in sorted(by_layer[layer], key=lambda x: str(_g(x, "label", default=""))):
            contract = derive_contract(b, connections, idx)
            v = (verdicts.get(contract["block_id"]) or {}).get("verdict")
            mark = _VERDICT_MARKS.get(v, "▫️")
            status = v or "не проверялся"
            out.append(f"### {mark} {contract['name']} "
                       f"({_g(b, 'type', default='feature')}) — {status}")
            out.append("")
            if contract["mission"]:
                out.append(contract["mission"].split("\n\n")[0])
                out.append("")
            if contract["kpi"]:
                out.append(f"**KPI:** {contract['kpi']}\n")
            if contract["acceptance"]:
                det = sum(1 for c in contract["acceptance"] if c["kind"] != "manual")
                out.append(f"**Приёмка:** {len(contract['acceptance'])} "
                           f"проверок (детерминированных: {det})\n")
            if contract["depends_on"]:
                deps = ", ".join(d["name"] for d in contract["depends_on"])
                out.append(f"**Зависит от:** {deps}\n")
            if contract["provides"]:
                provs = ", ".join(d["name"] for d in contract["provides"])
                out.append(f"**Отдаёт:** {provs}\n")

    if decisions:
        out += ["## Архитектурные решения", ""]
        for d in decisions:
            out.append(f"- **{_g(d, 'title', default='решение')}** — "
                       f"{_g(d, 'description', default='')}")
        out.append("")
    return "\n".join(out)


def _mermaid_id(block_id: str) -> str:
    return "n" + re.sub(r"[^a-zA-Z0-9]", "", str(block_id))[:10]


def generate_user_docs(project: dict) -> Dict[str, str]:
    """Туториалы для конечного пользователя — по блокам, у которых ЕСТЬ
    пользовательское содержимое (userBenefit/userFlow/description).

    Честность: блок без user-facing полей пропускается, а не заполняется
    выдумкой. Возвращает {slug: markdown}."""
    blocks = project.get("blocks") or []
    docs: Dict[str, str] = {}
    for b in blocks:
        benefit = _g(b, "userBenefit", "user_benefit")
        flow = _g(b, "userFlow", "user_flow")
        desc = _g(b, "description")
        if not (benefit or flow):
            continue        # нечего рассказать пользователю — не выдумываем
        name = _g(b, "name", default="?")
        lines = [f"# Как пользоваться: {name}", ""]
        if desc:
            lines += [str(desc), ""]
        if benefit:
            lines += ["## Что это даёт", "", str(benefit), ""]
        if flow:
            lines += ["## Как это работает (шаги)", ""]
            flow_s = str(flow)
            steps = [s.strip() for s in re.split(r"\n|(?:\d+[.)]\s*)", flow_s)
                     if s and s.strip()]
            if len(steps) > 1:
                lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
            else:
                lines.append(flow_s)
            lines.append("")
        edge = _g(b, "edgeCases", "edge_cases")
        if edge:
            lines += ["## Пограничные случаи", "", str(edge), ""]
        lines += ["---", "_Сгенерировано из контракта блока — не "
                  "редактировать вручную._", ""]
        docs[block_slug(b)] = "\n".join(lines)
    return docs


# ──────────────────────────────────────────────────────────────────────
# 4. Типизированная память оператора + drift-сканер (Kanon §5, R-7.82)
# ──────────────────────────────────────────────────────────────────────

_PROFILE_KINDS = ("lessons", "dont_use", "always_use")


class OperatorProfile:
    """Per-user память оператора, переживающая проекты: lessons.json /
    dont_use.json / always_use.json из atlas — здесь одним JSON.

    dont_use-правило: {"rule": подстрока-или-/regex/, "severity":
    "hard"|"soft", "reason": ...}. hard → drift-сканер валит прогон,
    soft → предупреждение."""

    def __init__(self, persist_path: str):
        self._path = persist_path

    def get(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return {k: list(data.get(k) or []) for k in _PROFILE_KINDS}

    def add(self, kind: str, rule: str, *, severity: str = "soft",
            reason: str = "") -> dict:
        if kind not in _PROFILE_KINDS:
            raise ValueError(f"kind must be one of {_PROFILE_KINDS}")
        if severity not in ("hard", "soft"):
            raise ValueError("severity must be hard|soft")
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self.get()
            entry = {"rule": str(rule).strip(), "severity": severity,
                     "reason": reason, "added_at": _now_iso()}
            # идемпотентность по тексту правила
            if not any(r.get("rule") == entry["rule"] for r in data[kind]):
                data[kind].append(entry)
            atomic_write_json(self._path, data)
        return self.get()

    def remove(self, kind: str, rule: str) -> bool:
        if kind not in _PROFILE_KINDS:
            return False
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self.get()
            before = len(data[kind])
            data[kind] = [r for r in data[kind] if r.get("rule") != rule]
            atomic_write_json(self._path, data)
            return len(data[kind]) < before


_DRIFT_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go",
                     ".rs", ".java", ".rb", ".php", ".sql", ".sh", ".yml",
                     ".yaml", ".toml", ".json", ".md"}
_DRIFT_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist",
                    "build", "__pycache__", ".next"}
_DRIFT_MAX_FILES = 2000
_DRIFT_MAX_BYTES = 512 * 1024


def _rule_matcher(rule: str):
    """Правило — подстрока (case-insensitive) или /regex/."""
    rule = rule.strip()
    if len(rule) > 2 and rule.startswith("/") and rule.endswith("/"):
        try:
            rx = re.compile(rule[1:-1], re.IGNORECASE)
            return lambda text: rx.search(text) is not None
        except re.error:
            pass
    low = rule.lower()
    return lambda text: low in text.lower()


def scan_for_drift(workspace: str, profile: dict) -> dict:
    """Пост-прогонный сканер кода против dont_use (atlas R-7.82, S-3).

    hard-нарушение → verdict fail (прогон нельзя принять), soft → warn.
    Сканируются только кодовые/конфиг файлы, без node_modules/.git."""
    rules = [(r, _rule_matcher(r.get("rule", "")))
             for r in (profile.get("dont_use") or []) if r.get("rule")]
    if not rules:
        return {"verdict": PASS, "violations": [], "warnings": [],
                "files_scanned": 0}

    violations, warnings = [], []
    scanned = 0
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _DRIFT_SKIP_DIRS]
        for name in names:
            if os.path.splitext(name)[1].lower() not in _DRIFT_EXTENSIONS:
                continue
            if scanned >= _DRIFT_MAX_FILES:
                break
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > _DRIFT_MAX_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            scanned += 1
            rel = os.path.relpath(path, workspace)
            for rule, match in rules:
                if match(text):
                    hit = {"file": rel, "rule": rule.get("rule"),
                           "severity": rule.get("severity", "soft"),
                           "reason": rule.get("reason", "")}
                    (violations if hit["severity"] == "hard"
                     else warnings).append(hit)
    return {"verdict": FAIL if violations else PASS,
            "violations": violations, "warnings": warnings,
            "files_scanned": scanned}


# ──────────────────────────────────────────────────────────────────────
# 5. Context-pack (узкий контекст агенту вместо «моря», atlas R-7.86)
# ──────────────────────────────────────────────────────────────────────

# Профиль определяет, какие части контекста нужны задаче:
#   design          — новый блок / крупный рефакторинг (полный пакет)
#   backend-fix     — правка логики (контракт + соседи, без нарратива продукта)
#   ui-fix          — фронтенд (без upstream-зависимостей)
#   acceptance-only — чистая верификация (только критерии приёмки)
_PACK_PROFILES = {
    "design": {"project": True, "decisions": True, "operator": True,
               "neighbors": True, "acceptance": True, "how": True},
    "backend-fix": {"project": False, "decisions": True, "operator": True,
                    "neighbors": True, "acceptance": True, "how": True},
    "ui-fix": {"project": False, "decisions": True, "operator": True,
               "neighbors": False, "acceptance": True, "how": True},
    "acceptance-only": {"project": False, "decisions": False,
                        "operator": False, "neighbors": False,
                        "acceptance": True, "how": False},
}


def build_context_pack(project: dict, block_id: str, *,
                       profile: str = "design",
                       decisions: Optional[List[dict]] = None,
                       operator_profile: Optional[dict] = None) -> dict:
    """Детерминированный context-pack для одного блока.

    Граф вместо моря контекста: контракт блока + однострочные миссии
    соседей по графу — НИКОГДА не весь проект."""
    if profile not in _PACK_PROFILES:
        raise ValueError(f"profile must be one of {list(_PACK_PROFILES)}")
    cfg = _PACK_PROFILES[profile]
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}
    block = idx.get(str(block_id))
    if not block:
        raise ValueError(f"block {block_id} not found")
    contract = derive_contract(block, connections, idx)

    pack: dict = {"profile": profile, "block": {
        "id": contract["block_id"], "name": contract["name"],
        "slug": contract["slug"],
        "mission": contract["mission"],
        "kpi": contract["kpi"],
    }}
    if cfg["acceptance"]:
        pack["block"]["acceptance"] = contract["acceptance"]
    if cfg["how"]:
        for key, src in (("implementation", ("implementation",)),
                         ("tech_stack", ("blockTechStack", "block_tech_stack")),
                         ("api_endpoints", ("apiEndpoints", "api_endpoints")),
                         ("files_to_create", ("filesToCreate", "files_to_create"))):
            v = _g(block, *src)
            if v:
                pack["block"][key] = v
    if cfg["neighbors"]:
        def _brief(ref: dict) -> dict:
            nb = idx.get(ref["block_id"], {})
            return {"name": ref["name"], "what": ref.get("what", ""),
                    "mission_brief": str(_g(nb, "goal", "description",
                                            default=""))[:200]}
        pack["depends_on"] = [_brief(r) for r in contract["depends_on"]]
        pack["provides"] = [_brief(r) for r in contract["provides"]]
    if cfg["project"]:
        pack["project"] = {
            "name": project.get("name"),
            "goal": _g(project, "goal", default=""),
            "audience": _g(project, "targetAudience", "target_audience",
                           default=""),
            "problem": _g(project, "narrativeProblem", "narrative_problem",
                          default=""),
            "solution": _g(project, "narrativeSolution",
                           "narrative_solution", default=""),
        }
    if cfg["decisions"] and decisions:
        pack["architecture_decisions"] = [
            {"title": _g(d, "title", default=""),
             "description": _g(d, "description", default=""),
             "reasoning": _g(d, "reasoning", default="")}
            for d in decisions]
    if cfg["operator"] and operator_profile:
        op = {k: operator_profile.get(k) for k in _PROFILE_KINDS
              if operator_profile.get(k)}
        if op:
            pack["operator_profile"] = op
    return pack


def context_pack_md(pack: dict) -> str:
    """Context-pack → markdown-промпт для кодинг-агента."""
    b = pack["block"]
    lines = [f"# Блок: {b['name']}", "", b.get("mission") or "—", ""]
    if b.get("kpi"):
        lines += ["## KPI", b["kpi"], ""]
    if b.get("acceptance"):
        lines += ["## Критерии приёмки (tri-state)", ""]
        for c in b["acceptance"]:
            marker = "" if c["kind"] == "manual" else f" `[{c['kind']}]`"
            lines.append(f"- [ ] {c['raw']}{marker}")
        lines.append("")
    for key, title in (("implementation", "Реализация"),
                       ("tech_stack", "Технологии"),
                       ("api_endpoints", "API"),
                       ("files_to_create", "Файлы")):
        if b.get(key):
            lines += [f"## {title}", str(b[key]), ""]
    if pack.get("depends_on"):
        lines += ["## Зависит от", ""]
        for d in pack["depends_on"]:
            lines.append(f"- {d['name']}: {d.get('what') or d.get('mission_brief', '')}")
        lines.append("")
    if pack.get("provides"):
        lines += ["## Отдаёт", ""]
        for d in pack["provides"]:
            lines.append(f"- {d['name']}: {d.get('what') or ''}")
        lines.append("")
    if pack.get("project"):
        p = pack["project"]
        lines += ["## Контекст продукта",
                  f"{p.get('name', '')}: {p.get('goal', '')}",
                  f"Проблема: {p.get('problem', '')}",
                  f"Решение: {p.get('solution', '')}", ""]
    if pack.get("architecture_decisions"):
        lines += ["## Архитектурные решения (не отменять молча)", ""]
        for d in pack["architecture_decisions"]:
            lines.append(f"- {d['title']}: {d['description']}"
                         + (f" ({d['reasoning']})" if d.get("reasoning") else ""))
        lines.append("")
    if pack.get("operator_profile"):
        lines += [operator_profile_md(pack["operator_profile"])]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# 6. Runnable-блоки (для batch-handoff / agent-loop поверх confirm)
# ──────────────────────────────────────────────────────────────────────

def pick_runnable_blocks(project: dict,
                         verdicts: Optional[dict] = None) -> List[dict]:
    """Блоки, готовые к передаче агенту (атласовский agent-loop, но БЕЗ
    автозапуска — только кандидаты для PENDING-пачки):

    1) ready_for_handoff (mission + ≥1 детерминированная проверка);
    2) ещё не pass по последнему вердикту;
    3) все depends_on-блоки либо pass, либо без вердикта-fail
       (на fail-фундаменте не строим)."""
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}
    verdicts = verdicts or {}

    out = []
    for b in blocks:
        contract = derive_contract(b, connections, idx)
        st = implementation_status(contract, b)
        if not st["ready_for_handoff"]:
            continue
        own = (verdicts.get(contract["block_id"]) or {}).get("verdict")
        if own == PASS:
            continue
        deps_ok = all(
            (verdicts.get(d["block_id"]) or {}).get("verdict") != FAIL
            for d in contract["depends_on"])
        if not deps_ok:
            continue
        out.append({"block_id": contract["block_id"],
                    "name": contract["name"], "slug": contract["slug"],
                    "deterministic_checks": st["deterministic_checks"]})
    return out


# ──────────────────────────────────────────────────────────────────────
# 7. Персистенс вердиктов (per-user, паттерн остальных сторов)
# ──────────────────────────────────────────────────────────────────────

class KanonStore:
    """Вердикты верификаций per-user: {project_id: {block_id: запись}}.
    Лента desync-ов — чтобы UI красил блоки, как канвас в atlas."""

    MAX_HISTORY = 20

    def __init__(self, persist_path: str):
        self._path = persist_path

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def record(self, project_id: str, results: List[dict]) -> dict:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            proj = data.setdefault(str(project_id), {})
            for r in results:
                bid = str(r.get("block_id", ""))
                if not bid:
                    continue
                slot = proj.setdefault(bid, {"history": []})
                slot["verdict"] = r.get("verdict")
                slot["cascade_state"] = r.get("cascade_state")
                slot["checked_at"] = r.get("checked_at")
                slot["history"].append({
                    "verdict": r.get("verdict"),
                    "checked_at": r.get("checked_at")})
                slot["history"] = slot["history"][-self.MAX_HISTORY:]
            atomic_write_json(self._path, data)
        return {"saved": len(results)}

    def get_project(self, project_id: str) -> dict:
        return self._load().get(str(project_id), {})


def kanon_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_sima_kanon)
    except Exception:
        return False
