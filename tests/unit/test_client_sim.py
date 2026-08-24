# -*- coding: utf-8 -*-
"""Симуляция клиентов/сегментов/партнёров: честность данных прежде всего.

Моки повторяют РЕАЛЬНЫЕ формы прода: рёбра networkx хранят тип в `_type`
(не `type`), get_all_nodes_async принимает tenant_id/strict_tenant,
generate_for_workload возвращает Optional[str].
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.marketing import client_sim as cs  # noqa: E402


class _NX:
    def __init__(self, edges):
        self._edges = edges

    def edges(self, data=True):
        return self._edges


class _GB:
    def __init__(self, nodes_by_label=None, edges=None):
        self._nodes = nodes_by_label or {}
        self.nx_graph = _NX(edges or [])
        self.strict_calls = []

    async def get_all_nodes_async(self, label=None, limit=5000,
                                  tenant_id=None, strict_tenant=False):
        self.strict_calls.append((label, tenant_id, strict_tenant))
        return self._nodes.get(label, [])

    async def close(self, save=False):
        pass


class _Gen:
    """Генератор снапшотов: person-кэш read-only, генерация запрещена."""
    def __init__(self, company=None, persons=None):
        self._company = company
        self._person_snapshots = persons or {}

    async def get_company_snapshot(self, force_regenerate=False):
        return self._company

    async def get_person_snapshot(self, pid, force_regenerate=False):
        raise AssertionError("генерация person-снапшота в read-only пути "
                             "(досье) запрещена — это LLM-вызов")


def _patch_graph(monkeypatch, gb, gen=None):
    async def _mv(uid, use_networkx=None):
        return gb

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda g=None, user_id=None: gen or _Gen())


@pytest.fixture(autouse=True)
def _tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "_store_dir", lambda: tmp_path)


# ── клиенты и сегменты ──────────────────────────────────────────────────

def test_list_clients_reads_graph_with_strict_tenant(monkeypatch):
    gb = _GB({"Client": [
        {"id": "c1", "name": "ООО Ромашка", "industry": "ритейл"},
        {"id": "c2", "name": "ЗАО Астра", "industry": "ритейл"},
        {"id": "c3", "name": "ИП Иванов"},
    ]})
    _patch_graph(monkeypatch, gb)
    out = asyncio.run(cs.list_clients("u1"))
    assert [c["id"] for c in out["clients"]] == ["c1", "c2", "c3"]
    seg = {s["name"]: s["count"] for s in out["segments"]}
    assert seg["industry: ритейл"] == 2
    assert seg["без атрибутов"] == 1, "клиент без атрибутов не выдумывается"
    assert all(strict for (_l, _t, strict) in gb.strict_calls), \
        "strict_tenant обязателен — иначе утекут NULL-tenant узлы чужих"


def test_empty_graph_is_honest_empty(monkeypatch):
    _patch_graph(monkeypatch, _GB())
    out = asyncio.run(cs.list_clients("u1"))
    assert out["clients"] == [] and out["segments"] == []


def test_dossier_uses_real_edge_type_key(monkeypatch):
    # рёбра networkx: тип лежит в `_type` — как в проде (см. graph_export)
    gb = _GB(
        {"Client": [{"id": "c1", "name": "ООО Ромашка",
                     "industry": "ритейл", "problem": "текучка кассиров"}],
         "Person": [{"id": "p9", "name": "Пётр Кузнецов"}],
         "Product": [{"id": "pr1", "name": "Кассовый модуль"}]},
        edges=[("c1", "pr1", {"_type": "uses"}),
               ("p9", "c1", {"_type": "works_at"})])
    _patch_graph(monkeypatch, gb)
    d = asyncio.run(cs.client_dossier("u1", "c1"))
    assert d["status"] == "success" and d["name"] == "ООО Ромашка"
    rel = {(r["name"], r["relation"]) for r in d["related"]}
    assert ("Кассовый модуль", "uses") in rel
    assert ("Пётр Кузнецов", "works_at") in rel
    card = cs._dossier_card(d)
    assert "текучка кассиров" in card and "ритейл" in card


def test_dossier_unknown_client(monkeypatch):
    _patch_graph(monkeypatch, _GB())
    d = asyncio.run(cs.client_dossier("u1", "nope"))
    assert d["status"] == "error"


def test_dossier_contact_snapshots_from_cache_only(monkeypatch):
    class _Snap:
        name = "Пётр Кузнецов"
        role = "Директор по закупкам"
        strengths = ["жёсткий переговорщик"]

    gb = _GB({"Client": [{"id": "c1", "name": "ООО Ромашка"}],
              "Person": [{"id": "p9", "name": "Пётр Кузнецов"}]},
             edges=[("p9", "c1", {"_type": "works_at"})])
    # _Gen.get_person_snapshot кидает AssertionError — если бы досье
    # генерировало снапшоты, тест бы упал
    _patch_graph(monkeypatch, gb, _Gen(persons={"p9": _Snap()}))
    d = asyncio.run(cs.client_dossier("u1", "c1"))
    assert d["contacts"][0]["role"] == "Директор по закупкам"


# ── группы рынка (гипотезы) ─────────────────────────────────────────────

class _CompanySnap:
    name = "Tessbrain"
    industry = "SaaS"
    products = [{"name": "Мозг компании",
                 "description": "память компании из встреч",
                 "target_audience": "СЕО малого бизнеса"}]
    business_model = "подписка"
    target_market = ""
    revenue_model = ""
    related_people = []


class _JsonLLM:
    def __init__(self, payload):
        self._p = payload
        self.prompts = []

    async def generate_json(self, prompt="", temperature=0.5):
        self.prompts.append(prompt)
        return self._p


def test_market_groups_marked_hypothesis_and_saved(monkeypatch):
    _patch_graph(monkeypatch, _GB(), _Gen(company=_CompanySnap()))
    llm = _JsonLLM({"groups": [
        {"name": "Сети кофеен", "who": "операционные директора",
         "pains": ["теряются договорённости"], "buying_trigger": "рост сети",
         "objections": ["дорого"], "channel": "отраслевые конфы",
         "validate_by": "5 интервью с опердирами", "fit_1_5": 4}]})
    monkeypatch.setattr("backend.core.llm.router.get_llm_router", lambda: llm)
    out = asyncio.run(cs.build_market_groups("u1", market="HoReCa Россия"))
    assert out["status"] == "success"
    g = out["groups"][0]
    assert g["origin"] == "hypothesis", "группа рынка — явная гипотеза"
    assert g["validate_by"], "у гипотезы обязан быть способ проверки"
    assert "Мозг компании" in llm.prompts[0], \
        "опора — реальный продукт из снапшота компании"
    assert cs.list_market_groups("u1")[0]["name"] == "Сети кофеен"


def test_market_groups_refuse_without_product(monkeypatch):
    _patch_graph(monkeypatch, _GB(), _Gen(company=None))
    out = asyncio.run(cs.build_market_groups("u1", market="HoReCa"))
    assert out["status"] == "error"
    assert "продукт" in out["message"], \
        "нет данных о продукте и нет описания — честный отказ, не выдумка"


# ── панель реакций ──────────────────────────────────────────────────────

def test_simulate_offer_refuses_empty_panel(monkeypatch):
    _patch_graph(monkeypatch, _GB())
    out = asyncio.run(cs.simulate_offer(
        "u1", offer="Предлагаем внедрить модуль за месяц"))
    assert out["status"] == "error"


def test_simulate_offer_panel_with_skeptic(monkeypatch):
    gb = _GB({"Client": [{"id": "c1", "name": "ООО Ромашка",
                          "industry": "ритейл"}]})
    _patch_graph(monkeypatch, gb)
    llm = _JsonLLM({"reactions": [
        {"name": "ООО Ромашка", "first_reaction": "у нас уже есть подрядчик",
         "interest_1_5": 2, "objections": ["дорого"],
         "open_questions": ["каков их бюджет — в данных нет"],
         "what_would_close": "пилот бесплатно", "next_step": "созвон"}],
        "skeptic": {"weakest_point": "нет цены", "fix_first": "добавить цену"}})
    monkeypatch.setattr("backend.core.llm.router.get_llm_router", lambda: llm)
    out = asyncio.run(cs.simulate_offer(
        "u1", offer="Предлагаем внедрить кассовый модуль",
        client_ids=["c1"]))
    assert out["status"] == "success"
    assert out["reactions"][0]["interest_1_5"] == 2
    assert out["skeptic"]["fix_first"] == "добавить цену"
    assert out["disclaimer"] == cs.DISCLAIMER
    assert cs.list_simulations("u1"), "панель сохраняется в историю"
    assert "ООО Ромашка" in llm.prompts[0]


# ── диалоги ─────────────────────────────────────────────────────────────

def test_chat_with_client_grounded_prompt(monkeypatch):
    gb = _GB({"Client": [{"id": "c1", "name": "ООО Ромашка",
                          "problem": "текучка кассиров"}]})
    _patch_graph(monkeypatch, gb)
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["workload"], seen["prompt"] = workload, prompt
        return "Нам важно решить текучку, но бюджет ограничен."

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    out = asyncio.run(cs.chat_with_client(
        "u1", client_id="c1", message="Что вам сейчас важнее всего?"))
    assert out["status"] == "success" and "текучку" in out["reply"]
    assert seen["workload"] == "chat"
    assert "текучка кассиров" in seen["prompt"], "карточка — из реальных данных"
    assert "гипотеза" in seen["prompt"], "правило разметки гипотез в промпте"


def test_partner_chat_refuses_without_twin(monkeypatch):
    async def _lt(uid, pid):
        return None, "", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    out = asyncio.run(cs.partner_chat("u1", person_id="p1",
                                      message="Обсудим партнёрство?"))
    assert out["status"] == "error"
    assert "не накоплено" in out["message"], \
        "нет слепка — честный отказ, а не выдуманный собеседник"


def test_partner_chat_uses_twin_profile(monkeypatch):
    class _Snap:
        name = "Андрей Соколов"

    async def _lt(uid, pid):
        return _Snap(), "интересуется white-label, торгуется по цене", \
            "короткие фразы"

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "White-label мне интересен, но цену надо обсуждать."

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    out = asyncio.run(cs.partner_chat("u1", person_id="p1",
                                      message="Готовы дать white-label"))
    assert out["status"] == "success" and out["name"] == "Андрей Соколов"
    assert "white-label" in seen["prompt"], "слепок попал в промпт"
    assert "гипотеза" in seen["prompt"]


def test_partner_pack_saved_to_reports(monkeypatch):
    class _Snap:
        name = "Андрей Соколов"

    async def _lt(uid, pid):
        return _Snap(), "профиль партнёра", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    _patch_graph(monkeypatch, _GB(), _Gen(company=_CompanySnap()))

    async def _gen(uid, workload, prompt, **kw):
        assert workload == "search_deep_synthesis", "пакет — премиум-нагрузка"
        assert "[из данных]" in prompt and "[гипотеза]" in prompt
        return ("## Концепция продукта под партнёра\n- [из данных] x\n"
                "## Коммерческое предложение\n- [гипотеза] y\n"
                "## Условия\n- <заполнить: цена>\n## План переговоров\n- z")

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)

    saved = {}

    class _Store:
        def add_report(self, rep):
            saved.update(rep)

    monkeypatch.setattr(
        "backend.core.reports.methodology_service.report_store_for_user",
        lambda uid: _Store())
    out = asyncio.run(cs.partner_pack("u1", person_id="p1"))
    assert out["status"] == "success"
    assert "## План переговоров" in out["markdown"]
    assert saved.get("report_type") == "partner_pack", \
        "пакет попадает в историю отчётов рядом с планёрками"


def test_partner_pack_refuses_without_twin(monkeypatch):
    async def _lt(uid, pid):
        return None, "", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    out = asyncio.run(cs.partner_pack("u1", person_id="p1"))
    assert out["status"] == "error"


# ── партнёры: список и режимы (регрессия «— выберите человека — пусто») ──

def test_partner_candidates_use_boardroom_source(monkeypatch):
    """Регрессия: прямое strict-чтение Person-узлов давало пустой список,
    хотя планёрка тех же людей видела. Источник обязан быть общий —
    get_all_people_profiles (strict + федеративный добор)."""
    class _GenWithPeople(_Gen):
        async def get_all_people_profiles(self, tenant_id=None):
            assert tenant_id == "u1"
            return [
                {"id": "p_nocap", "name": "NoCap", "role": "Инвестор",
                 "category": "external", "engagement_score": 9},
                {"id": "p_ivan", "name": "Иван Петров", "role": "директор",
                 "category": "management", "engagement_score": 20},
            ]

    _patch_graph(monkeypatch, _GB(), _GenWithPeople())
    out = asyncio.run(cs.list_partner_candidates("u1"))
    people = out["people"]
    assert [p["name"] for p in people] == ["NoCap", "Иван Петров"], \
        "внешние — первыми"
    assert people[0]["internal"] is False
    assert people[0]["category"] == "external"
    assert people[1]["internal"] is True


def test_partner_chat_negotiation_hides_company_context(monkeypatch):
    """Режим переговоров: контрагент НЕ видит наш снапшот компании —
    реальный партнёр внутренних данных не знает."""
    class _Snap:
        name = "NoCap"

    async def _lt(uid, pid):
        return _Snap(), "интересы: sakana_sys_2_3", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    _patch_graph(monkeypatch, _GB(), _Gen(company=_CompanySnap()))
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "Условия обсуждаемы."

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    out = asyncio.run(cs.partner_chat(
        "u1", person_id="p1", message="Предлагаю white-label",
        mode="negotiation"))
    assert out["status"] == "success" and out["mode"] == "negotiation"
    assert "Мозг компании" not in seen["prompt"], \
        "в переговорах слепку не подкладываем наши внутренние данные"


def test_partner_chat_co_create_gets_company_context(monkeypatch):
    """Режим «вместе составим КП»: слепок помогает нам — ему нужен наш
    продукт перед глазами, и отказ отвечать запрещён."""
    class _Snap:
        name = "NoCap"

    async def _lt(uid, pid):
        return _Snap(), "интересы: метрики удержания", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    _patch_graph(monkeypatch, _GB(), _Gen(company=_CompanySnap()))
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "Начал бы КП с метрик удержания."

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    out = asyncio.run(cs.partner_chat(
        "u1", person_id="p1", message="Давай вместе составим КП",
        mode="co_create"))
    assert out["status"] == "success" and out["mode"] == "co_create"
    assert "Мозг компании" in seen["prompt"], "наш продукт — перед глазами"
    assert "ВМЕСТЕ" in seen["prompt"]
    assert "отвечать отказом нельзя" in seen["prompt"]


def test_partner_pack_includes_co_create_dialog(monkeypatch):
    """Диалог совместной проработки обязан доехать до пакета."""
    class _Snap:
        name = "NoCap"

    async def _lt(uid, pid):
        return _Snap(), "профиль", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    _patch_graph(monkeypatch, _GB(), _Gen(company=_CompanySnap()))
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "## Концепция продукта под партнёра\n- ок"

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    monkeypatch.setattr(
        "backend.core.reports.methodology_service.report_store_for_user",
        lambda uid: type("S", (), {"add_report": lambda self, r: None})())
    out = asyncio.run(cs.partner_pack(
        "u1", person_id="p1",
        history=[{"role": "user", "text": "давай составим КП"},
                 {"role": "sim", "text": "начни с метрик удержания"}]))
    assert out["status"] == "success"
    assert "начни с метрик удержания" in seen["prompt"], \
        "пожелания слепка из проработки попали в промпт пакета"
    assert "СОВМЕСТНАЯ ПРОРАБОТКА" in seen["prompt"]


def test_partner_chat_accepts_long_document(monkeypatch):
    """«Отдать готовое КП и чтобы партнёр оценил»: длинный вставленный
    документ не должен обрубаться до 2000 символов."""
    class _Snap:
        name = "NoCap"

    async def _lt(uid, pid):
        return _Snap(), "профиль", ""

    monkeypatch.setattr("backend.core.twin.profile.load_twin", _lt)
    seen = {}

    async def _gen(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        return "Раздел с ценами слабый."

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _gen)
    kp = "КП: " + ("пункт предложения. " * 300) + "ФИНАЛЬНАЯ_СТРОКА_КП"
    out = asyncio.run(cs.partner_chat(
        "u1", person_id="p1", message=kp, mode="negotiation"))
    assert out["status"] == "success"
    assert "ФИНАЛЬНАЯ_СТРОКА_КП" in seen["prompt"], \
        "конец длинного КП дошёл до слепка"
    assert "готовый документ" in seen["prompt"], \
        "промпт учит разбирать принесённый документ"
