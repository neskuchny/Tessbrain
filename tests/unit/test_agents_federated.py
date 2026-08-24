"""Unit-тесты для get_task_spec_system после merge understanding-layer.

Архитектура (merged):
  - per-user кэш `_task_spec_systems` (dict) вместо process-global синглтона
    — закрывает мультитенант-утечку (#7): /tasks/process больше НЕ переиспользует
    граф чужого юзера.
  - граф строится через federated view merged_graph_view_for_user
    (personal ∪ org) — Wave 2.3: agent видит promoted org-данные команды.
  - пересоздание системы, когда число узлов выросло (свежие данные #7).
  - без user_id → cache_key "__default__", federated НЕ зовётся, storage=None.
  - _caller_user_id: verify-first (issue #107) — strict mode отклоняет
    неподписанный токен; compat mode извлекает sub.

agents.py подгружается через importlib (минуя backend.api.routes.__init__,
который тянет autogen — pre-existing dep issue в этом env).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest

# Real-modules bootstrap.
_REAL: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    pkg = types.ModuleType("backend.core.store")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = pkg
    _REAL["backend.core.store"] = pkg
    # monkeypatch.setattr резолвит через `getattr(parent, child)` — поэтому
    # parent-package backend.core должен явно знать о child store.
    import backend.core as _bc
    setattr(_bc, "store", pkg)

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")
# graph_view нужен для monkeypatch.setattr на merged_graph_view_for_user.
# Загружаем явно (минуя backend.core.store/__init__ который тянет numpy).
_gv = _force_load("backend.core.store.graph_view", "backend/core/store/graph_view.py")
_gb_mod = _force_load("backend.core.store.graph_builder", "backend/core/store/graph_builder.py")
# Привязываем подмодули к parent — monkeypatch.setattr резолвит через
# атрибут-цепочку, не через sys.modules.
_store_pkg = sys.modules["backend.core.store"]
setattr(_store_pkg, "tenant_paths", _tp)
setattr(_store_pkg, "graph_view", _gv)
setattr(_store_pkg, "graph_builder", _gb_mod)


def _load_agents_module():
    """Загружаем agents.py напрямую, обходя routes/__init__.py (autogen)."""
    # Подменяем тяжёлые imports чтобы не падали при exec_module agents.py.
    if "autogen" not in sys.modules:
        autogen_stub = types.ModuleType("autogen")
        for name in (
            "ConversableAgent", "GroupChat", "GroupChatManager",
            "UserProxyAgent", "register_function",
        ):
            setattr(autogen_stub, name, type(name, (), {}))
        sys.modules["autogen"] = autogen_stub
        for sub in ("autogen.a2a", "autogen.a2a.client", "autogen.a2a.server",
                    "autogen.agentchat", "autogen.agentchat.group",
                    "autogen.agentchat.group.guardrails",
                    "autogen.agentchat.group.patterns",
                    "autogen.agents", "autogen.agents.experimental"):
            if sub not in sys.modules:
                m = types.ModuleType(sub)
                for sym in ("A2aRemoteAgent", "A2aAgentServer", "CardSettings",
                            "a_initiate_group_chat", "initiate_group_chat",
                            "LLMGuardrail", "RegexGuardrail",
                            "AutoPattern", "DefaultPattern",
                            "ReasoningAgent", "ThinkNode"):
                    setattr(m, sym, type(sym, (), {}))
                sys.modules[sub] = m

    if "backend.api" not in sys.modules:
        m = types.ModuleType("backend.api")
        m.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api")]
        sys.modules["backend.api"] = m
    if "backend.api.routes" not in sys.modules:
        m = types.ModuleType("backend.api.routes")
        m.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api" / "routes")]
        sys.modules["backend.api.routes"] = m

    agents_path = Path(__file__).resolve().parents[2] / "backend" / "api" / "routes" / "agents.py"
    spec = importlib.util.spec_from_file_location("backend.api.routes.agents", agents_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.api.routes.agents"] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    agents_mod = _load_agents_module()
except Exception as e:
    pytest.skip(
        f"agents.py не загружается в этом env (pre-existing dep issue: {e})",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _restore_modules():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture
def reset_task_spec_cache():
    """Сбрасываем per-user кэш перед/после каждого теста, иначе второй
    тест увидит cached system от первого."""
    agents_mod._task_spec_systems = {}
    yield
    agents_mod._task_spec_systems = {}


def _run(coro):
    return asyncio.run(coro)


def _bearer(user_id: str) -> str:
    return "Bearer " + jwt.encode({"sub": user_id}, "secret", algorithm="HS256")


# =============================================================================
# _caller_user_id (issue #107 verify-first)
# =============================================================================


def test_caller_user_id_extracts_sub_in_compat_mode(monkeypatch):
    """compat mode (strict OFF) — sub извлекается из неподписанного токена."""
    import backend.core.auth.service_token as st
    monkeypatch.setattr(st, "_strict_chat_auth", lambda: False)
    u = str(uuid.uuid4())
    assert agents_mod._caller_user_id(_bearer(u)) == u


def test_caller_user_id_rejects_unverified_in_strict_mode(monkeypatch):
    """strict mode (default) — неподтверждённая подпись → None (анти-спуфинг)."""
    import backend.core.auth.service_token as st
    monkeypatch.setattr(st, "_strict_chat_auth", lambda: True)
    u = str(uuid.uuid4())
    assert agents_mod._caller_user_id(_bearer(u)) is None


def test_caller_user_id_returns_none_for_missing_header():
    assert agents_mod._caller_user_id(None) is None
    assert agents_mod._caller_user_id("") is None


def test_caller_user_id_returns_none_for_garbage():
    assert agents_mod._caller_user_id("Bearer not-a-jwt") is None
    assert agents_mod._caller_user_id("Basic dXNlcjpwYXNz") is None


# =============================================================================
# get_task_spec_system: federated view + per-user кэш
# =============================================================================


def _patch_merged(monkeypatch, nodes: int = 5):
    fake_gb = types.SimpleNamespace(
        nx_graph=types.SimpleNamespace(number_of_nodes=lambda: nodes),
    )
    merged_mock = AsyncMock(return_value=fake_gb)
    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user",
        merged_mock,
    )
    create_mock = lambda **kw: types.SimpleNamespace(storage=kw.get("storage"))
    monkeypatch.setattr(agents_mod, "create_task_specification_system", create_mock)
    return merged_mock


def test_user_id_triggers_federated_view(reset_task_spec_cache, monkeypatch):
    """С user_id — merged_graph_view_for_user вызван (federated personal ∪ org),
    система закэширована под ключом user_id."""
    user_id = str(uuid.uuid4())
    merged_mock = _patch_merged(monkeypatch, nodes=5)

    result = _run(agents_mod.get_task_spec_system(
        types.SimpleNamespace(graph=None), user_id=user_id))

    merged_mock.assert_called_once_with(user_id, use_networkx=None)
    assert result is not None
    assert agents_mod._task_spec_systems[user_id] is result


def test_per_user_cache_isolates_tenants(reset_task_spec_cache, monkeypatch):
    """Мультитенант-фикс (#7): разные user_id → разные системы (нет утечки
    графа чужого юзера через общий синглтон)."""
    _patch_merged(monkeypatch, nodes=5)
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())

    s1 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=u1))
    s2 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=u2))

    assert s1 is not s2
    assert agents_mod._task_spec_systems[u1] is s1
    assert agents_mod._task_spec_systems[u2] is s2


def test_cache_reused_when_graph_not_grown(reset_task_spec_cache, monkeypatch):
    """Тот же user_id и не выросший граф → переиспользуем закэшированную систему."""
    _patch_merged(monkeypatch, nodes=5)
    user_id = str(uuid.uuid4())

    s1 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=user_id))
    s2 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=user_id))

    assert s1 is s2  # кэш переиспользован


def test_cache_refreshed_when_graph_grows(reset_task_spec_cache, monkeypatch):
    """Граф вырос (новые узлы #7) → система пересоздаётся со свежими данными."""
    user_id = str(uuid.uuid4())
    # первый вызов — 5 узлов
    _patch_merged(monkeypatch, nodes=5)
    s1 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=user_id))
    # второй вызов — граф вырос до 20
    _patch_merged(monkeypatch, nodes=20)
    s2 = _run(agents_mod.get_task_spec_system(types.SimpleNamespace(graph=None), user_id=user_id))

    assert s1 is not s2  # пересоздана из-за роста графа


def test_no_user_id_skips_federated(reset_task_spec_cache, monkeypatch):
    """Без user_id — federated НЕ зовётся, система под ключом __default__,
    storage=None (backward-compat для callers без auth)."""
    merged_mock = _patch_merged(monkeypatch, nodes=5)

    result = _run(agents_mod.get_task_spec_system(
        types.SimpleNamespace(graph=None), user_id=None))

    merged_mock.assert_not_called()
    assert result is not None
    assert agents_mod._task_spec_systems["__default__"] is result
    assert result.storage is None


def test_federated_failure_does_not_crash(reset_task_spec_cache, monkeypatch):
    """Если merged_graph_view упал — не валим запрос, система создаётся
    без графа (storage=None)."""
    user_id = str(uuid.uuid4())
    merged_mock = AsyncMock(side_effect=RuntimeError("merge boom"))
    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user",
        merged_mock,
    )
    create_mock = lambda **kw: types.SimpleNamespace(storage=kw.get("storage"))
    monkeypatch.setattr(agents_mod, "create_task_specification_system", create_mock)

    result = _run(agents_mod.get_task_spec_system(
        types.SimpleNamespace(graph=None), user_id=user_id))

    merged_mock.assert_called_once()
    assert result is not None  # упали мягко, система всё равно создана
    assert result.storage is None
