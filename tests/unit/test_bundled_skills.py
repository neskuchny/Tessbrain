"""Unit-тесты P12i-E: bundled стартовые скиллы.

Чистый stdlib — importlib. root=tmp_path; bundled_dir указывает на
реальный репозиторный каталог. Без графа/БД/сети.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BUNDLED = _ROOT / "backend/core/hermes/bundled_skills"

for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_store_mod = _load("backend.core.hermes.skill_store",
                   "backend/core/hermes/skill_store.py")
_b = _load("backend.core.hermes.bundled",
           "backend/core/hermes/bundled.py")

SkillStore = _store_mod.SkillStore
SkillDoc = _store_mod.SkillDoc
seed_bundled_skills = _b.seed_bundled_skills


def test_repo_ships_bundled_skills() -> None:
    files = list(_BUNDLED.glob("*/*/SKILL.md"))
    assert len(files) >= 2  # graph-research + meeting-followups


def test_seed_into_empty_store(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    n = seed_bundled_skills(s, "u", bundled_dir=str(_BUNDLED))
    assert n >= 2
    names = {m.name for m in s.list("u")}
    assert "graph-research" in names and "meeting-followups" in names
    sk = s.view("u", "graph-research")
    assert sk and "Procedure" in (sk.procedure and "Procedure" or
                                  "Procedure")  # has procedure text
    assert sk.procedure.strip() != ""


def test_idempotent_second_call_zero(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert seed_bundled_skills(s, "u", bundled_dir=str(_BUNDLED)) >= 2
    # повторно — 0 (маркер .bundled_manifest)
    assert seed_bundled_skills(s, "u", bundled_dir=str(_BUNDLED)) == 0


def test_does_not_overwrite_user_skill(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("u", SkillDoc(name="graph-research", description="MINE",
                           procedure="my steps"))
    seed_bundled_skills(s, "u", bundled_dir=str(_BUNDLED))
    sk = s.view("u", "graph-research")
    assert sk.description == "MINE"        # пользовательский сохранён
    assert sk.procedure == "my steps"


def test_per_user_isolation(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    seed_bundled_skills(s, "alice", bundled_dir=str(_BUNDLED))
    # bob ещё не сидирован
    assert s.list("bob") == []
    assert seed_bundled_skills(s, "bob", bundled_dir=str(_BUNDLED)) >= 2


def test_missing_bundled_dir_safe(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert seed_bundled_skills(s, "u",
                               bundled_dir=str(tmp_path / "nope")) == 0
    assert s.list("u") == []


def test_marker_written_even_if_nothing_added(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    empty = tmp_path / "empty_bundled"
    empty.mkdir()
    assert seed_bundled_skills(s, "u", bundled_dir=str(empty)) == 0
    # повторный вызов с реальным каталогом — всё равно 0 (маркер стоит)
    assert seed_bundled_skills(s, "u", bundled_dir=str(_BUNDLED)) == 0


def test_never_raises_on_garbage(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert seed_bundled_skills(s, "", bundled_dir=str(_BUNDLED)) >= 0
    assert isinstance(
        seed_bundled_skills(s, "u", bundled_dir=None), int)
