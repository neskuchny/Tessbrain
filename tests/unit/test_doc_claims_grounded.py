# -*- coding: utf-8 -*-
"""Новые утверждения бизнес-карты привязаны к коду, а не к намерению.

Владелец добавил в документ шесть тем: передача задачи ИИ-исполнителю,
визуальная отчётность как формат, синхронизация и разные модели, MCP,
автосоздание регламентов, экосистема источников. Каждое утверждение здесь
сверяется с тем, что в коде действительно есть, — иначе документ снова
разойдётся с системой, как разошёлся до этого.

Проверяем не текст ради текста, а факты, на которых текст стоит:
  1. исполнители: подписочные инструменты И свой сервер, запуск за флагом;
  2. подписочные исполнители запрещены в закрытом контуре, свой — нет;
  3. число инструментов MCP в документе = числу в реестре;
  4. регламенты извлекаются на каждой встрече, а не по кнопке, и типов
     столько, сколько названо;
  5. приёмник стендапов существует и знает названные события;
  6. звонки не копируют транскрипты к нам, а их выводы модели помечены;
  7. уровни моделей распределены по нагрузкам, как описано;
  8. сценарий кофе: роли участников — реальные конвейеры в коде.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC = os.path.join(ROOT, "docs/ru/PRODUCT_CAPABILITIES.md")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def _doc() -> str:
    with open(DOC, encoding="utf-8") as f:
        return f.read()


def _load(name: str, relpath: str, pkgs=()):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in pkgs:
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── 1-2. Исполнители задач ──────────────────────────────────────────────

def test_both_executor_paths_exist():
    """Подписка (CLI) и свой сервер (OpenHands) — оба пути в реестре."""
    reg = _src("backend/core/executors/registry.py")
    known = reg[reg.index("_KNOWN_BACKENDS = {"):]
    known = known[:known.index("}")]
    for name in ("claude_code_cli", "cursor_cli", "codex_cli", "openhands"):
        assert f'"{name}"' in known, f"{name} обязан быть в реестре исполнителей"
    files = os.listdir(os.path.join(ROOT, "backend/core/executors/backends"))
    for f in ("claude_code.py", "cursor.py", "codex.py", "openhands.py"):
        assert f in files, f"нет реализации {f}"
    print("✅ оба пути исполнения существуют в коде")


def test_subscription_executors_blocked_in_air_gap_own_server_not():
    reg = _src("backend/core/executors/registry.py")
    ext = reg[reg.index("_EXTERNAL_BACKENDS = {"):]
    ext = ext[:ext.index("}")]
    for name in ("claude_code_cli", "cursor_cli", "codex_cli"):
        assert f'"{name}"' in ext, f"{name} ходит наружу — обязан быть внешним"
    assert '"openhands"' not in ext, (
        "свой сервер — единственный путь, проходящий в закрытом контуре"
    )
    print("✅ подписочные исполнители закрыты в контуре, свой сервер — нет")


def test_executor_launch_requires_human_and_flag():
    mcp = _src("mcp_server.py")
    block = mcp[mcp.index('"coding_handoff": ('):mcp.index('"sima_kanon_status"')]
    assert "НИЧЕГО не запускает" in block, "подготовка не должна запускать"
    assert "ЯВНО подтвердил" in block and "enable_coding_handoff_exec" in block, (
        "запуск обязан требовать подтверждения человека и флага"
    )
    print("✅ запуск исполнителя — только по подтверждению человека и за флагом")


# ── 3. MCP ──────────────────────────────────────────────────────────────

def test_mcp_tool_count_matches_registry():
    mcp = _src("mcp_server.py")
    block = mcp[mcp.index("TOOLS: Dict[str, tuple] = {"):]
    # верхнеуровневые ключи реестра: ровно 4 пробела отступа
    names = re.findall(r'^    "([a-z_]+)": ', block, re.M)
    assert len(names) >= 20, f"инструментов подозрительно мало: {len(names)}"
    doc = _doc()
    claimed = re.search(r"\*\*(\d+)\*\* — инструмента для внешних AI по MCP", doc)
    assert claimed, "в документе нет числа инструментов MCP"
    assert int(claimed.group(1)) == len(names), (
        f"документ обещает {claimed.group(1)}, в реестре {len(names)}"
    )
    # названные в документе группы должны существовать реально
    for tool in ("ask_brain", "compose_artifact", "run_skill", "ingest_url",
                 "coding_handoff", "confirm_coding_handoff", "goals_progress"):
        assert tool in names, f"в документе описан несуществующий {tool}"
    print(f"✅ инструментов MCP: {len(names)} — документ совпадает с реестром")


# ── 4. Регламенты ───────────────────────────────────────────────────────

def test_regulations_extracted_on_every_meeting():
    orch = _src("backend/core/capture/orchestrator.py")
    assert "RegulationExtractorAgent" in orch
    assert '"enable_regulation_extraction": True' in orch, (
        "извлечение регламентов обязано идти на каждой встрече, не по кнопке"
    )
    print("✅ регламенты извлекаются при обработке каждой встречи")


def test_regulation_types_match_doc():
    ext = _src("backend/core/templates/regulation_extractor.py")
    head = ext[:ext.index("import json")]
    kinds = ("Регламенты", "Требования", "Политики", "Процедуры", "Кейсы",
             "Методологии", "Лучшие практики", "Руководства", "Плейбуки",
             "Рабочие процессы")
    for k in kinds:
        assert k in head, f"тип {k} назван в документе, но не в коде"
    assert "десять типов" in _doc(), "число типов в документе должно совпадать"
    print("✅ десять типов накопленного знания — как в коде")


# ── 5-6. Экосистема источников ──────────────────────────────────────────

def test_standup_receiver_exists_with_named_events():
    dp = _src("backend/core/integrations/daily_pulse.py")
    kinds = dp[dp.index("EVENT_KINDS = ("):]
    kinds = kinds[:kinds.index(")")]
    for e in ("standup.processed", "chat_summary.daily", "blocker.raised",
              "weekly_report.generated", "sprint.analyzed"):
        assert e in kinds, f"событие {e} названо в документе, но не принимается"
    assert "enable_daily_pulse_integration" in _src(
        "backend/core/config/feature_flags.py")
    print("✅ приёмник стендапов есть, события совпадают, флаг существует")


def test_calls_domain_separated_and_llm_marked():
    ci = _src("backend/core/integrations/callinsight.py")
    head = ci[:ci.index("from __future__")]
    assert "НЕ копируются" in head, (
        "документ обещает, что транскрипты звонков к нам не копируются"
    )
    assert "llm_narrative" in ci, (
        "выводы их модели обязаны быть помечены, а не становиться фактом"
    )
    print("✅ звонки: транскрипты не копируются, выводы модели помечены")


# ── 7. Разные модели под задачи ─────────────────────────────────────────

def test_workload_levels_match_doc():
    wp = _load("wp_doc", "backend/core/llm/workload_policy.py")
    lv = wp.WORKLOAD_LEVEL
    for heavy in ("extraction", "nightly", "tz_generation",
                  "search_deep_synthesis"):
        assert lv.get(heavy) == "premium", (
            f"{heavy} описан как крупная модель — в коде {lv.get(heavy)}"
        )
    assert any(v in ("standard", "fast") for k, v in lv.items()
               if "search" in k or "chat" in k), (
        "частое/интерактивное обязано идти на модели поменьше"
    )
    print("✅ уровни моделей под нагрузки совпадают с описанием")


# ── 8. Сценарий кофе ────────────────────────────────────────────────────

def test_coffee_role_pipelines_exist():
    files = set(os.listdir(os.path.join(ROOT, "backend/core/coffee/role_pipelines")))
    for role in ("founder.py", "pm.py", "dev.py", "sales.py", "hr.py",
                 "design.py"):
        assert role in files, f"роль {role} названа в документе, но не реализована"
    assert "сценарий кофе" in _doc().lower(), "сценарий назван в документе"
    print("✅ роли сценария кофе — реальные конвейеры в коде")


# ── Документ не противоречит сам себе ───────────────────────────────────

def test_doc_has_no_stale_removed_claims():
    doc = _doc()
    for stale in ("SOC 2", "ФСТЭК", "Тендерный контур",
                  "не ищет по памяти компании"):
        assert stale not in doc, f"в документе осталось снятое утверждение: {stale}"
    print("✅ снятых утверждений в документе не осталось")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе утверждения документа сверены с кодом.")
