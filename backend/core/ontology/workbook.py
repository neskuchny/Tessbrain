# -*- coding: utf-8 -*-
"""Воркбук датасета: LLM пишет pandas-код, код исполняется в ПЕСОЧНИЦЕ.

Расширение DSL-подхода для вопросов, которые в 11 операций не влезают
(корреляции, оконные вычисления, произвольная логика). Философия та же,
что в query_engine («LLM не трогает цифры руками»), но на ступень выше:
LLM пишет КОД, код проходит статический deny-list, исполняется в
изолированном subprocess (python -I, пустое окружение, урезанные
builtins, таймаут) и обязан вернуть JSON-результат. Ответ помечается
«вычислено кодом» и приходит вместе с кодом — пользователь может
проверить логику.

Включение: TESSENT_DATASET_WORKBOOK=on (по умолчанию on). Требует
установленный pandas; без него — честный отказ с подсказкой.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 25
_MAX_CODE_LEN = 4000
_MAX_RESULT_LEN = 20000

# Статический deny-list: код с этими паттернами не исполняется вовсе.
# Импорты запрещены целиком — pd/df уже даны, больше ничего не нужно.
_DENY = [
    r"\bimport\b", r"__", r"\bopen\s*\(", r"\bexec\b", r"\beval\b",
    r"\bcompile\b", r"\bglobals\b", r"\blocals\b", r"\bvars\s*\(",
    r"\bgetattr\b", r"\bsetattr\b", r"\bdelattr\b", r"\binput\s*\(",
    r"\bbreakpoint\b", r"\bos\b", r"\bsys\b", r"subprocess", r"socket",
    r"urllib", r"requests", r"http", r"pathlib", r"shutil", r"pickle",
    # Чтение чего-либо в песочнице не нужно вовсе: df уже построен из строк
    # датасета. Перечислять читатели поимённо было ошибкой — мимо списка
    # проходили read_table, read_fwf, read_xml, read_stata, read_feather и
    # далее по мере роста pandas. Запрещаем весь класс.
    r"\bread_\w+",
    # Обход в numpy и приватные потроха pandas через атрибуты.
    r"pd\.(io|core|compat|api|_libs|util|testing)\b",
    r"\.attrs\b", r"DataFrame\.from_records\s*\(\s*open",
]
_DENY_RE = [re.compile(p, re.IGNORECASE) for p in _DENY]

# Экспорт (`to_*`) — тот же класс проблемы с другой стороны: to_json/to_html/
# to_xml/to_stata писали произвольные файлы мимо поимённого списка. Поэтому
# здесь не чёрный список, а БЕЛЫЙ: разрешены только преобразования в памяти.
_TO_ALLOWED = frozenset({
    "to_dict", "to_list", "to_numpy", "to_frame", "to_series",
    "to_datetime", "to_numeric", "to_timedelta", "to_period",
    "to_string", "to_markdown", "to_records", "to_flat_index",
})
_TO_RE = re.compile(r"\bto_\w+", re.IGNORECASE)

# Обёртка-раннер: читает {rows, code} из stdin, строит df, исполняет код
# с урезанными builtins, печатает {"result", "explanation"} в stdout.
_RUNNER = r'''
import json, sys
payload = json.loads(sys.stdin.read())
import pandas as pd
df = pd.DataFrame(payload["rows"])
SAFE_BUILTINS = {n: __builtins__[n] if isinstance(__builtins__, dict)
                 else getattr(__builtins__, n)
                 for n in ("len", "range", "min", "max", "sum", "sorted",
                           "round", "abs", "float", "int", "str", "dict",
                           "list", "set", "tuple", "enumerate", "zip",
                           "map", "filter", "any", "all", "bool",
                           "isinstance", "reversed", "divmod", "pow",
                           "print", "Exception", "ValueError",
                           "ZeroDivisionError", "KeyError", "TypeError")}
ns = {"df": df, "pd": pd, "__builtins__": SAFE_BUILTINS,
      "result": None, "explanation": ""}
exec(compile(payload["code"], "<workbook>", "exec"), ns)


def _clean(v):
    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, np.ndarray):
            return [_clean(x) for x in v.tolist()]
    except Exception:
        pass
    if isinstance(v, pd.DataFrame):
        return v.head(50).to_dict(orient="records")
    if isinstance(v, pd.Series):
        return {str(k): _clean(x) for k, x in v.head(50).items()}
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in list(v.items())[:100]}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in list(v)[:100]]
    return v


print(json.dumps({"result": _clean(ns.get("result")),
                  "explanation": str(ns.get("explanation") or "")[:800]},
                 ensure_ascii=False, default=str))
'''


def workbook_enabled() -> bool:
    return (os.getenv("TESSENT_DATASET_WORKBOOK", "on") or "").strip().lower() \
        in ("on", "1", "true", "yes")


def pandas_available() -> bool:
    try:
        import pandas  # noqa: F401
        return True
    except Exception:
        return False


def _deny_check(code: str) -> Optional[str]:
    for rx in _DENY_RE:
        if rx.search(code):
            return f"код отклонён политикой безопасности: паттерн {rx.pattern!r}"
    for m in _TO_RE.findall(code or ""):
        if m.lower() not in _TO_ALLOWED:
            return (f"код отклонён политикой безопасности: экспорт {m!r} "
                    f"не входит в список разрешённых преобразований")
    return None


async def _gen_code(question: str, dataset: dict,
                    prev_error: Optional[str] = None) -> Optional[str]:
    """LLM пишет pandas-код по жёсткому контракту."""
    try:
        from backend.core.llm.router import ModelTier, TaskType, get_llm_router
        profile = dataset.get("profile") or {}
        cols = "\n".join(
            f"- {p['name']} ({p['dtype']}; примеры: "
            f"{', '.join(str(s) for s in p.get('samples', [])[:3])})"
            for p in profile.get("columns", []))
        retry = (f"\nПредыдущая попытка упала с ошибкой:\n{prev_error}\n"
                 "Исправь её.") if prev_error else ""
        prompt = (
            "Напиши pandas-код, отвечающий на вопрос по таблице.\n"
            f"Колонки df:\n{cols}\n\n"
            f"Вопрос: {question}\n\n"
            "ЖЁСТКИЙ КОНТРАКТ:\n"
            "- df (pandas.DataFrame) и pd уже определены; НИКАКИХ import;\n"
            "- никакого чтения/записи файлов и сети;\n"
            "- числовые колонки могут быть строками: используй "
            "pd.to_numeric(df[...], errors='coerce');\n"
            "- положи ответ в переменную result (число/строка/dict/list/"
            "DataFrame), короткое пояснение на русском — в explanation;\n"
            "- верни ТОЛЬКО код, без markdown-ограждений." + retry)
        out = await get_llm_router().generate(
            prompt=prompt, task_type=TaskType.REASONING,
            model_tier=ModelTier.STANDARD, temperature=0.0, max_tokens=1200)
        code = (out or "").strip()
        code = re.sub(r"^```(?:python)?\s*|\s*```$", "", code).strip()
        return code[:_MAX_CODE_LEN] or None
    except Exception as e:
        logger.warning(f"workbook codegen failed: {e}")
        return None


async def _run_sandboxed(code: str, rows: List[dict]) -> dict:
    """Исполнить код в изолированном subprocess. Возвращает
    {ok, result?, explanation?, error?}."""
    with tempfile.TemporaryDirectory() as td:
        runner = os.path.join(td, "runner.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(_RUNNER)
        # Окружение строится С НУЛЯ: никаких API-ключей/прокси родителя.
        # -I нельзя: он игнорирует PYTHONPATH и pandas из venv не найдётся;
        # изоляция достигается пустым env + deny-list + урезанные builtins
        # + таймаут + временный cwd.
        env = {"PATH": os.environ.get("PATH", ""),
               "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "HOME": td, "TMPDIR": td}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", runner,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=td, env=env)
        payload = json.dumps({"rows": rows, "code": code},
                             ensure_ascii=False, default=str).encode()
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload), timeout=_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": f"таймаут {_TIMEOUT_SEC}с"}
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "error": "; ".join(tail[-3:])[:500] or "ошибка исполнения"}
    try:
        data = json.loads((out or b"").decode("utf-8", "replace")[:_MAX_RESULT_LEN])
        return {"ok": True, **data}
    except Exception:
        return {"ok": False, "error": "результат не является JSON"}


async def run_workbook(user_id: str, dataset_id: str, question: str,
                       registry=None) -> dict:
    """Полный цикл воркбука: код от LLM → deny-list → песочница →
    результат с кодом (одна повторная попытка при ошибке исполнения)."""
    if not workbook_enabled():
        return {"success": False, "error": "воркбук выключен "
                "(TESSENT_DATASET_WORKBOOK=off)"}
    if not pandas_available():
        return {"success": False, "error": "pandas не установлен на сервере — "
                "pip install pandas, затем повторите"}
    from backend.core.ontology.dataset_service import registry_for_user
    reg = registry or registry_for_user(user_id)
    dataset = reg.get(dataset_id)
    if not dataset:
        return {"success": False, "error": "датасет не найден"}
    rows = reg.load_rows(dataset_id)
    if not rows:
        return {"success": False, "error": "в датасете нет строк"}

    prev_error: Optional[str] = None
    for attempt in (1, 2):
        code = await _gen_code(question, dataset, prev_error)
        if not code:
            return {"success": False, "error": "LLM не сгенерировала код"}
        denied = _deny_check(code)
        if denied:
            logger.warning(f"workbook deny: {denied}")
            return {"success": False, "error": denied, "code": code}
        run = await _run_sandboxed(code, rows)
        if run.get("ok"):
            reg.add_finding(dataset_id, {
                "question": question, "mode": "workbook",
                "result_preview": str(run.get("result"))[:300],
                "confidence": "medium",
            })
            return {
                "success": True, "mode": "workbook", "question": question,
                "answer": run.get("result"),
                "explanation": run.get("explanation") or "",
                "code": code,
                "assessment": {
                    "confidence": "medium",
                    "notes": ["вычислено сгенерированным pandas-кодом в "
                              "песочнице — код приложен, сверь логику",
                              f"строк в данных: {len(rows)}"],
                },
                "dataset": {"id": dataset_id, "title": dataset.get("title"),
                            "data_as_of": dataset.get("refreshed_at")},
            }
        prev_error = run.get("error")
        logger.info(f"workbook attempt {attempt} failed: {prev_error}")
    return {"success": False,
            "error": f"код не выполнился за 2 попытки: {prev_error}",
            "code": code}
