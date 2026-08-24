# -*- coding: utf-8 -*-
"""Экспорт анализа компании для призменной карты — контракт должен сходиться.

Проверяем ТЕМ ЖЕ способом, каким будет проверять их сторона: jsonschema по
их файлу схемы + ссылочная целостность (как их validate_contract).
"""
import asyncio
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

jsonschema = pytest.importorskip("jsonschema")

from backend.api.routes import graph_export as ge  # noqa: E402

SCHEMA = json.loads((_ROOT / "docs" / "integrations"
                     / "tessent-analysis.schema.json").read_text(encoding="utf-8"))


class _GB:
    """Фейковый merged-view: узлы по меткам + nx-рёбра."""

    def __init__(self):
        import networkx as nx
        self.nx_graph = nx.DiGraph()
        # GraphBuilder пишет тип ребра в `_type` (не `type`) — прошлый тест
        # конструировал несуществующую форму и закреплял баг «всё direct»
        self.nx_graph.add_edge("p1", "prod1", _type="ASSIGNED_TO")
        self.nx_graph.add_edge("p1", "ghost", _type="direct")   # битое ребро
        self.driver = None
        self._nodes = {
            "Person": [{"id": "p1", "name": "Иван Петров",
                        "description": "директор по маркетингу",
                        "quote": "я отвечаю за весь маркетинг",
                        "meeting_id": "mtg_1"}],
            "Product": [{"id": "prod1", "name": "Т-Сенд",
                         "priority": "critical"}],
            "Task": [{"id": "t1", "title": "Сделать лендинг",
                      "status": "todo"}],
            "Decision": [{"id": "d1", "summary": "Подняли цены на 20%",
                          "status": "done"}],
            "Risk": [{"id": "r1", "summary": "Зависимость от одного клиента"}],
        }

    async def get_all_nodes_async(self, label=None, limit=5000,
                                  tenant_id=None, strict_tenant=False):
        return self._nodes.get(label, [])

    async def close(self, save=False):
        pass


@pytest.fixture()
def patched(monkeypatch, tmp_path):
    gb = _GB()

    async def _mv(uid, use_networkx=None):
        return gb

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)

    class _Snap:
        def to_dict(self):
            # РЕАЛЬНЫЕ формы из enhanced_snapshot: списки словарей.
            # Прошлый мок отдавал dict строк — формы, которой в проде нет,
            # и потому тест был зелёным при полностью пустых metrics.
            return {"name": "Т-Сенд", "industry": "SaaS",
                    "mission": "синхронизировать компании",
                    "kpis": [{"name": "Встреч обработано",
                              "current_value": 42, "trend": "→"},
                             {"name": "Клиенты",
                              "current_value": "18 платящих"}],
                    "financial_kpis": [{"name": "MRR", "value": 400000.0,
                                        "period": "2026-07"}]}

    class _Gen:
        user_id = ""

        async def get_company_snapshot(self, force_regenerate=False):
            return _Snap()

    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda gb, user_id=None: _Gen())

    class _GStore:
        def list_goals(self, **kw):
            return [{"title": "Выйти на 100 клиентов",
                     "target_date": "2026-12-31"}]

    monkeypatch.setattr("backend.core.goals.goal_tracker.goal_store_for_user",
                        lambda uid: _GStore())
    return gb


def test_contract_validates_against_their_schema(patched):
    out = asyncio.run(ge.build_company_analysis("acme"))
    jsonschema.Draft202012Validator(SCHEMA).validate(out)

    # ссылочная целостность — как в их validate_contract
    ids = {e["id"] for e in out["entities"]}
    for r in out.get("relations", []):
        assert r["source"] in ids and r["target"] in ids, \
            "битые рёбра должны отбрасываться у нас"
    assert len(ids) == len(out["entities"]), "дублей id быть не должно"

    # содержимое честное
    assert out["company"]["name"] == "Т-Сенд"
    assert out["company"]["domain"] == "SaaS"
    by_id = {e["id"]: e for e in out["entities"]}
    assert by_id["p1"]["kind"] == "entity"
    assert by_id["prod1"]["kind"] == "result" and by_id["prod1"]["critical"]
    assert by_id["t1"]["kind"] == "process"
    assert by_id["p1"]["provenance"][0]["quote"] == "я отвечаю за весь маркетинг"
    assert "provenance" not in by_id["t1"], \
        "нет цитаты — нет provenance, не выдумываем"
    # битое ребро p1→ghost выброшено, живое осталось; тип связи не потерян
    assert len(out["relations"]) == 1
    rel = out["relations"][0]
    assert rel["source"] == "p1" and rel["target"] == "prod1"
    assert rel["type"] in ("direct", "resource"), "type обязан быть из их enum"
    assert rel["resource_flow"] == "ASSIGNED_TO", \
        "исходный тип связи не должен теряться"
    # метрики РЕАЛЬНО отдаются (раньше их не было вовсе)
    m = {x["name"]: x for x in out["metrics"]}
    assert m["MRR"]["value"] == 400000.0, "financial_kpis не доехали"
    assert m["Встреч обработано"]["value"] == 42.0
    assert m["Встреч обработано"]["trend"] == "→"
    # «18 платящих» строкой → число + unit (их просьба №3)
    assert m["Клиенты"]["value"] == 18.0
    assert m["Клиенты"]["unit"] == "платящих"
    assert out["decisions"][0]["text"].startswith("Подняли цены")
    assert any(d.get("risk") for d in out["decisions"])
    assert out["goals"][0]["goal"] == "Выйти на 100 клиентов"
    assert out["mission"] == "синхронизировать компании"


def test_mcp_token_is_single_tenant(monkeypatch, tmp_path):
    """Ответ на их вопрос об авторизации: токен читает ровно один тенант."""
    monkeypatch.setenv("MCP_TOKENS_FILE", str(tmp_path / "toks.json"))
    import importlib

    import mcp_token_store
    importlib.reload(mcp_token_store)
    tok = mcp_token_store.mint("tenant-a", label="prism map")

    class _Req:
        headers = {"authorization": f"Bearer {tok}"}

    assert ge._resolve_export_user(_Req(), None) == "tenant-a"
    assert ge._resolve_export_user(_Req(), "tenant-a") == "tenant-a"
    from litestar.exceptions import HTTPException
    with pytest.raises(HTTPException):
        ge._resolve_export_user(_Req(), "tenant-B")   # чужой тенант — отказ

    class _Bad:
        headers = {"authorization": "Bearer tess_mcp_dead"}

    with pytest.raises(HTTPException):
        ge._resolve_export_user(_Bad(), None)


# ═══ Безопасность: fail-closed без проверяемого токена ═════════════════════

class _NoAuth:
    """Запрос вообще без Authorization."""
    headers = {}


def test_export_refuses_unverified_id(monkeypatch):
    """КРИТИЧНО: ?id=<чужой тенант> БЕЗ токена не должен отдавать анализ.

    trusted_user_id без Bearer возвращает (requested_id, "unverified") —
    принимать это означало бы выкачивание любой компании по одному id.
    """
    from litestar.exceptions import HTTPException
    with pytest.raises(HTTPException) as exc:
        ge._resolve_export_user(_NoAuth(), "чужой-тенант")
    assert exc.value.status_code == 401


def test_competency_sync_refuses_unverified(monkeypatch):
    """Тот же замок на POST: он выгружает профили СОТРУДНИКОВ наружу, а
    ролевой гейт внутри проверяет роль переданного user_id (чужой владелец
    сам себе founder) — значит без проверенного токена нельзя."""
    from litestar.exceptions import HTTPException
    with pytest.raises(HTTPException) as exc:
        ge._resolve_user_jwt(_NoAuth(), "чужой-тенант")
    assert exc.value.status_code == 401


def test_verified_jwt_is_accepted(monkeypatch):
    """Проверенный JWT работает — замок не ломает легальный путь."""
    monkeypatch.setattr(
        "backend.core.auth.service_token.trusted_user_id",
        lambda headers, req: ("tenant-a", "user_jwt"))

    class _Req:
        headers = {"authorization": "Bearer real.jwt.token"}

    assert ge._resolve_export_user(_Req(), "tenant-a") == "tenant-a"
    assert ge._resolve_user_jwt(_Req(), "tenant-a") == "tenant-a"


def test_parallel_edges_deduplicated(patched):
    """В проде граф — MultiDiGraph: параллельные рёбра дали бы дубли стрелок."""
    import networkx as nx
    gb = patched
    gb.nx_graph = nx.MultiDiGraph()
    gb.nx_graph.add_edge("p1", "prod1", _type="direct")
    gb.nx_graph.add_edge("p1", "prod1", _type="direct")   # дубль
    gb.nx_graph.add_edge("p1", "prod1", _type="feedback")  # другой тип — ок

    out = asyncio.run(ge.build_company_analysis("acme"))
    rels = out.get("relations", [])
    assert len(rels) == 2, f"дубли не схлопнулись: {rels}"
    assert {r["type"] for r in rels} == {"direct", "feedback"}


def test_strict_tenant_on_all_graph_reads(patched, monkeypatch):
    """Регрессия утечки: без strict_tenant видны legacy-узлы БЕЗ tenant_id —
    то есть чужие аккаунты. Ровно эту дыру чинили в снапшоте компании, и
    здесь узлы уезжают ещё и наружу, во внешнюю карту."""
    seen = []
    gb = patched
    orig = gb.get_all_nodes_async

    async def _spy(label=None, limit=5000, tenant_id=None, strict_tenant=False):
        seen.append({"label": label, "tenant_id": tenant_id,
                     "strict": strict_tenant})
        return await orig(label=label, limit=limit, tenant_id=tenant_id)

    gb.get_all_nodes_async = _spy
    asyncio.run(ge.build_company_analysis("acme"))
    assert seen, "чтений графа не было"
    for call in seen:
        assert call["tenant_id"] == "acme", call
        assert call["strict"] is True, f"strict_tenant не выставлен: {call}"


def test_company_id_prefers_org(patched, monkeypatch):
    """company.id = организация: иначе трое коллег создадут в карте три
    «разные компании» с пересекающимися сущностями."""
    monkeypatch.setattr("backend.core.ingest.membership.get_org_for_user",
                        lambda uid: "org-42")
    out = asyncio.run(ge.build_company_analysis("acme"))
    assert out["company"]["id"] == "org-42"


def test_company_id_falls_back_to_user(patched, monkeypatch):
    """Solo-пользователь без организации — id остаётся его own."""
    monkeypatch.setattr("backend.core.ingest.membership.get_org_for_user",
                        lambda uid: None)
    out = asyncio.run(ge.build_company_analysis("acme"))
    assert out["company"]["id"] == "acme"


def test_relation_type_stays_in_their_enum():
    """Их схема ограничивает relations[].type enum'ом — сырой ASSIGNED_TO
    провалил бы их же validate_contract."""
    assert ge._map_rel_type("ASSIGNED_TO") == "direct"
    assert ge._map_rel_type("MISSING") == "missing"
    assert ge._map_rel_type("feedback") == "feedback"
    assert ge._rel_type({"_type": "reinforcing"}) == "reinforcing"
    assert ge._rel_type({"type": "balancing"}) == "balancing", "fallback на type"
    assert ge._rel_type({}) == "direct"
