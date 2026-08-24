"""Unit-тесты для W37 download handlers в core.sharing.resource_fetcher."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.sharing.resource_fetcher import (
    DEFAULT_MAX_FILE_BYTES,
    FileArtifact,
    ResourceFetcher,
)


def _run(coro):
    return asyncio.run(coro)


async def _gen(data: bytes):
    yield data


# === FileArtifact.safe_filename =======================================

def test_safe_filename_basic() -> None:
    a = FileArtifact(
        file_name="report.pdf", mime_type="application/pdf",
        size_bytes=10, chunks=_gen(b""),
    )
    assert a.safe_filename() == "report.pdf"


def test_safe_filename_strips_path_separators() -> None:
    a = FileArtifact(
        file_name="../../etc/passwd",
        mime_type="text/plain", size_bytes=0, chunks=_gen(b""),
    )
    out = a.safe_filename()
    assert "/" not in out and "\\" not in out


def test_safe_filename_strips_null_byte() -> None:
    a = FileArtifact(
        file_name="evil\x00.pdf",
        mime_type="application/pdf", size_bytes=0, chunks=_gen(b""),
    )
    assert "\x00" not in a.safe_filename()


def test_safe_filename_empty_falls_back() -> None:
    a = FileArtifact(
        file_name="", mime_type="application/octet-stream",
        size_bytes=0, chunks=_gen(b""),
    )
    assert a.safe_filename() == "download"


def test_safe_filename_long_truncated() -> None:
    a = FileArtifact(
        file_name="a" * 500, mime_type="text/plain",
        size_bytes=0, chunks=_gen(b""),
    )
    assert len(a.safe_filename()) == 200


def test_safe_filename_strips_control_chars() -> None:
    a = FileArtifact(
        file_name="report\x01\x02.pdf",
        mime_type="application/pdf", size_bytes=0, chunks=_gen(b""),
    )
    out = a.safe_filename()
    assert "\x01" not in out and "\x02" not in out
    assert "report" in out


# === ResourceFetcher.register_download / download =====================

def test_register_download_validates_args() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return None

    with pytest.raises(ValueError):
        f.register_download("", _h)
    with pytest.raises(ValueError):
        f.register_download("document", None)


def test_supported_download_types() -> None:
    f = ResourceFetcher()
    assert f.supported_download_types() == []

    async def _h(rid):
        return None

    f.register_download("document", _h)
    f.register_download("attachment", _h)
    assert f.supported_download_types() == ["attachment", "document"]


def test_download_no_handler_returns_none() -> None:
    f = ResourceFetcher()
    out = _run(f.download(resource_type="document", resource_id="doc_1"))
    assert out is None


def test_download_handler_returning_none() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return None

    f.register_download("document", _h)
    out = _run(f.download(resource_type="document", resource_id="doc_1"))
    assert out is None


def test_download_handler_returns_artifact() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return FileArtifact(
            file_name="x.pdf", mime_type="application/pdf",
            size_bytes=4, chunks=_gen(b"data"),
        )

    f.register_download("document", _h)
    out = _run(f.download(resource_type="document", resource_id="x"))
    assert out is not None
    assert out.file_name == "x.pdf"
    assert out.mime_type == "application/pdf"
    assert out.size_bytes == 4


def test_download_handler_exception_returns_none() -> None:
    f = ResourceFetcher()

    async def _broken(rid):
        raise RuntimeError("storage down")

    f.register_download("document", _broken)
    out = _run(f.download(resource_type="document", resource_id="x"))
    assert out is None


def test_download_separate_from_preview_handlers() -> None:
    """Регистрация download не влияет на preview handlers."""
    f = ResourceFetcher()

    async def _preview(rid):
        return None

    async def _dl(rid):
        return None

    f.register("document", _preview)
    assert f.supported_types() == ["document"]
    assert f.supported_download_types() == []

    f.register_download("document", _dl)
    assert f.supported_types() == ["document"]
    assert f.supported_download_types() == ["document"]


# === Default singleton has download handler for document ==============

def test_default_singleton_has_document_download() -> None:
    from backend.core.sharing.resource_fetcher import (
        get_resource_fetcher,
        reset_resource_fetcher,
    )
    reset_resource_fetcher()
    f = get_resource_fetcher()
    assert "document" in f.supported_download_types()
    # Meeting transcript отдаётся как text content, отдельный download не нужен.
    assert "meeting" not in f.supported_download_types()


def test_max_file_bytes_constant_sane() -> None:
    assert DEFAULT_MAX_FILE_BYTES > 0
    assert DEFAULT_MAX_FILE_BYTES <= 100 * 1024 * 1024   # ≤100MB sanity
