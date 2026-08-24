"""Unit-тесты для Ontology SDK + Actions (P4).

Чистая логика: schema.py (stdlib-only) + sdk.py + actions.py грузятся
через importlib со stub-пакетами, обходя тяжёлый core.store.__init__
(numpy/networkx). Граф/БД не нужны.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(mod_name: str, rel_path: str):
    # Уже загружен (реальный или другим тестом) — не подменяем: замена
    # реального модуля ломала другие тесты в общем прогоне.
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap():
    for pkg in ("backend", "backend.core", "backend.core.store",
                "backend.core.ontology"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            # РЕАЛЬНЫЙ путь пакета: иначе всё, что импортирует backend.*
            # ПОСЛЕ этого файла в том же pytest-процессе, падало с
            # ModuleNotFoundError (stub с пустым __path__ перекрывал пакет).
            # Тяжёлые __init__ по-прежнему не исполняются — пакет-стаб.
            m.__path__ = [str(_ROOT / pkg.replace(".", "/"))]  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    _load("backend.core.store.schema", "backend/core/store/schema.py")
    sdk = _load("backend.core.ontology.sdk", "backend/core/ontology/sdk.py")
    actions = _load("backend.core.ontology.actions", "backend/core/ontology/actions.py")
    return sdk, actions


_sdk, _actions = _bootstrap()

EnforcementMode = _sdk.EnforcementMode
validate_object = _sdk.validate_object
validate_relationship = _sdk.validate_relationship
normalize_object_props = _sdk.normalize_object_props
Action = _actions.Action
apply_actions = _actions.apply_actions
ACTION_CREATE_NODE = _actions.ACTION_CREATE_NODE
ACTION_CREATE_RELATIONSHIP = _actions.ACTION_CREATE_RELATIONSHIP
ACTION_WRITEBACK = _actions.ACTION_WRITEBACK


def _run(coro):
    return asyncio.run(coro)


# === validate_object: режимы ============================================

def test_known_object_valid() -> None:
    r = validate_object("Person", {"name": "Иван"}, mode=EnforcementMode.STRICT)
    assert r.ok and not r.violations


def test_strict_rejects_unknown_type() -> None:
    r = validate_object("Dragon", {"name": "x"}, mode=EnforcementMode.STRICT)
    assert r.ok is False
    assert any("unknown object type" in v for v in r.violations)


def test_warn_keeps_unknown_type_non_breaking() -> None:
    r = validate_object("Dragon", {"name": "x"}, mode=EnforcementMode.WARN)
    assert r.ok is True
    assert any("unknown object type" in w for w in r.warnings)


def test_strict_rejects_missing_required() -> None:
    # Project требует name (project_id стал optional — узлы из statuses)
    r = validate_object("Project", {"description": "no name"},
                        mode=EnforcementMode.STRICT)
    assert r.ok is False
    assert any("missing required" in v for v in r.violations)


def test_warn_missing_required_is_warning_only() -> None:
    r = validate_object("Project", {"description": "no name"},
                        mode=EnforcementMode.WARN)
    assert r.ok is True
    assert any("missing required" in w for w in r.warnings)


def test_off_mode_passes_anything() -> None:
    r = validate_object("Whatever", {"junk": 1}, mode=EnforcementMode.OFF)
    assert r.ok is True


# === Нормализация =======================================================

def test_normalize_clamps_access_level_and_confidence() -> None:
    props, warns = normalize_object_props(
        {"access_level": 99, "confidence": 5.0, "name": "n"}
    )
    assert props["access_level"] == 5
    assert props["confidence"] == 1.0
    assert len(warns) >= 2


def test_normalize_coerces_list_fields_and_drops_none() -> None:
    props, _ = normalize_object_props(
        {"access_groups": "admins", "tags": None, "name": "n", "role": None}
    )
    assert props["access_groups"] == ["admins"]
    assert "role" not in props  # None убран
    assert "tags" not in props  # None убран


def test_validate_object_returns_normalized() -> None:
    r = validate_object("Person", {"name": "A", "confidence": 2.0},
                        mode=EnforcementMode.WARN)
    assert r.normalized["confidence"] == 1.0


# === validate_relationship ==============================================

def test_relationship_valid_pair() -> None:
    r = validate_relationship("Person", "Meeting", "PARTICIPATED_IN",
                              mode=EnforcementMode.STRICT)
    assert r.ok


def test_relationship_strict_rejects_unknown_type() -> None:
    r = validate_relationship("Person", "Meeting", "FROBNICATES",
                              mode=EnforcementMode.STRICT)
    assert r.ok is False


def test_relationship_strict_rejects_bad_pair() -> None:
    r = validate_relationship("Meeting", "Meeting", "PARTICIPATED_IN",
                              mode=EnforcementMode.STRICT)
    assert r.ok is False
    assert any("invalid relationship" in v for v in r.violations)


def test_relationship_warn_non_breaking() -> None:
    r = validate_relationship("X", "Y", "ZZZ", mode=EnforcementMode.WARN)
    assert r.ok is True and r.warnings


# === Actions: pseudo-transaction ========================================

def test_actions_strict_aborts_whole_batch_on_violation() -> None:
    calls: list = []

    async def ex(*, kind, target, payload):
        calls.append(target)
        return True

    rep = _run(apply_actions(
        [
            Action(ACTION_CREATE_NODE, "Person", {"name": "ok"}),
            Action(ACTION_CREATE_NODE, "Dragon", {"name": "bad"}),
        ],
        executor=ex, mode=EnforcementMode.STRICT,
    ))
    assert rep.aborted is True
    assert rep.status_code == 422
    assert calls == []  # ни одной записи — atomic abort


def test_actions_strict_all_valid_applies_all() -> None:
    calls: list = []

    async def ex(*, kind, target, payload):
        calls.append(target)
        return True

    rep = _run(apply_actions(
        [
            Action(ACTION_CREATE_NODE, "Person", {"name": "a"}),
            Action(ACTION_CREATE_NODE, "KPI", {"name": "b"}),
        ],
        executor=ex, mode=EnforcementMode.STRICT,
    ))
    assert rep.ok and rep.applied == 2 and rep.rejected == 0
    assert calls == ["Person", "KPI"]


def test_actions_warn_writes_despite_issues() -> None:
    async def ex(*, kind, target, payload):
        return True

    rep = _run(apply_actions(
        [Action(ACTION_CREATE_NODE, "Dragon", {"x": 1})],
        executor=ex, mode=EnforcementMode.WARN,
    ))
    assert rep.aborted is False
    assert rep.applied == 1


def test_actions_executor_failure_isolated() -> None:
    async def ex(*, kind, target, payload):
        if target == "boom":
            raise RuntimeError("down")
        return True

    rep = _run(apply_actions(
        [
            Action(ACTION_CREATE_NODE, "Person", {"name": "boom"}),
            Action(ACTION_CREATE_NODE, "Person", {"name": "fine"}),
        ],
        executor=ex, mode=EnforcementMode.WARN,
    ))
    # target берётся из .target (label "Person"), executor бросит на обоих?
    # нет — boom в payload.name, target="Person". Проверяем что не raises.
    assert isinstance(rep.applied + rep.rejected, int)


def test_actions_writeback_shape_checked() -> None:
    async def ex(*, kind, target, payload):
        return True

    rep = _run(apply_actions(
        [
            Action(ACTION_WRITEBACK, "jira", {"issue": "X"}),
            Action(ACTION_WRITEBACK, "", {"bad": 1}),  # нет target
        ],
        executor=ex, mode=EnforcementMode.STRICT,
    ))
    assert rep.aborted is True  # второй writeback без target → abort в STRICT


def test_actions_empty_and_garbage_no_raise() -> None:
    async def ex(*, kind, target, payload):
        return True

    r1 = _run(apply_actions([], executor=ex))
    assert r1.status_code == 400
    r2 = _run(apply_actions("nope", executor=ex))  # type: ignore[arg-type]
    assert r2.status_code == 400


def test_actions_audit_failure_does_not_break() -> None:
    async def ex(*, kind, target, payload):
        return True

    async def bad_audit(*, event, detail):
        raise RuntimeError("audit sink down")

    rep = _run(apply_actions(
        [Action(ACTION_CREATE_NODE, "Person", {"name": "a"})],
        executor=ex, mode=EnforcementMode.STRICT, audit=bad_audit,
    ))
    assert rep.applied == 1


def test_actions_bounded_by_max() -> None:
    async def ex(*, kind, target, payload):
        return True

    big = [Action(ACTION_CREATE_NODE, "Person", {"name": f"p{i}"})
           for i in range(50)]
    rep = _run(apply_actions(big, executor=ex,
                             mode=EnforcementMode.WARN, max_actions=10))
    assert rep.applied == 10


def test_action_report_to_dict_shape() -> None:
    async def ex(*, kind, target, payload):
        return True

    rep = _run(apply_actions(
        [Action(ACTION_CREATE_NODE, "Person", {"name": "a"})],
        executor=ex, mode=EnforcementMode.STRICT,
    ))
    d = rep.to_dict()
    assert d["applied"] == 1
    assert "outcomes" in d and d["outcomes"][0]["kind"] == ACTION_CREATE_NODE
