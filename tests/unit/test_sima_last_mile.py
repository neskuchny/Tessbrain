# -*- coding: utf-8 -*-
"""SIMA, последняя миля: визуальная приёмка, выдача результата, автоподбор.

Три шага, доделанные по решению владельца продукта:

1. Визуальная приёмка подключена к циклу Kanon: HTML-результат рендерится
   на desktop и mobile, мультимодальный критик сверяет с ТЗ. По духу
   Kanon вердикт НЕ создаёт pass — только роняет в рефайн; нет
   инструмента → inconclusive, не влияет.
2. Результат прогона можно забрать zip-ом (GET /handoff/{id}/bundle) —
   раньше код оставался папкой на сервере, и человек без доступа к
   серверу не мог забрать его никак.
3. Встречи под тему проекта предлагаются автоматически, с совпавшими
   словами: видно, ПОЧЕМУ предложено.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _load_suggest():
    name = "backend.core.sima.meeting_suggest"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.sima"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "sima", "meeting_suggest.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ms = _load_suggest()
score_meetings = _ms.score_meetings

_PROJECT = {
    "name": "Сводка по клиенту перед звонком",
    "description": "Менеджер получает досье клиента одной ссылкой",
    "blocks": [
        {"title": "Досье клиента", "goal": "история встреч и договорённостей"},
        {"title": "Отправка ссылки", "description": "доставка менеджеру в телеграм"},
    ],
}


# ── 1. Автоподбор встреч ────────────────────────────────────────────────

def test_relevant_meeting_ranked_with_reasons():
    meetings = [
        {"id": "m1", "title": "Обсуждение досье клиента для менеджеров",
         "summary": "менеджерам нужна история договорённостей перед звонком"},
        {"id": "m2", "title": "Планёрка по отпускам",
         "summary": "график отпусков на август"},
    ]
    out = score_meetings(_PROJECT, meetings)
    assert [x["meeting"]["id"] for x in out] == ["m1"], (
        "релевантная встреча должна быть предложена, нерелевантная — нет"
    )
    assert out[0]["matched_words"], "совпавшие слова обязаны возвращаться"
    assert any("клиент" in w for w in out[0]["matched_words"])
    print("✅ релевантная встреча предложена, с объяснением почему")


def test_single_word_overlap_is_not_a_match():
    """Одно случайное общее слово — не повод предлагать."""
    meetings = [{"id": "m3", "title": "Звонок поставщику труб",
                 "summary": "обсудили сроки поставки"}]
    out = score_meetings(_PROJECT, meetings)
    assert out == [], "одного пересечения недостаточно для предложения"
    print("✅ одно случайное слово не считается совпадением")


def test_no_overlap_gives_empty_list():
    meetings = [{"id": "m4", "title": "Weekly sync", "summary": "misc"}]
    assert score_meetings(_PROJECT, meetings) == []
    assert score_meetings({"name": "", "blocks": []}, meetings) == []
    print("✅ нет пересечений → честно пустой список")


def test_long_summary_does_not_win_by_volume():
    """Нормировка: длинная сводка не выигрывает объёмом."""
    focused = {"id": "f", "title": "Досье клиента перед звонком",
               "summary": "менеджер история"}
    bloated = {"id": "b", "title": "Общая встреча",
               "summary": ("клиента менеджер история звонком досье " +
                           "слова " * 300)}
    out = score_meetings(_PROJECT, [focused, bloated])
    assert out and out[0]["meeting"]["id"] == "f", (
        "сфокусированная встреча обязана ранжироваться выше раздутой"
    )
    print("✅ раздутая сводка не выигрывает за счёт объёма")


def test_suggest_route_checks_access():
    src = _src("backend/api/routes/sima.py")
    start = src.index("async def suggest_tessent_meetings(")
    body = src[start:start + 1500]
    assert "_require_project_access" in body
    router = src[src.index("sima_router = Router("):]
    assert "suggest_tessent_meetings" in router
    print("✅ роут автоподбора зарегистрирован и проверяет доступ")


# ── 2. Выдача результата ────────────────────────────────────────────────

def test_bundle_route_exists_and_guarded():
    src = _src("backend/api/routes/task_analysis.py")
    start = src.index("async def handoff_bundle(")
    body = src[start:start + 4000]
    assert 'rec.get("status") != DONE' in body, (
        "наполовину написанный код нельзя отдавать как готовый"
    )
    assert "HandoffStore(uid)" in body, "изоляция per-user"
    assert "_SKIP_DIRS" in body and '".git"' in body, (
        "служебные каталоги не пакуются"
    )
    assert 'fp.startswith(root + os.sep)' in body, (
        "симлинк наружу не должен выносить чужие файлы"
    )
    assert "_MAX_BYTES" in body, "нужен кап на размер архива"
    router = src[src.index("route_handlers=["):]
    assert "handoff_bundle" in router
    print("✅ zip-выдача: только своё, только готовое, с капами")


# ── 3. Визуальная приёмка в цикле ───────────────────────────────────────

def test_visual_accept_wired_into_loop():
    src = _src("backend/core/sima/kanon_loop.py")
    assert "def visual_accept_enabled" in src
    assert '"KANON_VISUAL_ACCEPT", "on"' in src, (
        "по решению владельца включено по умолчанию"
    )
    start = src.index("async def verify_after_run(")
    body = src[start:]
    assert "_visual_accept(" in body, "визуальная приёмка не в цикле"
    print("✅ визуальная приёмка включена и встроена в цикл")


def test_visual_verdict_cannot_create_pass():
    """Дух Kanon: глаза могут уронить, но не могут подтвердить."""
    src = _src("backend/core/sima/kanon_loop.py")
    start = src.index("async def _visual_accept(")
    body = src[start:src.index("async def verify_after_run(")]
    assert '"fail" if hard else "pass"' in body
    # в цикле учитывается только fail
    loop = src[src.index("async def verify_after_run("):]
    assert 'visual.get("verdict") == "fail"' in loop
    assert 'visual.get("verdict") == "pass"' not in loop, (
        "визуальный pass не должен влиять на приёмку"
    )
    print("✅ визуальный вердикт роняет в рефайн, но не создаёт pass")


def test_visual_tool_missing_is_inconclusive():
    src = _src("backend/core/sima/kanon_loop.py")
    start = src.index("async def _visual_accept(")
    body = src[start:src.index("async def verify_after_run(")]
    assert "tool_missing" in body and '"inconclusive"' in body, (
        "нет Playwright/модели → inconclusive, а не fail и не pass"
    )
    print("✅ без инструмента — inconclusive, мягкая деградация")


def test_visual_failures_reach_refine_spec():
    """Претензии критика обязаны доехать до рефайн-ТЗ дословно."""
    src = _src("backend/core/sima/kanon_loop.py")
    loop = src[src.index("async def verify_after_run("):]
    assert '"__visual__"' in loop
    assert '"evidence": [' in loop, (
        "визуальные замечания идут в evidence — его читает _build_refine_spec"
    )
    print("✅ визуальные замечания попадают в рефайн-ТЗ")


def test_dockerfile_installs_chromium():
    src = _src("Dockerfile")
    assert "playwright install" in src, (
        "без chromium в образе визуальная приёмка всегда tool_missing"
    )
    assert src.count("PLAYWRIGHT_BROWSERS_PATH") >= 2, (
        "путь браузера нужен и в builder, и в runtime"
    )
    assert "libnss3" in src, "runtime-библиотеки рендера обязаны стоять"
    print("✅ chromium ставится в образ, runtime-библиотеки на месте")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты последней мили прошли.")
