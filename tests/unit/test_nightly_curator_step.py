"""Unit-тесты для интеграции ProfileCurator в nightly_consolidation (W7).

Проверяем _curate_user_profile:
- skip когда флаг выключен,
- skip когда нет истории / мало сообщений,
- skip когда curator не вернул proposals,
- happy-path: proposals → apply_proposals → diff → update_profile,
- skip когда diff пустой (proposals не меняют профиль).

Подход: загружаем nightly_consolidation через importlib.util и стабим
тяжёлые зависимости (`backend.core.store`, `backend.core.config.flags`,
`backend.core.memory.profile_curator`, `backend.memory.user_profiles`,
`backend.core.llm.router`) в sys.modules. Это нужно потому что
backend/core/store/__init__.py тянет numpy, а в sandbox его нет.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from dataclasses import dataclass, field
from typing import Any

# === Stubs ДО импорта nightly_consolidation =================================

_TB = pathlib.Path(__file__).resolve().parents[2]


def _ensure_pkg(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as package
    sys.modules[name] = mod
    return mod


# backend.core.store.tenant_paths — нужны graph_path_for_user/graphs_dir.
# Грузим напрямую файл, минуя backend.core.store.__init__.py (он тянет numpy).
_ensure_pkg("backend")
_ensure_pkg("backend.core")
_ensure_pkg("backend.core.store")

_tp_path = _TB / "backend" / "core" / "store" / "tenant_paths.py"
_tp_spec = importlib.util.spec_from_file_location(
    "backend.core.store.tenant_paths", _tp_path
)
_tp_mod = importlib.util.module_from_spec(_tp_spec)
sys.modules["backend.core.store.tenant_paths"] = _tp_mod
_tp_spec.loader.exec_module(_tp_mod)


# === Загрузка nightly_consolidation ==========================================

_NC_PATH = _TB / "backend" / "core" / "corrections" / "nightly_consolidation.py"
_ensure_pkg("backend.core.corrections")
_nc_spec = importlib.util.spec_from_file_location(
    "backend.core.corrections.nightly_consolidation", _NC_PATH
)
_nc_mod = importlib.util.module_from_spec(_nc_spec)
sys.modules[_nc_spec.name] = _nc_mod
_nc_spec.loader.exec_module(_nc_mod)
NightlyConsolidationService = _nc_mod.NightlyConsolidationService


# === Хелперы для лёгких стабов ===============================================

class _FakeFlags:
    def __init__(self, **kw: Any) -> None:
        self.enable_profile_curator = kw.get("enable_profile_curator", True)


def _install_config_flags(enable: bool) -> None:
    pkg = _ensure_pkg("backend.core.config")
    pkg.flags = _FakeFlags(enable_profile_curator=enable)


@dataclass
class _FakeProfile:
    user_id: str
    preferred_language: str = "ru"
    expertise: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "preferred_language": self.preferred_language,
            "expertise": list(self.expertise),
        }


class _FakeProfileService:
    def __init__(self, history: list[dict[str, str]], profile: _FakeProfile) -> None:
        self._history = history
        self._profile = profile
        self.updates: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        return None

    async def get_recent_queries(self, user_id: str, limit: int = 50):
        return self._history[:limit]

    async def get_profile(self, user_id: str) -> _FakeProfile:
        return self._profile

    async def update_profile(self, user_id: str, updates: dict[str, Any]) -> _FakeProfile:
        self.updates.append(updates)
        for k, v in updates.items():
            setattr(self._profile, k, v)
        return self._profile


def _install_user_profiles(service: _FakeProfileService) -> None:
    pkg = _ensure_pkg("backend.memory")
    mod = types.ModuleType("backend.memory.user_profiles")

    class UserProfileService:
        def __new__(cls, *a, **kw):  # type: ignore[no-untyped-def]
            return service

    mod.UserProfileService = UserProfileService
    sys.modules["backend.memory.user_profiles"] = mod
    pkg.user_profiles = mod


def _install_llm_router() -> None:
    pkg = _ensure_pkg("backend.core.llm")
    mod = types.ModuleType("backend.core.llm.router")

    class LLMRouter:
        pass

    mod.LLMRouter = LLMRouter
    sys.modules["backend.core.llm.router"] = mod
    pkg.router = mod


def _install_curator(proposals: list[Any]) -> None:
    pkg = _ensure_pkg("backend.core.memory")
    mod = types.ModuleType("backend.core.memory.profile_curator")

    @dataclass
    class ProfileUpdate:
        action: str
        field: str
        value: Any
        confidence: float = 0.9
        reason: str = ""

    class ProfileCurator:
        def __init__(self, llm_router=None, **kw: Any) -> None:
            self.llm_router = llm_router

        async def curate(self, *, messages, current_profile):
            return list(proposals)

    def apply_proposals(profile: dict[str, Any], proposals_in: list[Any]) -> dict[str, Any]:
        new = dict(profile)
        for p in proposals_in:
            if p.action == "set":
                new[p.field] = p.value
            elif p.action == "append":
                cur = new.get(p.field) or []
                if isinstance(cur, list) and p.value not in cur:
                    new[p.field] = [*cur, p.value]
            elif p.action == "remove":
                cur = new.get(p.field) or []
                if isinstance(cur, list) and p.value in cur:
                    new[p.field] = [x for x in cur if x != p.value]
        return new

    mod.ProfileCurator = ProfileCurator
    mod.ProfileUpdate = ProfileUpdate
    mod.apply_proposals = apply_proposals
    sys.modules["backend.core.memory.profile_curator"] = mod
    pkg.profile_curator = mod
    return ProfileUpdate  # type: ignore[return-value]


# === Тесты ==================================================================

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if sys.version_info < (3, 10) else asyncio.run(coro)


def _make_history(n: int) -> list[dict[str, str]]:
    return [{"query": f"q{i}"} for i in range(n)]


def test_curator_step_skips_when_flag_off(tmp_path) -> None:
    _install_config_flags(enable=False)
    svc = NightlyConsolidationService(
        users_data_path=str(tmp_path),
        log_path=str(tmp_path / "logs"),
    )
    out = _run(svc._curate_user_profile("user-1"))
    assert out == {"proposed": 0, "applied": 0}


def test_curator_step_skips_when_history_too_short(tmp_path) -> None:
    _install_config_flags(enable=True)
    fake_svc = _FakeProfileService(history=_make_history(2), profile=_FakeProfile("u"))
    _install_user_profiles(fake_svc)
    _install_llm_router()
    _install_curator(proposals=[])

    svc = NightlyConsolidationService(
        users_data_path=str(tmp_path),
        log_path=str(tmp_path / "logs"),
    )
    out = _run(svc._curate_user_profile("u"))
    assert out["applied"] == 0
    assert fake_svc.updates == []


def test_curator_step_no_proposals_no_update(tmp_path) -> None:
    _install_config_flags(enable=True)
    fake_svc = _FakeProfileService(history=_make_history(10), profile=_FakeProfile("u"))
    _install_user_profiles(fake_svc)
    _install_llm_router()
    _install_curator(proposals=[])

    svc = NightlyConsolidationService(
        users_data_path=str(tmp_path),
        log_path=str(tmp_path / "logs"),
    )
    out = _run(svc._curate_user_profile("u"))
    assert out["proposed"] == 0
    assert out["applied"] == 0
    assert fake_svc.updates == []


def test_curator_step_applies_diff(tmp_path) -> None:
    _install_config_flags(enable=True)
    fake_svc = _FakeProfileService(
        history=_make_history(10),
        profile=_FakeProfile("u", preferred_language="ru", expertise=["python"]),
    )
    _install_user_profiles(fake_svc)
    _install_llm_router()
    ProfileUpdate = _install_curator(proposals=[])
    # Подсовываем proposals: меняем язык + добавляем экспертизу.
    sys.modules["backend.core.memory.profile_curator"].ProfileCurator.curate = (  # type: ignore[attr-defined]
        lambda self, *, messages, current_profile: _async_return([
            ProfileUpdate(action="set", field="preferred_language", value="en"),
            ProfileUpdate(action="append", field="expertise", value="ml"),
        ])
    )

    svc = NightlyConsolidationService(
        users_data_path=str(tmp_path),
        log_path=str(tmp_path / "logs"),
    )
    out = _run(svc._curate_user_profile("u"))
    assert out["proposed"] == 2
    assert out["applied"] == 2
    # update_profile вызывался один раз с обоими изменениями.
    assert len(fake_svc.updates) == 1
    upd = fake_svc.updates[0]
    assert upd["preferred_language"] == "en"
    assert upd["expertise"] == ["python", "ml"]


def test_curator_step_no_update_when_proposals_match_current(tmp_path) -> None:
    _install_config_flags(enable=True)
    fake_svc = _FakeProfileService(
        history=_make_history(10),
        profile=_FakeProfile("u", preferred_language="en"),
    )
    _install_user_profiles(fake_svc)
    _install_llm_router()
    ProfileUpdate = _install_curator(proposals=[])
    # Proposal сообщает то же значение, что уже стоит в профиле.
    sys.modules["backend.core.memory.profile_curator"].ProfileCurator.curate = (  # type: ignore[attr-defined]
        lambda self, *, messages, current_profile: _async_return([
            ProfileUpdate(action="set", field="preferred_language", value="en"),
        ])
    )

    svc = NightlyConsolidationService(
        users_data_path=str(tmp_path),
        log_path=str(tmp_path / "logs"),
    )
    out = _run(svc._curate_user_profile("u"))
    assert out["proposed"] == 1
    assert out["applied"] == 0
    assert fake_svc.updates == []


async def _async_return(value):
    return value
