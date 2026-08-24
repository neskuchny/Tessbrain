# -*- coding: utf-8 -*-
"""Слой согласий (готов к включению одним флагом) + моделирование слепка.

Согласие: выдаёт только сам человек, всегда конкретное (кому/что/до когда),
отзыв мгновенный, гейт fail-closed — и пока ENABLE_PROFILE_EXCHANGE выключен,
наружу не уходит ничего даже при живом согласии.
Слепок: датасет дообучения строится из реальных решений/мнений, планёрка
умеет сажать за стол слепки живых людей, диалог с директором держит историю.
"""
import asyncio
import importlib
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def consent(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENTS_DIR", str(tmp_path / "consents"))
    monkeypatch.delenv("ENABLE_PROFILE_EXCHANGE", raising=False)
    import backend.core.consent.consent_store as cs
    return importlib.reload(cs)


# ── Согласия ────────────────────────────────────────────────────────────────

def test_grant_list_revoke_roundtrip(consent):
    rec = consent.grant("emp-1", grantee="org-B", scope=["role", "decisions"],
                        days=30, note="обмен с партнёром")
    assert rec["consent_text_version"] == consent.CONSENT_TEXT_VERSION
    rows = consent.list_for("emp-1")
    assert len(rows) == 1 and rows[0]["active"] is True
    assert consent.revoke("emp-1", rec["id"]) is True
    assert consent.list_for("emp-1")[0]["active"] is False
    # повторный отзыв — честный False
    assert consent.revoke("emp-1", rec["id"]) is False


def test_has_active_consent_semantics(consent):
    consent.grant("emp-1", grantee="org-B", scope=["role"], days=30)
    assert consent.has_active_consent("emp-1", grantee="org-B", scope="role")
    assert not consent.has_active_consent("emp-1", grantee="org-C", scope="role"), \
        "согласие для org-B не должно работать для org-C"
    assert not consent.has_active_consent("emp-1", grantee="org-B",
                                          scope="decisions"), \
        "scope=role не покрывает decisions"
    assert not consent.has_active_consent("emp-2", grantee="org-B", scope="role")
    # wildcard
    consent.grant("emp-3", grantee="*", scope=["*"], days=10)
    assert consent.has_active_consent("emp-3", grantee="кто-угодно",
                                      scope="opinions")


def test_expired_consent_is_dead(consent, monkeypatch):
    rec = consent.grant("emp-1", grantee="org-B", days=1)
    rows = consent._load("emp-1")
    rows[0]["expires_at"] = 1                     # прошлое
    consent._save("emp-1", rows)
    assert not consent.has_active_consent("emp-1", grantee="org-B")


def test_gate_is_fail_closed(consent, monkeypatch):
    """Главное: без флага наружу нельзя ДАЖЕ с живым согласием; с флагом,
    но без согласия — тоже нельзя. Включение = ровно одна env-строка."""
    consent.grant("emp-1", grantee="org-B", scope=["*"], days=30)
    with pytest.raises(PermissionError, match="выключен"):
        consent.require_person_consent("emp-1", grantee="org-B")
    monkeypatch.setenv("ENABLE_PROFILE_EXCHANGE", "1")
    consent.require_person_consent("emp-1", grantee="org-B")   # не поднимает
    with pytest.raises(PermissionError, match="согласия"):
        consent.require_person_consent("emp-2", grantee="org-B")


def test_grant_validation(consent):
    with pytest.raises(ValueError):
        consent.grant("", grantee="x")
    with pytest.raises(ValueError):
        consent.grant("emp-1", grantee="org-B", scope=["чушь"])
    rec = consent.grant("emp-1", grantee="org-B", days=10_000)
    assert rec["expires_at"] - rec["granted_at"] <= 365 * 86400, \
        "бессрочных согласий не бывает"


# ── Датасет дообучения ИИ-копии ────────────────────────────────────────────

class _Snap:
    name = "Иван Петров"
    role = "Директор по маркетингу"
    decisions = [{"summary": "Выбрал стратегию контент-маркетинга",
                  "category": "strategic"}]
    opinions = [{"summary": "Холодные звонки для нас не работают",
                 "sentiment": "negative"}]
    ideas = ["Серия вебинаров для клиентов"]
    contradictions = []
    psychological = {}

    def to_text(self, max_length=2000):
        return "Иван Петров, директор по маркетингу"


def test_training_examples_from_real_data():
    from backend.core.twin.training_export import build_examples
    ex = build_examples(_Snap(), system_card="Ты — Иван Петров.")
    joined = " ".join(str(e) for e in ex)
    assert "контент-маркетинга" in joined
    assert "Холодные звонки" in joined
    assert "вебинаров" in joined
    # каждый пример — валидный messages-формат
    for e in ex:
        roles = [m["role"] for m in e["messages"]]
        assert roles == ["system", "user", "assistant"], roles
    # обязательный пример честного отказа — копия учится не выдумывать
    assert "в моём слепке этого нет" in joined.lower()


def test_training_dataset_stats(monkeypatch):
    import backend.core.twin.training_export as te

    async def _load(uid, pid):
        return _Snap(), "профиль", "манера речи: краткий"

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)
    out = asyncio.run(te.build_training_dataset("u-1", "Иван Петров"))
    assert out["status"] == "success"
    assert out["format"] == "jsonl-messages"
    assert out["examples_count"] == 4       # 1 решение + 1 мнение + 1 идея + отказ
    assert out["sources"] == {"decisions": 1, "opinions": 1, "ideas": 1}
    assert "мало" in out["note"], "честное предупреждение о малом объёме"
    import json as _json
    for line in out["jsonl"].splitlines():
        _json.loads(line)                    # каждая строка — валидный JSON


def test_training_dataset_no_person(monkeypatch):
    import backend.core.twin.training_export as te

    async def _load(uid, pid):
        return None, "", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)
    out = asyncio.run(te.build_training_dataset("u-1", "Никто Такой"))
    assert out["status"] == "no_data"


# ── Планёрка со слепками живых людей + диалог с директором ─────────────────

def _patch_ctx(monkeypatch):
    import backend.core.boardroom.boardroom_service as bs

    async def _hdr(uid):
        return "Компания: Т-Сенд; Стадия: стартап"

    async def _ctx(uid, q, *, use_brain, days_back):
        return "данные компании"

    monkeypatch.setattr(bs, "_company_header", _hdr)
    monkeypatch.setattr(bs, "_brain_context", _ctx)
    monkeypatch.setattr(bs, "_domain_context", lambda uid, d: "")

    async def _cal(uid, did):
        return ""

    monkeypatch.setattr(bs, "_role_calibration", _cal)
    return bs


def test_boardroom_with_live_twins(monkeypatch):
    bs = _patch_ctx(monkeypatch)

    async def _load(uid, pid):
        return _Snap(), "профиль слепка Ивана", "голос"

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)

    seen_systems = {}

    async def _speak(uid, sysp, userp, *, heavy=False):
        if "ПРОТОКОЛ ОБСУЖДЕНИЯ" in userp:
            return "решение"
        if "РЕШЕНИЕ ПЛАНЁРКИ" in userp:
            return "сверка ок"
        seen_systems[sysp[:60]] = sysp
        return "реплика"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.run_boardroom(
        "u-1", "куда двигаться?",
        director_ids=["ceo"], person_ids=["Иван Петров"],
        rounds=1, save=False))
    assert res["status"] == "success"
    kinds = {c["id"]: c["kind"] for c in res["cast"]}
    assert kinds.get("ceo") == "role"
    assert kinds.get("twin:Иван Петров") == "twin"
    assert any("профиль слепка Ивана" in s for s in seen_systems.values()), \
        "слепок должен говорить из СВОЕГО профиля, а не из ролевого шаблона"
    names = [c["name"] for c in res["cast"]]
    assert "Иван Петров (слепок)" in names


def test_boardroom_missing_twin_skipped(monkeypatch):
    bs = _patch_ctx(monkeypatch)

    async def _load(uid, pid):
        return None, "", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)
    res = asyncio.run(bs.run_boardroom(
        "u-1", "вопрос", director_ids=["ceo"],
        person_ids=["Нет Такого"], save=False))
    assert res["status"] == "error", "1 роль + 0 слепков = слишком мало участников"


def test_ask_director_dialogue(monkeypatch):
    bs = _patch_ctx(monkeypatch)
    seen = {}

    async def _speak(uid, sysp, userp, *, heavy=False):
        seen["sys"] = sysp
        seen["user"] = userp
        return "ответ CMO"

    monkeypatch.setattr(bs, "_speak", _speak)
    res = asyncio.run(bs.ask_director(
        "u-1", "cmo", "что с лидами?",
        history=[{"who": "you", "text": "привет"},
                 {"who": "director", "text": "здравствуй"}]))
    assert res["status"] == "success" and res["answer"] == "ответ CMO"
    assert "Директор по маркетингу" in seen["sys"] or "маркетинг" in seen["sys"]
    assert "данные компании" in seen["user"]
    assert "ПРЕДЫДУЩИЙ РАЗГОВОР" in seen["user"] and "привет" in seen["user"]
    assert "что с лидами?" in seen["user"]
    # неизвестный директор
    r2 = asyncio.run(bs.ask_director("u-1", "nope", "q"))
    assert r2["status"] == "error"


# ── Слепок — человек, а не база данных ──────────────────────────────────

def test_twin_prompt_requires_reasoning_in_role():
    """Регрессия на жалобу из прода: слепок инвестора на «что нам делать
    для инвестиций?» отвечал «в моём слепке этого нет» вместо взгляда.
    Промпт обязан разделять ФАКТЫ (отказ без данных) и ВЗГЛЯД (отказ
    запрещён — рассуждение в роли)."""
    from backend.core.twin.profile import twin_system_prompt
    p = twin_system_prompt("NoCap", "профиль: инвестор", "")
    assert "НЕ БАЗА ДАННЫХ" in p
    assert "отказом НЕЛЬЗЯ" in p, "на просьбу взгляда отказ запрещён"
    assert "что нам делать" in p, "вопросы-взгляды названы явно"
    assert "в моём слепке этого нет" in p, "для фактов отказ остаётся"
    assert "на встречах этого не звучало" in p, \
        "рассуждение помечается, где кончаются данные"


def test_twin_prompt_includes_company_context():
    from backend.core.twin.profile import twin_system_prompt
    p = twin_system_prompt("NoCap", "профиль", "",
                           company_context="Компания X, продукт Y, стадия MVP")
    assert "КОНТЕКСТ КОМПАНИИ" in p and "стадия MVP" in p
    p2 = twin_system_prompt("NoCap", "профиль", "", company_context="")
    assert "КОНТЕКСТ КОМПАНИИ" not in p2, "пустой контекст — честно без блока"


def test_twin_ask_passes_company_context(monkeypatch):
    """twin_ask обязан подложить снапшот компании: без него слепку не из
    чего рассуждать о вопросах шире собственных встреч."""
    import backend.api.routes.person_twin as pt

    class _Snap:
        name = "NoCap"
        version = 1
        meetings_participated = 3

    async def _load(uid, pid):
        return _Snap(), "профиль инвестора", ""

    async def _company(uid, max_length=6000):
        return "Компания Tessbrain: продукт «Мозг компании», стадия growth"

    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "Как инвестор, я бы начал с метрик удержания."

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)
    monkeypatch.setattr(
        "backend.core.twin.profile.company_context_text", _company)
    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)

    class _Req:
        headers = {}

    out = asyncio.run(pt.twin_ask.fn(
        pt.TwinAskRequest(person_id="NoCap",
                          question="Что нам делать для инвестиций?",
                          user_id="u1"),
        _Req()))
    assert out["status"] == "success"
    assert "стадия growth" in seen["prompt"], \
        "контекст компании дошёл до промпта слепка"
    assert "отказом НЕЛЬЗЯ" in seen["prompt"]


def test_external_twin_is_not_an_employee():
    """Регрессия на жалобу из прода: слепок внешнего Йена говорил «наш
    продукт MeetFlow», «мы в Synlabs» и отказывался покупать то, что якобы
    «сам строит». Внешняя категория обязана менять кадрирование."""
    from backend.core.twin.profile import twin_system_prompt
    p = twin_system_prompt("Йен", "профиль: обучение риелторов", "",
                           company_context="Компания Synlabs: MeetFlow",
                           category="external")
    assert "НЕ работаешь в этой компании" in p
    assert "не «наши»" in p
    assert "это НЕ твоя компания" in p, \
        "контекст компании помечен как чужой (вид снаружи)"
    assert "не коллега" in p
    # внутренний сотрудник — прежнее кадрирование
    p2 = twin_system_prompt("Иван", "профиль", "",
                            company_context="Компания X",
                            category="employee")
    assert "коллега" in p2 and "НЕ работаешь" not in p2
    # категория неизвестна — ведём себя как раньше (сотрудник)
    p3 = twin_system_prompt("Некто", "профиль", "")
    assert "НЕ работаешь" not in p3


def test_twin_ask_passes_category(monkeypatch):
    """twin_ask обязан определить категорию человека и передать её в промпт."""
    import backend.api.routes.person_twin as pt

    class _Snap:
        name = "Йен"
        version = 1
        meetings_participated = 2

    async def _load(uid, pid):
        return _Snap(), "профиль", ""

    async def _company(uid, max_length=6000):
        return "Компания Synlabs"

    async def _category(uid, pid, name=""):
        return "external"

    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "Смотрю на это со стороны."

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _load)
    monkeypatch.setattr(
        "backend.core.twin.profile.company_context_text", _company)
    monkeypatch.setattr(
        "backend.core.twin.profile.person_category", _category)
    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)

    class _Req:
        headers = {}

    out = asyncio.run(pt.twin_ask.fn(
        pt.TwinAskRequest(person_id="Йен", question="Купишь MeetFlow?",
                          user_id="u1"), _Req()))
    assert out["status"] == "success"
    assert "НЕ работаешь в этой компании" in seen["prompt"], \
        "внешняя категория дошла до промпта"
