# -*- coding: utf-8 -*-
"""Язык ответа моделей = язык интерфейса пользователя.

Жалоба из прода: интерфейс переведён на английский, а «Планёрка» и слепки
отвечали по-русски — промпты у нас написаны по-русски, и без явной
инструкции модель отвечает на языке промпта. Инструкция приклеивается
ХВОСТОМ к системному промпту; для русского тенанта промпт обязан остаться
прежним байт-в-байт (иначе ломается кэш и меняется поведение на ровном
месте).
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.llm.lang import lang_instruction  # noqa: E402


def test_ru_and_empty_change_nothing():
    assert lang_instruction("") == ""
    assert lang_instruction(None) == ""
    assert lang_instruction("ru") == ""
    assert lang_instruction("RU") == "", "регистр кода не важен"


def test_known_language_named_in_instruction():
    got = lang_instruction("en")
    assert "English" in got and "(en)" in got
    assert "Deutsch" in lang_instruction("de")


def test_unknown_code_passed_as_is_not_invented():
    """Неизвестный код не выдумываем в название — отдаём модели как есть."""
    got = lang_instruction("nl")
    assert "nl" in got
    assert got.count("nl") >= 1


def _patch_boardroom(monkeypatch):
    import backend.core.boardroom.boardroom_service as bs

    async def _hdr(uid):
        return "Компания: Т-Сенд"

    async def _ctx(uid, q, *, use_brain, days_back):
        return "данные компании"

    async def _cal(uid, did):
        return ""

    monkeypatch.setattr(bs, "_company_header", _hdr)
    monkeypatch.setattr(bs, "_brain_context", _ctx)
    monkeypatch.setattr(bs, "_domain_context", lambda uid, d: "")
    monkeypatch.setattr(bs, "_role_calibration", _cal)
    return bs


def test_director_answers_in_interface_language(monkeypatch):
    bs = _patch_boardroom(monkeypatch)
    seen = {}

    async def _speak(uid, sysp, userp, *, heavy=False):
        seen["sys"] = sysp
        return "answer"

    monkeypatch.setattr(bs, "_speak", _speak)
    asyncio.run(bs.ask_director("u-1", "cmo", "what about leads?", lang="en"))
    assert "English" in seen["sys"], \
        "директору сказано отвечать на языке интерфейса"

    asyncio.run(bs.ask_director("u-1", "cmo", "что с лидами?"))
    assert "ЯЗЫК ОТВЕТА" not in seen["sys"], \
        "русскому тенанту промпт не меняем"


def test_boardroom_participants_get_language(monkeypatch):
    bs = _patch_boardroom(monkeypatch)
    prompts = []

    async def _speak(uid, sysp, userp, *, heavy=False):
        prompts.append(sysp)
        return "position"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom(
        "u-1", "should we raise prices?", director_ids=["ceo", "cfo"],
        rounds=1, use_brain=False, lang="en", save=False))
    assert res["status"] == "success"
    assert prompts, "участники высказались"
    # каждая роль, председатель и аудитор — все на языке пользователя
    assert all("English" in p for p in prompts
               if "секретарь планёрки" not in p), \
        "инструкция языка доехала до всех системных промптов, кроме "\
        "строгого JSON-секретаря"


def test_abstention_recognized_in_english(monkeypatch):
    """Право промолчать работает на любом языке ответа: «I abstain» должно
    сворачиваться в ту же короткую строку, что и «ВОЗДЕРЖИВАЮСЬ»."""
    bs = _patch_boardroom(monkeypatch)

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "секретарь планёрки" in sysp:
            return '{"resolved": true, "contested": []}'
        if "председатель" in sysp or "аудитор" in sysp:
            return "decision"
        return "I abstain — nothing to add here."

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom(
        "u-1", "q", director_ids=["ceo", "cfo"], rounds=1,
        use_brain=False, lang="en", save=False))
    assert res["status"] == "success"
    replies = list(res["rounds"][0]["replies"].values())
    assert replies and all(r.startswith("Abstaining") for r in replies), \
        f"английское воздержание не распознано: {replies}"


# ── язык получателя вне HTTP-запроса ────────────────────────────────────

def test_resolve_falls_back_to_persona_when_no_request(monkeypatch):
    """Ночная джоба запроса не имеет: язык берётся из Persona человека."""
    from backend.core.llm import lang as L

    class _CC:
        preferred_language = "en"

    class _P:
        communication_cognitive = _CC()

    class _Store:
        async def get(self, uid):
            return _P()

    monkeypatch.setattr("backend.core.persona.store.get_persona_store",
                        lambda: _Store())
    monkeypatch.setattr(L, "_request_lang", lambda: "")
    assert asyncio.run(L.resolve_answer_lang("u-1")) == "en"


def test_request_locale_wins_over_persona(monkeypatch):
    """Человек переключил язык в интерфейсе — это сильнее старой настройки."""
    from backend.core.llm import lang as L

    class _CC:
        preferred_language = "en"

    class _P:
        communication_cognitive = _CC()

    class _Store:
        async def get(self, uid):
            return _P()

    monkeypatch.setattr("backend.core.persona.store.get_persona_store",
                        lambda: _Store())
    monkeypatch.setattr(L, "_request_lang", lambda: "de")
    assert asyncio.run(L.resolve_answer_lang("u-1")) == "de"
    # …но для текста, который читает ДРУГОЙ человек, решает его Persona
    assert asyncio.run(
        L.resolve_answer_lang("u-1", prefer_persona=True)) == "en"


def test_json_mode_protects_keys():
    got = lang_instruction("en", json_values=True)
    assert "КЛЮЧИ JSON оставь" in got, "ключи схемы не переводим"
    assert "имена собственные" in got, "имена из карточек не переводим"
    assert lang_instruction("ru", json_values=True) == ""


def test_pulse_report_speaks_recipient_language():
    """Пульс собирается шаблонами в коде — переводится каталогом, а не
    моделью. Русский вывод при этом обязан остаться прежним."""
    from backend.core.pulse.manager_report import compose_markdown
    analysis = {
        "signals": {"overdue": [{"title": "Счёт клиенту", "_overdue_days": 3}],
                    "blocked": [], "no_deadline": [], "no_owner": [],
                    "stale": [], "deadline_soon": [], "deferred": [],
                    "not_in_tracker": []},
        "overloaded": [], "counts": {"overdue": 1, "no_deadline": 0,
                                     "no_owner": 0, "stale": 0},
        "open_total": 1, "done_total": 0,
    }
    ru = compose_markdown(analysis)
    assert "Пульс исполнения" in ru and "Требует вмешательства" in ru
    assert "просрочена на 3 дн." in ru

    en = compose_markdown(analysis, locale="en")
    assert "Execution pulse" in en and "Needs your attention" in en
    assert "3 days overdue" in en
    assert "Счёт клиенту" in en, "название задачи — данные, не переводим"
