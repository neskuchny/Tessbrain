"""Unit-тесты для core.bookmarks.service (W17).

Полные DB/Qdrant integration тесты — отдельно. Здесь покрываем:
- Bookmark dataclass round-trip
- make_search_payload
- BookmarkService с mocked deps (postgres/qdrant/embedder/llm)
- Add-flow без deps (graceful degradation)
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.bookmarks.service import (
    Bookmark,
    BookmarkService,
    make_search_payload,
)

# === Bookmark dataclass ================================================

def test_bookmark_to_dict_round_trip() -> None:
    b = Bookmark(
        id="b-1", user_id="u-1", url="https://x.com",
        title="T", description="D", tags=["a", "b"],
    )
    d = b.to_dict()
    assert d["id"] == "b-1"
    assert d["tags"] == ["a", "b"]
    assert d["url"] == "https://x.com"


def test_bookmark_from_row_dict() -> None:
    row = {
        "id": "b-1", "user_id": "u-1", "url": "https://x.com",
        "title": "T", "description": "D", "tags": ["a"], "section_id": "regs",
        "qdrant_point_id": "p-1", "status": "active",
    }
    b = Bookmark.from_row(row)
    assert b.id == "b-1"
    assert b.section_id == "regs"
    assert b.qdrant_point_id == "p-1"


def test_bookmark_from_row_missing_optional_fields() -> None:
    """Row без лишних полей — defaults используются."""
    row = {"id": "b-1", "user_id": "u-1", "url": "https://x.com"}
    b = Bookmark.from_row(row)
    assert b.title == ""
    assert b.tags == []
    assert b.status == "active"


# === make_search_payload ===============================================

def test_make_search_payload_includes_all_parts() -> None:
    out = make_search_payload(title="T", description="D", tags=["a", "b"])
    assert "T" in out and "D" in out and "a b" in out


def test_make_search_payload_skips_empty() -> None:
    out = make_search_payload(title="T", description="", tags=[])
    assert out == "T"


def test_make_search_payload_all_empty() -> None:
    assert make_search_payload(title="", description="", tags=[]) == ""


# === BookmarkService — graceful degradation ============================

def _run(coro):
    return asyncio.run(coro)


def test_search_returns_empty_without_deps() -> None:
    """Без qdrant/embedder — search возвращает пустой list."""
    svc = BookmarkService()
    out = _run(svc.search(query="something"))
    assert out == []


def test_search_returns_empty_with_empty_query() -> None:
    """Пустой query → no embedding API call."""
    svc = BookmarkService(qdrant=object(), embedder=object())
    out = _run(svc.search(query=""))
    assert out == []


def test_add_validates_url() -> None:
    svc = BookmarkService()
    with pytest.raises(ValueError):
        _run(svc.add(user_id="u-1", url="not-a-url"))


# === BookmarkService.add с mocked deps ================================

class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text):
        self.calls += 1
        return [0.1] * 8


class _FakeQdrant:
    def __init__(self) -> None:
        self.upserted = []
        self.searched = []

    async def upsert(self, *, points, collection):
        self.upserted.append((points, collection))

    async def search(self, *, vector, limit, filter_conditions, collection):
        self.searched.append({"limit": limit, "filter": filter_conditions})
        return [{
            "score": 0.9,
            "payload": {
                "bookmark_id": "b-1", "url": "https://x", "title": "T",
                "description": "D", "tags": ["a"], "section_id": "regs",
                "type": "bookmark",
            },
        }]


def test_add_indexes_in_qdrant_when_caller_provides_metadata() -> None:
    """Если caller дал title+desc+tags, enrich скипается, но Qdrant индексирует."""
    qdrant = _FakeQdrant()
    embedder = _FakeEmbedder()
    svc = BookmarkService(qdrant=qdrant, embedder=embedder)
    bookmark = _run(svc.add(
        user_id="u-1",
        url="https://wiki.local/x",
        title="My Page",
        description="A page",
        tags=["docs"],
        auto_enrich=False,
    ))
    assert bookmark.qdrant_point_id is not None
    assert len(qdrant.upserted) == 1
    points, collection = qdrant.upserted[0]
    assert collection == "bookmarks"
    payload = points[0]["payload"]
    assert payload["bookmark_id"] == bookmark.id
    assert payload["title"] == "My Page"
    assert payload["tags"] == ["docs"]


def test_add_skips_qdrant_when_no_embedder() -> None:
    qdrant = _FakeQdrant()
    svc = BookmarkService(qdrant=qdrant, embedder=None)
    bookmark = _run(svc.add(
        user_id="u-1", url="https://x.com",
        title="T", description="D", tags=["a"], auto_enrich=False,
    ))
    assert bookmark.qdrant_point_id is None
    assert qdrant.upserted == []


def test_search_uses_section_filter() -> None:
    qdrant = _FakeQdrant()
    embedder = _FakeEmbedder()
    svc = BookmarkService(qdrant=qdrant, embedder=embedder)
    out = _run(svc.search(query="foo", section_id="regs", top_k=5))
    assert len(out) == 1
    call = qdrant.searched[0]
    assert call["limit"] == 5
    assert call["filter"]["type"] == "bookmark"
    assert call["filter"]["section_id"] == "regs"


def test_search_omits_section_when_not_provided() -> None:
    qdrant = _FakeQdrant()
    embedder = _FakeEmbedder()
    svc = BookmarkService(qdrant=qdrant, embedder=embedder)
    _run(svc.search(query="foo", top_k=5))
    call = qdrant.searched[0]
    assert "section_id" not in call["filter"]


def test_search_handles_qdrant_failure() -> None:
    """Qdrant падает → возвращаем []."""
    class _Boom(_FakeQdrant):
        async def search(self, **kw):
            raise RuntimeError("qdrant down")

    embedder = _FakeEmbedder()
    svc = BookmarkService(qdrant=_Boom(), embedder=embedder)
    out = _run(svc.search(query="foo"))
    assert out == []


def test_search_handles_embedder_failure() -> None:
    """Embedder падает → []."""
    class _BadEmbedder:
        async def embed(self, text):
            raise RuntimeError("embedder down")

    qdrant = _FakeQdrant()
    svc = BookmarkService(qdrant=qdrant, embedder=_BadEmbedder())
    out = _run(svc.search(query="foo"))
    assert out == []
