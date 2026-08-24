"""Unit-тесты для org-aware идемпотентности meeting ingest.

Покрывает:
  - dedup.normalize_transcript / content_hash / build_dedup_key
  - membership.get_org_for_user (с реальным JSON-файлом)
  - meeting_promote.should_promote — решающее правило про personal sources
  - meeting_upsert.find_completed_episode / claim_episode / mark_completion
    (через FakeSupabase — без живого PostgREST)
  - promote_meeting_to_org — реальный NetworkX flow с двумя tmp графами
  - Regression: Mini Tess pipe не пострадал
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from backend.core.ingest import dedup, meeting_promote, meeting_upsert
from backend.core.ingest import membership

# Импортируем заранее — иначе в общем suite другой тест подменяет
# sys.modules['backend.core.store'] пустым ModuleType с __path__=[], после
# чего `from backend.core.store.X import Y` падает с ModuleNotFoundError.
# Используем importlib напрямую, чтобы загрузить из файла независимо от
# того, что лежит в sys.modules.
import importlib
import importlib.util
import sys as _sys
from pathlib import Path as _Path

_REAL_STORE_MODULES: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = _Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL_STORE_MODULES[name] = mod
    return mod


# Парент-пакет — фиксируем как настоящий пакет с правильным __path__,
# иначе подменённый pkg блокирует subimport.
import types as _types
if not getattr(_sys.modules.get("backend.core.store"), "__file__", None):
    pkg = _types.ModuleType("backend.core.store")
    pkg.__path__ = [str(_Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    _sys.modules["backend.core.store"] = pkg
    _REAL_STORE_MODULES["backend.core.store"] = pkg
else:
    _REAL_STORE_MODULES["backend.core.store"] = _sys.modules["backend.core.store"]

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")
# graph_builder тянет numpy/networkx — импортируем штатно, ОК если не упадёт.
from backend.core.store import graph_builder as _gb  # noqa: F401, E402
_REAL_STORE_MODULES["backend.core.store.graph_builder"] = _gb


@pytest.fixture(autouse=True)
def _restore_store_modules():
    """Восстанавливает реальные backend.core.store.* перед каждым тестом —
    защита от тестов, которые подменяют их стабами."""
    import sys
    for name, mod in _REAL_STORE_MODULES.items():
        sys.modules[name] = mod
    yield


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# dedup.py
# =============================================================================


def test_normalize_transcript_strips_whitespace_and_case():
    a = dedup.normalize_transcript("  Hello   WORLD\n\n")
    b = dedup.normalize_transcript("hello world")
    assert a == b == "hello world"


def test_normalize_transcript_removes_timestamps():
    raw = "[00:01:23] Alice: hi\n[00:01:30] Bob: hey"
    norm = dedup.normalize_transcript(raw)
    assert "00:01:23" not in norm
    assert "alice" in norm and "bob" in norm


def test_normalize_transcript_empty():
    assert dedup.normalize_transcript("") == ""
    assert dedup.normalize_transcript(None) == ""  # type: ignore[arg-type]


def test_content_hash_stable_across_whitespace_variants():
    h1 = dedup.content_hash("Hello World")
    h2 = dedup.content_hash("  HELLO   world  ")
    h3 = dedup.content_hash("hello world")
    assert h1 == h2 == h3
    assert len(h1) == 64  # sha256 hex


def test_content_hash_changes_with_meaningful_edit():
    h1 = dedup.content_hash("the deal is closed")
    h2 = dedup.content_hash("the deal is not closed")  # семантика разная
    assert h1 != h2


def test_extract_source_picks_first_available():
    assert dedup.extract_source({"provider": "zoom"}) == "zoom"
    assert dedup.extract_source({"source": "GMeet"}) == "gmeet"
    assert dedup.extract_source({"integration_type": "teams"}) == "teams"
    assert dedup.extract_source({}) is None


def test_extract_external_id_picks_first_available():
    assert dedup.extract_external_id({"zoom_meeting_id": "123"}) == "123"
    assert dedup.extract_external_id({"external_id": "abc"}) == "abc"
    assert dedup.extract_external_id({}) is None


def test_build_dedup_key_full_corp_meeting():
    meeting = {"provider": "zoom", "zoom_meeting_id": "987654321"}
    transcript = "Q3 review meeting"
    key = dedup.build_dedup_key(meeting, transcript)
    assert key["source"] == "zoom"
    assert key["external_id"] == "987654321"
    assert key["content_hash"] is not None and len(key["content_hash"]) == 64


def test_build_dedup_key_manual_upload_no_external_id():
    """Manual upload без provider — нет external_id, есть content_hash."""
    key = dedup.build_dedup_key({}, "personal voice memo")
    assert key["source"] is None
    assert key["external_id"] is None
    assert key["content_hash"] is not None


# =============================================================================
# membership.py
# =============================================================================


@pytest.fixture
def fake_mapping(tmp_path, monkeypatch):
    """Подменяет user_org_mapping_path на tmp файл."""
    p = tmp_path / "user_org_mapping.json"
    monkeypatch.setattr(
        "backend.core.ingest.membership._user_org_mapping_path",
        lambda: str(p),
    )
    membership.clear_cache()
    return p


def test_get_org_for_user_returns_none_for_missing_file(fake_mapping):
    """Solo-flow: нет mapping → нет org. Ничего не падает."""
    assert membership.get_org_for_user(str(uuid.uuid4())) is None


def test_get_org_for_user_reads_from_json(fake_mapping):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({
        user_id: {"org_id": org_id, "role": "founder", "access_level": 3},
    }))
    membership.clear_cache()
    assert membership.get_org_for_user(user_id) == org_id
    assert membership.get_user_org_role(user_id) == "founder"


def test_get_org_for_user_handles_corrupt_json(fake_mapping):
    """Битый JSON не должен валить production-flow."""
    fake_mapping.write_text("not json {{{")
    membership.clear_cache()
    assert membership.get_org_for_user(str(uuid.uuid4())) is None


# =============================================================================
# meeting_promote.should_promote
# =============================================================================


def test_should_promote_returns_none_for_mini_tess(fake_mapping):
    """REGRESSION: Mini Tess push НЕ должен попадать в org-graph автоматически."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "founder"}}))
    membership.clear_cache()

    assert meeting_promote.should_promote(user_id, "mini_tess") is None
    assert meeting_promote.should_promote(user_id, "mini_tess_input") is None
    assert meeting_promote.should_promote(user_id, "telegram_voice_personal") is None


def test_should_promote_returns_org_for_corp_source(fake_mapping):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "founder"}}))
    membership.clear_cache()

    assert meeting_promote.should_promote(user_id, "zoom") == org_id
    assert meeting_promote.should_promote(user_id, "meetflow") == org_id


def test_should_promote_returns_none_for_solo_user(fake_mapping):
    """Solo (нет mapping) → нет promote, даже для corp-source."""
    user_id = str(uuid.uuid4())
    assert meeting_promote.should_promote(user_id, "zoom") is None


# =============================================================================
# FakeSupabase для meeting_upsert
# =============================================================================


class FakeSupabase:
    """In-memory PostgREST mock — повторяет subset операций над processed_meetings.

    Поддерживает GET (с eq/in/or-фильтрами), PATCH, POST. Хранит UNIQUE по
    (org_id, source, external_id) — повторный POST с теми же тремя — 23505.
    """

    def __init__(self):
        self.rows: list[dict] = []
        self._id_seq = 0

    async def _request(self, method, path, params=None, json_data=None, headers=None):
        params = params or {}
        if "/processed_meetings" not in path:
            return []

        if method == "GET":
            return self._select(params)
        if method == "PATCH":
            return self._patch(params, json_data or {})
        if method == "POST":
            return self._insert(json_data or {})
        raise NotImplementedError(method)

    def _matches(self, row: dict, params: dict) -> bool:
        for k, raw in params.items():
            if k in ("select", "limit"):
                continue
            if k == "or":
                # `(status.neq.running,processing_lease_until.lt.X)` — disjunction.
                inner = raw.strip("()")
                conds = inner.split(",")
                if not any(self._eval_cond(row, c) for c in conds):
                    return False
                continue
            # eq.X / in.(a,b) / lt.X / neq.X
            if not self._eval_filter(row.get(k), raw):
                return False
        return True

    def _eval_filter(self, value, expr: str) -> bool:
        op, _, arg = expr.partition(".")
        if op == "eq":
            return str(value) == arg
        if op == "neq":
            return str(value) != arg
        if op == "in":
            allowed = arg.strip("()").split(",")
            return str(value) in allowed
        if op == "lt":
            return value is not None and str(value) < arg
        return True  # неизвестный оператор — не фильтруем

    def _eval_cond(self, row: dict, cond: str) -> bool:
        # cond вида "status.neq.running" → field=status, expr=neq.running
        parts = cond.split(".", 1)
        if len(parts) != 2:
            return False
        field, expr = parts
        return self._eval_filter(row.get(field), expr)

    def _select(self, params):
        out = [r for r in self.rows if self._matches(r, params)]
        limit = params.get("limit")
        if limit:
            out = out[: int(limit)]
        return out

    def _patch(self, params, patch_body):
        updated = []
        for r in self.rows:
            if self._matches(r, params):
                r.update({k: v for k, v in patch_body.items() if v is not None or k == "processing_lease_until"})
                updated.append(r)
        return updated

    def _insert(self, body):
        # UNIQUE(org_id, source, external_id) — partial, только когда все 3 not null.
        if all(body.get(k) for k in ("org_id", "source", "external_id")):
            for r in self.rows:
                if (
                    r.get("org_id") == body["org_id"]
                    and r.get("source") == body["source"]
                    and r.get("external_id") == body["external_id"]
                ):
                    raise RuntimeError("duplicate key value violates unique constraint (23505)")
        # legacy UNIQUE(user_id, meeting_id)
        for r in self.rows:
            if r.get("user_id") == body.get("user_id") and r.get("meeting_id") == body.get("meeting_id"):
                raise RuntimeError("duplicate key value violates unique constraint (23505)")
        self._id_seq += 1
        body = dict(body)
        body["id"] = f"row_{self._id_seq}"
        self.rows.append(body)
        return [body]


# =============================================================================
# meeting_upsert.py
# =============================================================================


def test_find_completed_episode_returns_none_when_empty():
    sb = FakeSupabase()
    res = _run(meeting_upsert.find_completed_episode(
        sb, org_id="o1", source="zoom", external_id="123", content_hash="h",
    ))
    assert res is None


def test_find_completed_episode_matches_corp_key():
    sb = FakeSupabase()
    sb.rows.append({
        "id": "r1", "meeting_id": "m1", "user_id": "u1",
        "org_id": "org-a", "source": "zoom", "external_id": "z123",
        "status": "completed", "content_hash": "abc",
    })
    res = _run(meeting_upsert.find_completed_episode(
        sb, org_id="org-a", source="zoom", external_id="z123", content_hash="abc",
    ))
    assert res is not None
    assert res["meeting_id"] == "m1"


def test_find_completed_episode_falls_back_to_content_hash():
    """Разные external_id (re-export), тот же контент — должны дедуплицировать."""
    sb = FakeSupabase()
    sb.rows.append({
        "id": "r1", "meeting_id": "m1", "user_id": "u1",
        "org_id": "org-a", "source": "zoom", "external_id": "z_OLD",
        "status": "completed", "content_hash": "samehash",
    })
    res = _run(meeting_upsert.find_completed_episode(
        sb, org_id="org-a", source="zoom", external_id="z_NEW", content_hash="samehash",
    ))
    assert res is not None
    assert res["meeting_id"] == "m1"


def test_claim_episode_creates_new_row():
    sb = FakeSupabase()
    res = _run(meeting_upsert.claim_episode(
        sb, user_id="u1", meeting_id="m1",
        org_id="o1", source="zoom", external_id="z1", content_hash="h",
        subscription_id="s1",
    ))
    assert res["acquired"] is True
    assert len(sb.rows) == 1
    assert sb.rows[0]["status"] == meeting_upsert.STATUS_RUNNING


def test_claim_episode_loses_race_on_unique_violation():
    """Двое пытаются claim'нуть один corp-эпизод (разный user_id, тот же external)."""
    sb = FakeSupabase()
    r1 = _run(meeting_upsert.claim_episode(
        sb, user_id="alice", meeting_id="m_alice",
        org_id="o1", source="zoom", external_id="z1", content_hash="h",
        subscription_id=None,
    ))
    r2 = _run(meeting_upsert.claim_episode(
        sb, user_id="bob", meeting_id="m_bob",
        org_id="o1", source="zoom", external_id="z1", content_hash="h",  # тот же external!
        subscription_id=None,
    ))
    assert r1["acquired"] is True
    assert r2["acquired"] is False
    assert "race" in r2["reason"]


def test_mark_completion_updates_existing_row():
    sb = FakeSupabase()
    sb.rows.append({
        "id": "r1", "user_id": "u1", "meeting_id": "m1",
        "status": meeting_upsert.STATUS_RUNNING,
    })
    _run(meeting_upsert.mark_completion(
        sb, user_id="u1", meeting_id="m1",
        org_id="o1", source="zoom", external_id="z1", content_hash="h",
        subscription_id="s1", status=meeting_upsert.STATUS_COMPLETED,
    ))
    assert sb.rows[0]["status"] == meeting_upsert.STATUS_COMPLETED
    assert sb.rows[0]["org_id"] == "o1"


def test_mark_completion_creates_row_if_missing():
    """no_transcript path — claim не вызывался, mark должен создать запись."""
    sb = FakeSupabase()
    _run(meeting_upsert.mark_completion(
        sb, user_id="u1", meeting_id="m1",
        org_id=None, source=None, external_id=None, content_hash=None,
        subscription_id="s1", status=meeting_upsert.STATUS_NO_TRANSCRIPT,
    ))
    assert len(sb.rows) == 1
    assert sb.rows[0]["status"] == meeting_upsert.STATUS_NO_TRANSCRIPT


# =============================================================================
# promote_meeting_to_org — реальный NetworkX flow
# =============================================================================


@pytest.fixture
def tmp_graphs(tmp_path, monkeypatch):
    """Изолированный _DATA_ROOT — графы пишутся в tmp.

    Импортируем модуль здесь (не через путевую строку), потому что в
    общем suite другой тест может временно подменить
    sys.modules['backend.core.store.tenant_paths'] стабом — путевой
    monkeypatch.setattr тогда не сможет resolve'ить.
    """
    from backend.core.store import tenant_paths as tp
    monkeypatch.setattr(tp, "_DATA_ROOT", tmp_path)
    return tmp_path


def _populate_personal_graph(user_id: str, meeting_id: str):
    """Положить в personal-graph пользователя один meeting + decision + task."""
    GraphBuilder = _gb.GraphBuilder
    graph_path_for_user = _tp.graph_path_for_user

    async def _do():
        gb = GraphBuilder(use_networkx=True, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        await gb.create_node(
            node_id=f"meeting_{meeting_id}",
            label="Meeting",
            properties={"meeting_id": meeting_id, "user_id": user_id, "title": "Q3 review"},
        )
        await gb.create_node(
            node_id=f"decision_{meeting_id}_1",
            label="Decision",
            properties={
                "user_id": user_id,
                "summary": "Ship feature X",
                "source_meeting_id": meeting_id,
            },
        )
        await gb.create_node(
            node_id=f"task_{meeting_id}_1",
            label="Task",
            properties={
                "user_id": user_id,
                "title": "Write spec",
                "source_meeting_id": meeting_id,
            },
        )
        await gb.create_relationship(
            from_id=f"meeting_{meeting_id}",
            to_id=f"decision_{meeting_id}_1",
            rel_type="HAS_DECISION",
        )
        await gb.create_relationship(
            from_id=f"meeting_{meeting_id}",
            to_id=f"task_{meeting_id}_1",
            rel_type="HAS_TASK",
        )
        await gb.save()  # persist на диск — иначе promote читает чистый файл
        await gb.close()

    _run(_do())


def test_promote_copies_meeting_decision_task_to_org_graph(tmp_graphs):
    """Happy path: содержимое одного meeting'а едет в org-graph целиком."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    meeting_id = str(uuid.uuid4())
    _populate_personal_graph(user_id, meeting_id)

    result = _run(meeting_promote.promote_meeting_to_org(
        user_id=user_id, meeting_id=meeting_id, org_id=org_id, source="zoom",
    ))
    assert result["promoted"] is True
    assert result["nodes_copied"] == 3  # meeting + decision + task
    assert result["edges_copied"] == 2

    # Проверим org-graph на диске.
    org_graph_file = Path(_tp.org_graph_path(org_id))
    assert org_graph_file.exists()
    data = json.loads(org_graph_file.read_text())
    node_ids = {n["id"] for n in data.get("nodes", [])}
    assert f"meeting_{meeting_id}" in node_ids
    assert f"decision_{meeting_id}_1" in node_ids
    assert f"task_{meeting_id}_1" in node_ids


def test_promote_missing_meeting_returns_reason(tmp_graphs):
    """Если в personal-graph нет такого meeting'а — не падаем, возвращаем reason."""
    result = _run(meeting_promote.promote_meeting_to_org(
        user_id=str(uuid.uuid4()),
        meeting_id=str(uuid.uuid4()),
        org_id=str(uuid.uuid4()),
        source="zoom",
    ))
    assert result["promoted"] is False
    assert "not_found" in (result["reason"] or "")


def test_promote_is_idempotent(tmp_graphs):
    """Двойной promote того же meeting'а: количество узлов в org-graph не растёт."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    meeting_id = str(uuid.uuid4())
    _populate_personal_graph(user_id, meeting_id)

    _run(meeting_promote.promote_meeting_to_org(
        user_id=user_id, meeting_id=meeting_id, org_id=org_id, source="zoom",
    ))
    _run(meeting_promote.promote_meeting_to_org(
        user_id=user_id, meeting_id=meeting_id, org_id=org_id, source="zoom",
    ))

    data = json.loads(Path(_tp.org_graph_path(org_id)).read_text())
    # Узлы по id уникальны — повторный create_node не плодит дубликаты.
    ids = [n["id"] for n in data.get("nodes", [])]
    assert len(ids) == len(set(ids))


def test_promote_only_intra_episode_edges(tmp_graphs):
    """Кросс-эпизодная связь в personal-graph НЕ должна попасть в org-копию."""
    GraphBuilder = _gb.GraphBuilder
    graph_path_for_user = _tp.graph_path_for_user

    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    m_promoted = str(uuid.uuid4())
    m_other = str(uuid.uuid4())
    _populate_personal_graph(user_id, m_promoted)

    # Добавим в personal-graph узел из другой встречи + cross-edge.
    async def _add_cross():
        gb = GraphBuilder(use_networkx=True, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        await gb.create_node(
            node_id=f"meeting_{m_other}", label="Meeting",
            properties={"meeting_id": m_other, "user_id": user_id},
        )
        await gb.create_relationship(
            from_id=f"meeting_{m_promoted}", to_id=f"meeting_{m_other}", rel_type="RELATED_TO",
        )
        await gb.save()
        await gb.close()
    _run(_add_cross())

    _run(meeting_promote.promote_meeting_to_org(
        user_id=user_id, meeting_id=m_promoted, org_id=org_id, source="zoom",
    ))

    data = json.loads(Path(_tp.org_graph_path(org_id)).read_text())
    node_ids = {n["id"] for n in data.get("nodes", [])}
    assert f"meeting_{m_other}" not in node_ids  # узел не из этого эпизода — не скопирован
    for e in data.get("links", []):
        # никаких edges с источником/целью не из episode
        assert f"meeting_{m_other}" not in (e.get("source"), e.get("target"))
