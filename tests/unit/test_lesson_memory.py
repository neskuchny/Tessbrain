"""Unit-тесты P12d: lesson-память (удачи/неудачи + граф-заземление).

Чистый stdlib-модуль — грузим через importlib. root=tmp_path, часы
инъектируем. Без LLM/графа/сети.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]

for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m
_spec = importlib.util.spec_from_file_location(
    "backend.core.hermes.lesson_memory",
    _ROOT / "backend/core/hermes/lesson_memory.py",
)
_lm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _lm
_spec.loader.exec_module(_lm)

Lesson = _lm.Lesson
LessonStore = _lm.LessonStore
to_prompt = _lm.to_prompt
extract_graph_refs = _lm.extract_graph_refs
lesson_from_outcome = _lm.lesson_from_outcome
maybe_capture_lesson = _lm.maybe_capture_lesson
maybe_recall_block = _lm.maybe_recall_block


class _Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"t{self.n}"


class _Step:
    def __init__(self, tool, args=None):
        self.tool, self.args = tool, args or {}


# === Lesson model ======================================================

def test_lesson_id_stable_by_kind_and_norm_text() -> None:
    a = Lesson(text="  Did  X ", kind="success")
    b = Lesson(text="did x", kind="success")
    c = Lesson(text="did x", kind="failure")
    assert a.id == b.id            # норм. текст
    assert a.id != c.id            # разный kind
    assert json.loads(json.dumps(a.to_dict()))["id"] == a.id


# === store add/dedupe/prune ===========================================

def test_add_and_all_roundtrip(tmp_path) -> None:
    s = LessonStore(str(tmp_path), clock=_Clock())
    assert s.add("u", Lesson(text="use graph_search before kag")) is True
    got = s.all("u")
    assert len(got) == 1 and got[0].created_at == "t1"


def test_add_dedupe_bumps_hits_merges_refs(tmp_path) -> None:
    clk = _Clock()
    s = LessonStore(str(tmp_path), clock=clk)
    s.add("u", Lesson(text="same lesson", tags=["a"], graph_refs=["n1"]))
    s.add("u", Lesson(text="SAME lesson", tags=["b"], graph_refs=["n2"],
                       pipeline_id="mtg_1"))
    all_ = s.all("u")
    assert len(all_) == 1
    L = all_[0]
    assert L.hits == 1
    assert set(L.tags) == {"a", "b"}
    assert set(L.graph_refs) == {"n1", "n2"}
    assert L.pipeline_id == "mtg_1"


def test_add_rejects_empty(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    assert s.add("u", Lesson(text="   ")) is False
    assert s.add("u", "nope") is False  # type: ignore[arg-type]


def test_prune_keeps_top_by_hits(tmp_path) -> None:
    s = LessonStore(str(tmp_path), max_lessons=2, clock=_Clock())
    for i in range(4):
        s.add("u", Lesson(text=f"lesson {i}"))
    assert len(s.all("u")) == 2


def test_user_isolation(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    s.add("alice", Lesson(text="alice lesson"))
    assert s.all("bob") == []


def test_corrupt_file_safe(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    p = pathlib.Path(tmp_path) / "u.json"
    p.write_text("{not json", encoding="utf-8")
    assert s.all("u") == []  # never raises


# === recall ============================================================

def test_recall_by_token_overlap(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    s.add("u", Lesson(text="deploy needs migrations first", tags=["deploy"]))
    s.add("u", Lesson(text="weather is nice today"))
    r = s.recall("u", "how to deploy with migrations", limit=5)
    assert r and "migrations" in r[0].text


def test_recall_failures_boosted(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    s.add("u", Lesson(text="x approach works", kind="success", tags=["x"]))
    s.add("u", Lesson(text="x approach fails on big input", kind="failure",
                      tags=["x"]))
    r = s.recall("u", "x approach", limit=2)
    assert r[0].kind == "failure"   # провал важнее не повторить


def test_recall_empty_query_or_none(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    s.add("u", Lesson(text="something"))
    assert s.recall("u", "", limit=3) == []
    assert s.recall("u", "zzz qqq", limit=3) == []  # no overlap


# === extractors ========================================================

def test_extract_graph_refs_and_pipeline() -> None:
    steps = [
        _Step("graph_search", {"node_id": "person_1"}),
        _Step("kag", {"entity_id": "proj_2", "pipeline_id": "mtg_42"}),
        _Step("done", {}),
    ]
    refs, pid = extract_graph_refs(steps)
    assert set(refs) == {"person_1", "proj_2"} and pid == "mtg_42"
    assert extract_graph_refs("garbage") == ([], "")


def test_lesson_from_outcome_success_and_failure() -> None:
    ok = lesson_from_outcome(success=True, task="find meetings",
                             tools=["search", "done", "search"])
    assert ok.kind == "success" and "search" in ok.tags and "done" not in ok.tags
    bad = lesson_from_outcome(success=False, task="x", tools=["a"],
                              summary="timeout")
    assert bad.kind == "failure" and "timeout" in bad.text
    assert lesson_from_outcome(success=True, task="  ", tools=[]) is None


# === orchestrators (env-gated, never-raises) ===========================

def test_capture_disabled_noop(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    r = maybe_capture_lesson(success=True, task="t",
                             steps=[_Step("a")], store=s, user_id="u",
                             enabled=False)
    assert r is None and s.all("u") == []


def test_capture_persists_with_graph_refs(tmp_path) -> None:
    s = LessonStore(str(tmp_path), clock=_Clock())
    lid = maybe_capture_lesson(
        success=True, task="find Иван meetings",
        steps=[_Step("graph_search", {"node_id": "n1"}), _Step("done")],
        store=s, user_id="u", enabled=True,
    )
    assert lid
    L = s.all("u")[0]
    assert L.kind == "success" and L.graph_refs == ["n1"]


def test_capture_failure_lesson(tmp_path) -> None:
    s = LessonStore(str(tmp_path), clock=_Clock())
    maybe_capture_lesson(success=False, task="hard task",
                         steps=[_Step("a")], store=s, user_id="u",
                         summary="LLM gave up", enabled=True)
    assert s.all("u")[0].kind == "failure"


def test_recall_block_disabled_and_enabled(tmp_path) -> None:
    s = LessonStore(str(tmp_path))
    s.add("u", Lesson(text="deploy needs migrations", tags=["deploy"]))
    assert maybe_recall_block("u", "deploy", store=s, enabled=False) == ""
    blk = maybe_recall_block("u", "how to deploy", store=s, enabled=True)
    assert "Уроки из прошлого" in blk and "migrations" in blk


def test_to_prompt_marks_kinds_and_graph() -> None:
    out = to_prompt([
        Lesson(text="ok thing", kind="success"),
        Lesson(text="bad thing", kind="failure", graph_refs=["n9"]),
    ])
    assert "✓ ok thing" in out
    assert "⚠️ bad thing" in out and "граф:n9" in out
    assert to_prompt([]) == ""
