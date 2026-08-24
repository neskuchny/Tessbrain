"""Unit-тесты для core.bookmarks.fetcher (W17)."""
from __future__ import annotations

import pytest
from backend.core.bookmarks.fetcher import (
    AirGapRejected,
    FetchError,
    extract_title,
    fetch_url,
    html_to_text,
    is_url_safe,
)

# === is_url_safe =========================================================

@pytest.mark.parametrize("url", [
    "http://example.com",
    "https://example.com/foo?a=1",
    "https://192.168.1.5/internal",
])
def test_is_url_safe_non_enterprise(url: str) -> None:
    assert is_url_safe(url, enterprise_mode=False) is True


@pytest.mark.parametrize("url", [
    "ftp://example.com",
    "javascript:alert(1)",
    "http://",
    "",
])
def test_is_url_safe_rejects_non_http(url: str) -> None:
    assert is_url_safe(url, enterprise_mode=False) is False


@pytest.mark.parametrize("url", [
    "http://localhost:8080",
    "https://192.168.1.5",
    "http://gw.mycorp.local",
    "https://wiki.svc.cluster.local",
    "http://10.0.0.5",
])
def test_is_url_safe_enterprise_internal_allowed(url: str) -> None:
    assert is_url_safe(url, enterprise_mode=True) is True


@pytest.mark.parametrize("url", [
    "https://example.com",
    "https://api.openai.com/v1",
    "http://1.1.1.1",
])
def test_is_url_safe_enterprise_external_rejected(url: str) -> None:
    assert is_url_safe(url, enterprise_mode=True) is False


# === extract_title =======================================================

def test_extract_title_simple() -> None:
    assert extract_title("<html><title>My Page</title></html>") == "My Page"


def test_extract_title_with_attributes() -> None:
    out = extract_title('<title lang="en">Hello</title>')
    assert out == "Hello"


def test_extract_title_unescapes_entities() -> None:
    assert extract_title("<title>A &amp; B</title>") == "A & B"


def test_extract_title_strips_inner_tags() -> None:
    assert extract_title("<title>foo <b>bar</b></title>") == "foo bar"


def test_extract_title_missing() -> None:
    assert extract_title("<html><body>no title</body></html>") == ""


def test_extract_title_multiline() -> None:
    out = extract_title("<title>\nMulti\nLine\n</title>")
    assert "Multi" in out and "Line" in out


# === html_to_text ========================================================

def test_html_to_text_basic() -> None:
    out = html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in out and "world" in out


def test_html_to_text_strips_scripts() -> None:
    """JS не должен попасть в plain text."""
    raw = '<p>safe</p><script>alert("bad")</script>'
    out = html_to_text(raw)
    assert "safe" in out
    assert "alert" not in out
    assert "bad" not in out


def test_html_to_text_strips_styles() -> None:
    raw = "<style>.x{color:red}</style><p>hi</p>"
    out = html_to_text(raw)
    assert "hi" in out
    assert "color" not in out


def test_html_to_text_unescapes_entities() -> None:
    out = html_to_text("<p>a &amp; b &lt;c&gt;</p>")
    assert "a & b" in out
    assert "<c>" in out


def test_html_to_text_collapses_whitespace() -> None:
    out = html_to_text("<p>a   \n\n\n  b</p>")
    assert out == "a b"


def test_html_to_text_empty() -> None:
    assert html_to_text("") == ""


# === fetch_url errors (без сети) =========================================

def test_fetch_url_air_gap_rejection() -> None:
    """В enterprise режиме external URL → AirGapRejected."""
    import asyncio
    with pytest.raises(AirGapRejected):
        asyncio.run(fetch_url("https://example.com", enterprise_mode=True))


def test_fetch_url_invalid_scheme() -> None:
    import asyncio
    with pytest.raises(FetchError):
        asyncio.run(fetch_url("ftp://example.com"))


def test_fetch_url_no_host() -> None:
    import asyncio
    with pytest.raises(FetchError):
        asyncio.run(fetch_url("http://"))
