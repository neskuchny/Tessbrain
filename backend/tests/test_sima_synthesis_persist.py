"""Регрессия персиста синтеза SIMA (Фаза 0.4): результат генерации (ТЗ и т.п.)
автоматически становится артефактом библиотеки с обратной ссылкой на проект.
Раньше синтез жил только в поле проекта — «кубик = атом» не замыкался."""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

from backend.api.routes import sima  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_persist_builds_tz_artifact_with_backref():
    captured = {}

    class _DB:
        async def create_artifact(self, uid, data):
            captured["uid"] = uid
            captured["data"] = data
            return {"id": "art-1", **data}

    import backend.db.sima_client as _sc
    with patch.object(_sc, "get_sima_db", AsyncMock(return_value=_DB())):
        aid = _run(sima._persist_synthesis_artifact(
            "user-1", "proj-1", atype="tz", title="ТЗ: X",
            markdown="# Спецификация\n...", source_ids=["m1", "d2"]))

    assert aid == "art-1"
    d = captured["data"]
    assert captured["uid"] == "user-1"
    assert d["artifactType"] == "tz"
    assert d["sourceProjectId"] == "proj-1"          # обратная ссылка
    assert d["payload"]["markdown"].startswith("# Спецификация")
    assert d["payload"]["sourceIds"] == ["m1", "d2"]  # трассировка источников
    assert "#tz" in d["tags"] and "#синтез" in d["tags"]


def test_persist_noops_on_empty_content():
    """Пустой синтез не создаёт мусорный артефакт."""
    assert _run(sima._persist_synthesis_artifact(
        "u", "p", atype="tz", title="t", markdown="   ")) is None
    assert _run(sima._persist_synthesis_artifact(
        "u", "", atype="tz", title="t", markdown="x")) is None


def test_persist_never_raises_on_db_error():
    """Провал персиста НЕ ломает генерацию — возвращает None, не исключение."""
    class _DB:
        async def create_artifact(self, uid, data):
            raise RuntimeError("db down")

    import backend.db.sima_client as _sc
    with patch.object(_sc, "get_sima_db", AsyncMock(return_value=_DB())):
        assert _run(sima._persist_synthesis_artifact(
            "u", "p", atype="tz", title="t", markdown="x")) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
