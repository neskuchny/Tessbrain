# -*- coding: utf-8 -*-
"""Extractor-адаптеры для eval-harness (P10).

P6 дал чистый harness с инъектируемым `extractor(case_input)->dict`,
но не было способа подключить к нему **реальный LLM** и прогнать как
CI-гейт перед включением `ONTOLOGY_EXTRACTION_MODE=strict`.

Здесь:
  • build_extraction_prompt — детерминированный промпт из кейса
  • parse_extraction — устойчивый разбор ответа LLM в
    {objects, actions} (никогда не raises → {} при мусоре)
  • llm_extract_all — async-прогон по кейсам через инъектируемый
    `llm_json_call` (фейк в тесте, реальный клиент в CI/проде),
    сбой кейса изолирован
  • precomputed_extractor — sync-обёртка предрассчитанных
    результатов для синхронного harness.run_eval

Дизайн (как P0–P9): чистый, инъектируемый, never-raises.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# llm_json_call(prompt) -> str|dict|list (ответ модели; str парсится)
LLMJsonCall = Callable[[str], Awaitable[Any]]

_PROMPT_TMPL = (
    "Извлеки из текста сущности и действия СТРОГО по онтологии графа "
    "знаний и верни ТОЛЬКО JSON вида "
    '{{"objects":[{{"label":"<Тип>","props":{{...}}}}],'
    '"actions":[{{"kind":"create_node","target":"<Тип>"}}]}}.\n'
    "Текст:\n{body}"
)


def build_extraction_prompt(case_input: Any) -> str:
    """Детерминированный промпт из входа кейса. Не raises."""
    if isinstance(case_input, str):
        body = case_input
    else:
        try:
            body = json.dumps(case_input, ensure_ascii=False)
        except Exception:
            body = str(case_input)
    return _PROMPT_TMPL.format(body=body)


def parse_extraction(raw: Any) -> dict:
    """Ответ LLM → {"objects":[...],"actions":[...]}. Никогда не raises.

    Принимает dict/list (уже распарсенный generate_json) или str
    (в т.ч. с markdown-ограждением ```json). При любой проблеме —
    пустой результат, не исключение (это понизит recall, что
    корректно отразит гейт).
    """
    obj: Any = raw
    if isinstance(raw, str):
        txt = raw.strip()
        if "```" in txt:
            # вырезаем содержимое первого fenced-блока
            parts = txt.split("```")
            if len(parts) >= 2:
                cand = parts[1]
                if cand.lstrip().lower().startswith("json"):
                    cand = cand.lstrip()[4:]
                txt = cand.strip()
        try:
            obj = json.loads(txt)
        except Exception:
            return {"objects": [], "actions": []}

    if not isinstance(obj, dict):
        return {"objects": [], "actions": []}

    objects = obj.get("objects")
    actions = obj.get("actions")
    return {
        "objects": objects if isinstance(objects, list) else [],
        "actions": actions if isinstance(actions, list) else [],
    }


async def llm_extract_all(
    cases: list,
    llm_json_call: LLMJsonCall,
    *,
    prompt_builder: Callable[[Any], str] | None = None,
) -> dict:
    """Прогнать кейсы через LLM. Ключ — `_input_key(case.input)`
    (его же ждёт `make_input_extractor`).

    Никогда не raises: падение/таймаут модели на кейсе → пустой
    результат для этого кейса (гейт это поймает через recall).
    """
    build = prompt_builder or build_extraction_prompt
    out: dict = {}
    for case in cases or []:
        cid = getattr(case, "id", "?")
        ikey = _input_key(getattr(case, "input", None))
        try:
            prompt = build(getattr(case, "input", None))
            raw = await llm_json_call(prompt)
            out[ikey] = parse_extraction(raw)
        except Exception as exc:
            logger.warning("eval.llm: case '%s' failed: %s", cid, exc)
            out[ikey] = {"objects": [], "actions": []}
    return out


def _input_key(case_input: Any) -> str:
    """Стабильный ключ кейса по его input (harness даёт input, не id)."""
    try:
        return json.dumps(case_input, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(case_input)


def make_input_extractor(results_by_input_key: dict) -> Callable[[Any], dict]:
    """Sync-extractor для harness.run_eval: ищет предрассчитанный
    результат по сериализованному `case.input`. Не raises."""
    return lambda case_input: results_by_input_key.get(
        _input_key(case_input), {"objects": [], "actions": []}
    )


__all__ = [
    "LLMJsonCall",
    "build_extraction_prompt",
    "parse_extraction",
    "llm_extract_all",
    "make_input_extractor",
    "_input_key",
]
