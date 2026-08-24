"""Unit-тесты P8: чистые хелперы lineage-API + edge-lineage контракт.

litestar и core.store — тяжёлые/недоступны в юнит-окружении, поэтому
стабим litestar и грузим только нужные модули через importlib.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_litestar() -> None:
    """Stub только если litestar не установлен. Должен быть достаточно
    полным, чтобы не сломать другие route-тесты, которые тоже делают
    `try: import litestar` (иначе их guard решит, что litestar есть,
    но без exceptions/params → collection error)."""
    try:
        import litestar  # noqa: F401
        return
    except ImportError:
        pass

    ls = types.ModuleType("litestar")
    ls.Router = lambda *a, **kw: types.SimpleNamespace(args=a, kwargs=kw)
    for verb in ("get", "post", "put", "patch", "delete"):
        setattr(ls, verb, lambda *a, **kw: (lambda fn: fn))
    sys.modules["litestar"] = ls

    exc_mod = types.ModuleType("litestar.exceptions")

    class HTTPException(Exception):
        def __init__(self, status_code: int = 500, detail: str = "") -> None:
            self.status_code = status_code
            self.detail = detail

    exc_mod.HTTPException = HTTPException
    sys.modules["litestar.exceptions"] = exc_mod

    params_mod = types.ModuleType("litestar.params")
    params_mod.Parameter = lambda **kw: None
    sys.modules["litestar.params"] = params_mod


def _bootstrap():
    # backend.core.auth — потому что routes/lineage.py импортирует из него
    # user_guard. Пакета в этом списке не было, и тест падал
    # «No module named 'backend.core.auth'»: импорт в маршруте появился
    # позже, чем список заглушек, и разъехался с ним.
    for pkg in ("backend", "backend.core", "backend.core.lineage",
                "backend.core.auth", "backend.api", "backend.api.routes"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    _stub_litestar()
    _load("backend.core.auth.user_guard", "backend/core/auth/user_guard.py")
    _load("backend.core.lineage.lineage", "backend/core/lineage/lineage.py")
    return _load("backend.api.routes.lineage",
                 "backend/api/routes/lineage.py")


_api = _bootstrap()
_extract_lineage = _api._extract_lineage
_coerce_lineage_json = _api._coerce_lineage_json
_pipeline_of = _api._pipeline_of
_lin = sys.modules["backend.core.lineage.lineage"]
stamp_props = _lin.stamp_props


# === _extract_lineage ===================================================

def test_extract_from_stamped_props() -> None:
    p = stamp_props({}, created_by="extractor", pipeline_id="mtg_1",
                    stage="extract")
    out = _extract_lineage(p)
    assert out["has_lineage"] is True
    assert out["lineage"]["created_by"] == "extractor"
    assert len(out["trace"]) == 1


def test_extract_from_empty_is_safe() -> None:
    out = _extract_lineage({})
    assert out["has_lineage"] is False
    assert out["trace"] == []


def test_extract_handles_none() -> None:
    out = _extract_lineage(None)  # type: ignore[arg-type]
    assert out["has_lineage"] is False


# === _coerce_lineage_json (Neo4j хранит вложенное как строку) ===========

def test_coerce_json_string_to_dict() -> None:
    inner = {"created_by": "x", "history": []}
    props = {"_lineage": json.dumps(inner)}
    _coerce_lineage_json(props)
    assert isinstance(props["_lineage"], dict)
    assert props["_lineage"]["created_by"] == "x"


def test_coerce_broken_json_drops_key() -> None:
    props = {"_lineage": "{not valid"}
    _coerce_lineage_json(props)
    assert "_lineage" not in props


def test_coerce_leaves_dict_untouched() -> None:
    props = {"_lineage": {"created_by": "y"}}
    _coerce_lineage_json(props)
    assert props["_lineage"]["created_by"] == "y"


def test_coerce_no_lineage_key_noop() -> None:
    props = {"name": "n"}
    _coerce_lineage_json(props)
    assert props == {"name": "n"}


# === edge-lineage serialize контракт (Neo4j) ============================
# _apply_edge_lineage сериализует _lineage в JSON-строку для Neo4j;
# проверяем сам контракт roundtrip через P7-модуль + json.

def test_edge_lineage_serialize_roundtrip() -> None:
    stamped = stamp_props({}, created_by="svc", pipeline_id="p",
                          stage="create_relationship:MEMBER_OF",
                          note="a->b")
    serialized = json.dumps(stamped["_lineage"], ensure_ascii=False)
    back = {"_lineage": serialized}
    _coerce_lineage_json(back)
    rec = _extract_lineage(back)
    assert rec["lineage"]["created_by"] == "svc"
    assert rec["trace"][0]["note"] == "a->b"


def test_router_constructed() -> None:
    assert _api.router is not None


# === _pipeline_of (P9 overlay) =========================================

def test_pipeline_of_from_lineage() -> None:
    p = stamp_props({}, created_by="svc", pipeline_id="mtg_42",
                    stage="extract")
    assert _pipeline_of(p) == "mtg_42"


def test_pipeline_of_from_flat_mirror() -> None:
    # узел без вложенного _lineage, но с плоским зеркалом
    assert _pipeline_of({"pipeline_id": "flat_7"}) == "flat_7"


def test_pipeline_of_empty_and_garbage() -> None:
    assert _pipeline_of({}) == ""
    assert _pipeline_of({"pipeline_id": ""}) == ""
    assert _pipeline_of(None) == ""  # type: ignore[arg-type]


def test_pipeline_router_has_both_handlers() -> None:
    # router сконструирован с lineage_trace + lineage_pipeline.
    # Стаб litestar.Router варьируется между route-тестами (другой
    # файл мог установить свой), поэтому проверяем мягко.
    router = _api.router
    kwargs = getattr(router, "kwargs", None)
    if not kwargs:
        # стаб вернул None/без kwargs — функции всё равно должны быть
        assert callable(_api.lineage_trace)
        assert callable(_api.lineage_pipeline)
        return
    assert len(kwargs.get("route_handlers", [])) == 2
