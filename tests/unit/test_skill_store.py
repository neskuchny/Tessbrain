"""Unit-тесты P12b: Hermes-track skill-store.

Модуль не тянет тяжёлый backend.core.* — грузим напрямую через
importlib. Часы инъектируем, root = tmp_path. Без графа/БД/сети.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load():
    for pkg in ("backend", "backend.core", "backend.core.hermes"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(
        "backend.core.hermes.skill_store",
        _ROOT / "backend/core/hermes/skill_store.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_m = _load()
SkillDoc = _m.SkillDoc
SkillStore = _m.SkillStore
serialize = _m.serialize
parse = _m.parse


class _Clock:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"t{self.n}"


def _doc(name="deploy-prod", cat="ops"):
    return SkillDoc(
        name=name, description="Deploy to prod safely", category=cat,
        tags=["deploy", "ops"], requires=["terminal"],
        when_to_use="When shipping a release.",
        procedure="1. run tests\n2. build\n3. deploy",
        pitfalls="Don't skip migrations.",
        verification="curl /healthz returns ok",
    )


# === serialize / parse =================================================

def test_roundtrip_preserves_all_fields() -> None:
    d = _doc()
    back = parse(serialize(d))
    assert back.name == d.name
    assert back.description == d.description
    assert back.category == "ops"
    assert back.tags == ["deploy", "ops"]
    assert back.requires == ["terminal"]
    assert "run tests" in back.procedure
    assert back.pitfalls == "Don't skip migrations."
    assert back.verification.startswith("curl")


def test_parse_garbage_never_raises() -> None:
    for bad in ("", "   ", "not a skill", "---\nbroken: [", 12345, None):
        d = parse(bad)  # type: ignore[arg-type]
        assert isinstance(d, SkillDoc)
        assert d.name == ""


def test_parse_no_frontmatter_extracts_sections() -> None:
    txt = "# x\n## Procedure\nstep one\n## Pitfalls\nbad stuff\n"
    d = parse(txt)
    assert d.procedure == "step one"
    assert d.pitfalls == "bad stuff"


# === CRUD ==============================================================

def test_create_view_roundtrip(tmp_path) -> None:
    clk = _Clock()
    s = SkillStore(str(tmp_path), clock=clk)
    assert s.create("user1", _doc()) is True
    got = s.view("user1", "deploy-prod")
    assert got is not None
    assert got.description == "Deploy to prod safely"
    assert got.created_at == "t1" and got.updated_at == "t1"


def test_view_absent_returns_none(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert s.view("u", "nope") is None
    assert s.exists("u", "nope") is False


def test_list_is_metadata_only_and_isolated(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("alice", _doc("skill-a", "ops"))
    s.create("alice", _doc("skill-b", "research"))
    s.create("bob", _doc("skill-c", "ops"))
    metas = s.list("alice")
    assert {m.name for m in metas} == {"skill-a", "skill-b"}  # bob isolated
    # Level 0 = метаданные, без тела
    assert all(not hasattr(m, "procedure") for m in metas)
    assert metas[0].to_dict().keys() >= {"name", "description", "category"}


def test_patch_replaces_and_bumps_version(tmp_path) -> None:
    clk = _Clock()
    s = SkillStore(str(tmp_path), clock=clk)
    s.create("u", _doc())  # version 1, t1
    ok = s.patch("u", "deploy-prod", "Don't skip migrations.",
                 "Always run migrations first.")
    assert ok is True
    v = s.view("u", "deploy-prod")
    assert "Always run migrations first." in v.pitfalls
    assert v.version == 2  # bumped


def test_patch_missing_old_or_skill_returns_false(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("u", _doc())
    assert s.patch("u", "deploy-prod", "NONEXISTENT", "x") is False
    assert s.patch("u", "ghost", "a", "b") is False


def test_delete(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("u", _doc())
    assert s.delete("u", "deploy-prod") is True
    assert s.view("u", "deploy-prod") is None
    assert s.delete("u", "deploy-prod") is False  # already gone


# === safety / bounds ===================================================

def test_max_skills_enforced(tmp_path) -> None:
    s = SkillStore(str(tmp_path), max_skills=3)
    for i in range(5):
        s.create("u", _doc(f"skill-{i}", "general"))
    assert len(s.list("u")) == 3


def test_slug_sanitizes_path_traversal(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    # вредные имя/категория не должны вылезти за root
    s.create("u", SkillDoc(name="../../evil", category="../x",
                           description="d"))
    # файл создан где-то ВНУТРИ tmp_path, не выше
    created = list(pathlib.Path(tmp_path).rglob("SKILL.md"))
    assert created, "skill should be created"
    for p in created:
        assert str(tmp_path) in str(p.resolve())


def test_create_rejects_empty_name(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert s.create("u", SkillDoc(name="", description="d")) is False


def test_create_overwrite_keeps_created_at(tmp_path) -> None:
    clk = _Clock()
    s = SkillStore(str(tmp_path), clock=clk)
    s.create("u", _doc())                       # created_at=t1
    d2 = _doc()
    d2.description = "updated"
    s.create("u", d2)                           # updated_at=t2
    v = s.view("u", "deploy-prod")
    assert v.description == "updated"
    assert v.created_at == "t1"                  # сохранён при перезаписи
    assert v.updated_at == "t2"                  # обновлён
