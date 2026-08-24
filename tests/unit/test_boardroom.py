# -*- coding: utf-8 -*-
"""Виртуальная планёрка директоров и ролевые отчёты.

Проверяем механику, а не «ум» модели: раунды видят реплики друг друга,
председатель получает полный протокол, решение сверяется с данными, при
пустом мозге планёрка честно отказывается, ролевой фокус режет знания по
релевантности роли, а слепок сотрудника собирается целиком.
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import backend.core.boardroom.boardroom_service as bs  # noqa: E402


def _patch_ctx(monkeypatch, *, ctx="Данные компании: продукт X, выручка растёт"):
    async def _hdr(uid):
        return "Компания: Т-Сенд; Стадия: стартап"

    async def _ctx(uid, q, *, use_brain, days_back):
        return ctx

    monkeypatch.setattr(bs, "_company_header", _hdr)
    monkeypatch.setattr(bs, "_brain_context", _ctx)
    monkeypatch.setattr(bs, "_domain_context", lambda uid, d: "")

    async def _cal(uid, did):
        return ""

    monkeypatch.setattr(bs, "_role_calibration", _cal)


def test_rounds_see_each_other(monkeypatch, tmp_path):
    """Раунд 2 обязан получить реплики раунда 1 — иначе это не обсуждение."""
    _patch_ctx(monkeypatch)
    calls = []

    async def _speak(uid, sysp, userp, *, heavy=False):
        calls.append({"sys": sysp, "user": userp, "heavy": heavy})
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "## Решение\nделаем X\n## Протокол разногласий\nCTO против"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp:
            return "противоречий нет"
        return f"позиция (вызов {len(calls)})"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom(
        "u-1", "Запускать ли продукт Y?",
        director_ids=["ceo", "cmo"], rounds=2, save=False))

    assert res["status"] == "success"
    assert len(res["rounds"]) == 2
    r2_prompts = [c["user"] for c in calls
                  if "ПРОТОКОЛ ПРЕДЫДУЩЕГО РАУНДА" in c["user"]]
    assert len(r2_prompts) == 2, "во втором раунде оба должны видеть протокол"
    assert "Генеральный директор" in r2_prompts[0]
    assert "позиция" in r2_prompts[0], "реплики раунда 1 не дошли до раунда 2"
    # председатель и аудитор — на сильной модели
    heavy = [c for c in calls if c["heavy"]]
    assert len(heavy) == 2, "синтез и сверка должны идти премиум-уровнем"
    assert res["decision"].startswith("## Решение")
    assert res["verification"] == "противоречий нет"
    assert "симуляция" in res["disclaimer"]


def test_empty_brain_refuses(monkeypatch):
    """Пустой контекст → отказ, а не планёрка-выдумка."""
    _patch_ctx(monkeypatch, ctx="")

    async def _speak(*a, **k):
        raise AssertionError("до модели дойти не должно")

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom("u-1", "вопрос",
                                       director_ids=["ceo", "cmo"], save=False))
    assert res["status"] == "no_data"


def test_bad_cast_rejected(monkeypatch):
    res = asyncio.run(bs.run_boardroom("u-1", "вопрос",
                                       director_ids=["ceo"], save=False))
    assert res["status"] == "error"
    res2 = asyncio.run(bs.run_boardroom("u-1", "", save=False))
    assert res2["status"] == "error"


def test_verification_gets_decision_and_data(monkeypatch):
    """Аудитор сверяет именно решение с именно данными компании."""
    _patch_ctx(monkeypatch, ctx="ФАКТ: у компании нет отдела продаж")
    seen = {}

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp:
            seen["verify"] = userp
            return "решение противоречит факту об отделе продаж"
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "нанять 10 продавцов"
        return "реплика"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom("u-1", "как расти?",
                                       director_ids=["ceo", "sales"],
                                       rounds=1, save=False))
    assert "нанять 10 продавцов" in seen["verify"]
    assert "нет отдела продаж" in seen["verify"]
    assert "противоречит" in res["verification"]


def test_prompts_exist_for_all_directors():
    """Каждому директору из каталога — свой .md-голос (промпты-как-данные)."""
    for did in bs.DIRECTORS:
        assert bs._load_prompt(did), f"нет промпта для {did}"
    assert bs._load_prompt("chairman") and bs._load_prompt("common_rules")


# ── Ролевой фокус в контексте отчётов ──────────────────────────────────────

def test_role_focus_filters_by_keywords():
    from backend.core.reports.report_context import _matches_focus
    kw = ["маркет", "лид"]
    assert _matches_focus("Запустили маркетинговую кампанию", kw)
    assert _matches_focus("Пришло 50 лидов", kw)
    assert not _matches_focus("Починили сервер и деплой", kw)
    assert _matches_focus("что угодно", None), "без фокуса проходит всё"
    assert _matches_focus("что угодно", []), "пустой фокус = вся компания"


def test_role_methodologies_have_prompts_and_focus():
    from backend.core.reports.methodology_service import (
        METHODOLOGIES,
        ROLE_FOCUS,
        _system_prompt,
    )
    for rid in ("ceo_weekly", "marketing_weekly", "sales_weekly", "tech_weekly"):
        assert rid in METHODOLOGIES, rid
        assert rid in ROLE_FOCUS, rid
        sp = _system_prompt(rid)
        assert len(sp) > 300, f"пустой промпт {rid}"
        assert "{{dynamic_context}}" in sp, f"{rid}: нет динамики"
    assert ROLE_FOCUS["ceo_weekly"] == [], "CEO видит всю компанию"
    assert ROLE_FOCUS["marketing_weekly"] == ["marketing"]
    # домен tech существует в реестре
    from backend.core.sleep.domain_snapshots import DEFAULT_DOMAINS
    assert "tech" in DEFAULT_DOMAINS


def test_build_company_context_passes_focus(monkeypatch):
    import backend.core.reports.report_context as rc
    seen = {}

    async def _snap(uid):
        return "профиль"

    async def _dom(uid, domains):
        seen["domains"] = domains
        return "## НАКОПЛЕННАЯ КАРТИНА: Маркетинг\nхроника"

    async def _facts(uid, days, keywords=None):
        seen["facts_kw"] = keywords
        return "факты", {"Decision": 1}

    async def _sums(uid, *, days_back, project_id, folder_id, keywords=None):
        seen["sums_kw"] = keywords
        return "саммари", 3, 3

    monkeypatch.setattr(rc, "_company_snapshot_text", _snap)
    monkeypatch.setattr(rc, "_domain_snapshots_text", _dom)
    monkeypatch.setattr(rc, "_graph_facts_text", _facts)
    monkeypatch.setattr(rc, "_meeting_summaries", _sums)

    out = asyncio.run(rc.build_company_context(
        "u-1", days_back=7, domains=["marketing"]))
    assert seen["domains"] == ["marketing"]
    assert seen["facts_kw"] and "маркет" in seen["facts_kw"], \
        "keywords должны подтянуться из реестра доменов"
    assert seen["sums_kw"] == seen["facts_kw"]
    assert out["stats"]["focus_domains"] == ["marketing"]
    assert out["stats"]["has_domain_snapshot"] is True
    assert "НАКОПЛЕННАЯ КАРТИНА" in out["text"]


# ── Слепок сотрудника ──────────────────────────────────────────────────────

def test_twin_profile_is_complete():
    from backend.api.routes.person_twin import build_twin_profile

    class _Snap:
        decisions = [{"summary": "выбрал стратегию A", "category": "strategic"}]
        ideas = ["идея про вебинары"]
        opinions = [{"summary": "скептичен к холодным звонкам",
                     "sentiment": "negative"}]
        contradictions = []
        psychological = {"стиль": "прямой, быстрый"}

        def to_text(self, max_length=2000):
            assert max_length >= 100_000, "слепок не должен обрезаться"
            return "Иван Петров — директор по маркетингу"

    out = build_twin_profile(_Snap())
    assert "Иван Петров" in out
    assert "выбрал стратегию A" in out and "strategic" in out
    assert "вебинары" in out
    assert "холодным звонкам" in out
    assert "прямой, быстрый" in out


# ── Переговорная механика (а не параллельный залп) ─────────────────────────

def test_sequential_speakers_see_each_other_within_round(monkeypatch):
    """Второй спикер раунда 1 обязан видеть первого и получить требование
    оспорить слабое место — иначе это залп мнений, а не переговоры."""
    _patch_ctx(monkeypatch)
    prompts = []

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "решение"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp or "СТЕНОГРАММА" in userp:
            return "ок"
        prompts.append(userp)
        return f"реплика №{len(prompts)}"

    monkeypatch.setattr(bs, "_speak", _speak)
    asyncio.run(bs.run_boardroom("u-1", "вопрос?",
                                 director_ids=["ceo", "cmo", "sales"],
                                 rounds=1, save=False))
    assert len(prompts) == 3
    assert "УЖЕ ПРОЗВУЧАЛО В ЭТОМ РАУНДЕ" not in prompts[0], \
        "первый спикер говорит первым"
    assert "УЖЕ ПРОЗВУЧАЛО В ЭТОМ РАУНДЕ" in prompts[1]
    assert "реплика №1" in prompts[1], "второй не видит первого"
    assert "реплика №1" in prompts[2] and "реплика №2" in prompts[2]
    # несогласие — прямо, но БЕЗ принуждения: есть право воздержаться,
    # и явный запрет выдумывать возражения (иначе галлюцинации и токены)
    assert "ВОЗДЕРЖИВАЮСЬ" in prompts[1], "нет права промолчать"
    assert "выдумывай возражений" in prompts[1]
    assert "третий вариант" in prompts[1]


def test_unresolved_disagreement_triggers_resolution_round(monkeypatch):
    """Секретарь нашёл неразрешённое разногласие → дополнительный раунд
    строго по спорным пунктам (консенсус или мотивированный отказ)."""
    _patch_ctx(monkeypatch)
    resolution_prompts = []

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "СТЕНОГРАММА" in userp:
            return ('{"resolved": false, "contested": '
                    '["CTO против найма 10 продавцов"]}')
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "решение"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp:
            return "сверка"
        if "РАУНД РАЗРЕШЕНИЯ РАЗНОГЛАСИЙ" in userp:
            resolution_prompts.append(userp)
            return "уступаю по пункту 1"
        return "реплика"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom("u-1", "как расти?",
                                       director_ids=["ceo", "cto"],
                                       rounds=1, save=False))
    assert res["contested_points"] == ["CTO против найма 10 продавцов"]
    assert len(res["rounds"]) == 2, "должен появиться раунд разрешения"
    assert res["rounds"][-1].get("resolution_round") is True
    assert resolution_prompts and "CTO против найма" in resolution_prompts[0]


def test_consensus_reached_no_extra_round(monkeypatch):
    """Разногласий нет → лишний раунд не тратится."""
    _patch_ctx(monkeypatch)

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "СТЕНОГРАММА" in userp:
            return '{"resolved": true, "contested": []}'
        if "РАУНД РАЗРЕШЕНИЯ РАЗНОГЛАСИЙ" in userp:
            raise AssertionError("лишний раунд при консенсусе")
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "решение"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp:
            return "сверка"
        return "реплика"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom("u-1", "вопрос",
                                       director_ids=["ceo", "cmo"],
                                       rounds=1, save=False))
    assert res["contested_points"] == [] and len(res["rounds"]) == 1


def test_ask_directors_panel_discussion(monkeypatch):
    """«Спросить нескольких»: по очереди, с реакцией на коллег, итог честный."""
    _patch_ctx(monkeypatch)
    prompts = []

    async def _speak(uid, sysp, userp, *, heavy=False):
        prompts.append(userp)
        if "Итог в 3-6 предложениях" in userp:
            return "пришли к X, CTO остался против"
        return f"голос №{len(prompts)}"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.ask_directors("u-1", ["cmo", "cto"], "поднимать цены?"))
    assert res["status"] == "success" and res["mode"] == "panel"
    assert len(res["voices"]) == 2
    assert "КОЛЛЕГИ УЖЕ ВЫСКАЗАЛИСЬ" in prompts[1], "второй не видит первого"
    assert "не согласен — скажи прямо" in prompts[1]
    assert "ВОЗДЕРЖИВАЮСЬ" in prompts[1], "в чате тоже есть право промолчать"
    assert "CTO остался против" in res["summary"]
    # один директор → обычный ответ, не panel
    res1 = asyncio.run(bs.ask_directors("u-1", ["cmo"], "вопрос"))
    assert res1.get("mode") != "panel" and res1["status"] == "success"


# ── Калибровка роли под компанию ───────────────────────────────────────────

def test_role_calibration_generated_and_cached(monkeypatch, tmp_path):
    """Калибровка строится из профиля компании, кэшируется и
    инвалидируется при изменении профиля."""
    import backend.core.boardroom.role_calibration as rcal
    monkeypatch.setenv("BOARDROOM_CALIBRATION_DIR", str(tmp_path))

    company = {"text": "Компания «РемонтПро», ремонт квартир, 12 человек"}

    async def _snap(uid):
        return company["text"]

    monkeypatch.setattr("backend.core.reports.report_context._company_snapshot_text", _snap)

    calls = []

    async def _gen(uid, workload, prompt):
        calls.append(prompt)
        return "## О чём ты реально беспокоишься\n• локальная репутация"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload", _gen)

    out1 = asyncio.run(rcal.get_role_calibration("u-1", "cmo", "Директор по маркетингу"))
    assert "локальная репутация" in out1
    assert len(calls) == 1
    # генерационный промпт требует и «что вредно», и отраслевые тонкости
    assert "НЕ работает или вредно" in calls[0]
    assert "отраслевое допущение" in calls[0]
    assert "РемонтПро" in calls[0], "калибровка должна видеть профиль компании"

    # повторный вызов — из кэша, без нового LLM-вызова
    out2 = asyncio.run(rcal.get_role_calibration("u-1", "cmo", "Директор по маркетингу"))
    assert out2 == out1 and len(calls) == 1

    # профиль компании изменился → пересборка
    company["text"] = "Компания «РемонтПро» выросла до 40 человек, вышла в B2B"
    asyncio.run(rcal.get_role_calibration("u-1", "cmo", "Директор по маркетингу"))
    assert len(calls) == 2, "смена профиля компании должна инвалидировать кэш"


def test_role_calibration_empty_company_is_empty(monkeypatch, tmp_path):
    """Нет профиля компании → нет калибровки (а не выдуманная специфика)."""
    import backend.core.boardroom.role_calibration as rcal
    monkeypatch.setenv("BOARDROOM_CALIBRATION_DIR", str(tmp_path))

    async def _snap(uid):
        return ""

    monkeypatch.setattr("backend.core.reports.report_context._company_snapshot_text", _snap)

    async def _gen(*a, **k):
        raise AssertionError("без профиля LLM звать нельзя")

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload", _gen)
    out = asyncio.run(rcal.get_role_calibration("u-1", "cmo", "CMO"))
    assert out == ""


def test_calibration_injected_into_role_system(monkeypatch):
    """Калибровка попадает в системный промпт роли на планёрке."""
    _patch_ctx(monkeypatch)

    async def _cal(uid, did):
        return "ты беспокоишься о локальной репутации и повторных клиентах"

    monkeypatch.setattr(bs, "_role_calibration", _cal)
    parts = asyncio.run(bs._build_participants("u-1", ["cmo"], [], "шапка"))
    assert "КАЛИБРОВКА ТВОЕЙ РОЛИ" in parts["cmo"]["system"]
    assert "локальной репутации" in parts["cmo"]["system"]


def test_abstention_is_respected_and_cheap(monkeypatch):
    """Директор, которому нечего сказать, воздерживается: его многословный
    «отказ» нормализуется в одну строку протокола, а не раздувает контекст."""
    _patch_ctx(monkeypatch)

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "решение"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp or "СТЕНОГРАММА" in userp:
            return "ок"
        if "технический директор" in sysp.lower():
            return ("ВОЗДЕРЖИВАЮСЬ. Хотя, если подумать, можно было бы ещё "
                    "долго рассуждать о том, что вопрос ценообразования "
                    "теоретически касается нагрузки на биллинг...")
        return "поднимаем цены на 20%"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom("u-1", "поднимать ли цены?",
                                       director_ids=["cmo", "cto"],
                                       rounds=1, save=False))
    cto_reply = res["rounds"][0]["replies"]["cto"]
    assert cto_reply == "Воздерживаюсь: по этому вопросу мне добавить нечего."
    assert "биллинг" not in cto_reply, "хвост после воздержания должен отсекаться"
    # содержательная реплика осталась как есть
    assert res["rounds"][0]["replies"]["cmo"] == "поднимаем цены на 20%"
