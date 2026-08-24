# -*- coding: utf-8 -*-
"""
Изолированный код-раннер для сложных вычислений в разборе документов.

Когда safe_calc (AST-арифметика) не хватает — например, надо посчитать по
таблице циклом/группировкой — методика может задать поле `code` (Python над
переменной `data`), и раннер исполнит его В ИЗОЛЯЦИИ.

БЕЗОПАСНОСТЬ (defense-in-depth), т.к. это исполнение кода:
  1. По умолчанию ВЫКЛЮЧЕН. Включается только env ANALYSIS_CODE_EXEC=on —
     осознанно, в доверенном/контейнеризованном окружении.
  2. Отдельный процесс `python -I` (isolated: без site, без PYTHON*-env).
  3. Урезанные builtins: нет open/exec/eval/__import__/compile/input и т.п.
  4. Жёсткий timeout + лимиты CPU/памяти/дескрипторов (Unix, setrlimit).
  5. Никаких аргументов/файлов; данные — только через stdin, результат —
     через переменную `result` (сериализуется в JSON).

Это НЕ абсолютный джейл (для этого нужен контейнер/gVisor), поэтому флаг
по умолчанию off и в проде включается только внутри изоляции.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5           # сек CPU/wall
DEFAULT_MEM_MB = 256


def is_enabled() -> bool:
    """Раннер включён только явным env-флагом (по умолчанию OFF).

    Дефолт был "on" — вопреки этой же строке докстринга и docs/ANALYSIS_TAB.md.
    Раньше это означало исполнение произвольного Python на сервере у любого
    развёртывания, которое не знало про флаг. Урезанные builtins защитой не
    являются: из них выходят штатным `().__class__.__base__.__subclasses__()`.
    Deny-by-default — единственный честный дефолт для исполнения кода.
    """
    return os.getenv("ANALYSIS_CODE_EXEC", "off").strip().lower() in ("1", "on", "true", "yes")


# Бутстрап дочернего процесса: читает {code, data} со stdin, исполняет код в
# урезанном namespace, печатает JSON-результат. Без импортов в namespace.
_BOOTSTRAP = r'''
import json, sys
_payload = json.loads(sys.stdin.read())
_code = _payload.get("code", "")
_data = _payload.get("data")
_safe_names = (
    "abs","min","max","sum","len","round","range","enumerate","sorted",
    "float","int","str","bool","list","dict","tuple","set","zip","map",
    "filter","any","all","reversed","divmod","pow","print",
)
_b = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
_allowed = {n: _b[n] for n in _safe_names if n in _b}
_g = {"__builtins__": _allowed, "data": _data, "result": None}
try:
    exec(compile(_code, "<analysis_code>", "exec"), _g)
    sys.stdout.write(json.dumps({"ok": True, "result": _g.get("result")}, default=str, ensure_ascii=False))
except Exception as _e:
    sys.stdout.write(json.dumps({"ok": False, "error": str(_e)[:500]}, ensure_ascii=False))
'''


def _limits(mem_mb: int, cpu_s: int):
    """preexec_fn: жёсткие лимиты ресурсов дочернего процесса (Unix)."""
    def _apply():
        try:
            import resource
            mem = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
            resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))  # запрет fork
        except Exception:
            pass
    return _apply


def run_code(code: str, data: Any = None, *, timeout: int = DEFAULT_TIMEOUT,
             mem_mb: int = DEFAULT_MEM_MB) -> Dict[str, Any]:
    """Исполнить `code` над `data` в изоляции. Возвращает
    {ok, result} или {ok:False, error}. Никогда не бросает.

    Требует ANALYSIS_CODE_EXEC=on — иначе {ok:False, error:'disabled'}.
    """
    if not is_enabled():
        return {"ok": False, "error": "code execution disabled (ANALYSIS_CODE_EXEC off)"}
    if not (code or "").strip():
        return {"ok": False, "error": "empty code"}

    payload = json.dumps({"code": code, "data": data}, ensure_ascii=False)
    preexec = _limits(mem_mb, timeout) if os.name == "posix" else None
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _BOOTSTRAP],
            input=payload, capture_output=True, text=True,
            timeout=timeout, preexec_fn=preexec,
            env={"PATH": os.getenv("PATH", "")},  # чистое окружение
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout ({timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": f"runner failed: {e}"}

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()[:300]
        return {"ok": False, "error": err or f"no output (exit {proc.returncode})"}
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "error": f"bad output: {out[:200]}"}
