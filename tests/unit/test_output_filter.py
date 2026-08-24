"""Unit-тесты для core.llm.output_filter (W14 DLP)."""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

# Загружаем под изолированным namespace чтобы обойти core.llm.__init__.
_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "llm" / "output_filter.py"
)
_spec = importlib.util.spec_from_file_location("_dlp_isolated", _PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

DLPConfig = _module.DLPConfig
DLPViolation = _module.DLPViolation
filter_output = _module.filter_output
_is_internal_host = _module._is_internal_host


# === Email ==============================================================

def test_redacts_email() -> None:
    r = filter_output("Contact me at john.doe@example.com please")
    assert "[REDACTED:email]" in r.text
    assert "john.doe@example.com" not in r.text
    assert ("email", "john.doe@example.com") in r.redactions


def test_multiple_emails_all_redacted() -> None:
    r = filter_output("a@x.com and b@y.com")
    assert r.text.count("[REDACTED:email]") == 2


def test_email_redaction_off() -> None:
    cfg = DLPConfig(redact_emails=False)
    r = filter_output("contact a@b.com", cfg)
    assert "a@b.com" in r.text
    assert r.redactions == []


# === Secret tokens ======================================================

@pytest.mark.parametrize("token", [
    "sk-" + "a" * 40,                         # OpenAI
    "sk-ant-" + "x" * 30,                     # Anthropic
    "AIza" + "B" * 35,                        # Google
    "ghp_" + "1" * 36,                        # GitHub PAT
    "AKIA" + "Z" * 16,                        # AWS
    "xoxb-" + "1234567890",                   # Slack
])
def test_redacts_known_secrets(token: str) -> None:
    r = filter_output(f"key is {token} ok")
    assert "[REDACTED:secret]" in r.text
    assert token not in r.text


def test_redacts_pem_private_key() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "ABCDEFGHIJKLMNOP\n+/=\n"
        "-----END RSA PRIVATE KEY-----"
    )
    r = filter_output(f"key:\n{pem}\nend")
    assert "[REDACTED:secret]" in r.text
    assert "BEGIN RSA PRIVATE KEY" not in r.text


# === Credit cards / IBAN ================================================

def test_redacts_credit_card() -> None:
    r = filter_output("card 4111-1111-1111-1111 valid")
    assert "[REDACTED:credit_card]" in r.text


def test_redacts_iban() -> None:
    r = filter_output("send to DE89370400440532013000 by friday")
    assert "[REDACTED:iban]" in r.text


# === RU PII =============================================================

def test_redacts_snils() -> None:
    r = filter_output("СНИЛС 123-456-789 01")
    assert "[REDACTED:ru_snils]" in r.text


# === External URL handling ==============================================

def test_external_url_redacted_when_enabled() -> None:
    cfg = DLPConfig(redact_external_urls=True)
    r = filter_output("see https://api.openai.com/v1/foo for info", cfg)
    assert "[REDACTED:external_url]" in r.text
    assert "openai.com" not in r.text


def test_internal_url_kept() -> None:
    cfg = DLPConfig(redact_external_urls=True)
    r = filter_output("local: http://vllm:8000/v1 ok", cfg)
    assert "http://vllm:8000/v1" in r.text


def test_internal_localhost_kept() -> None:
    cfg = DLPConfig(redact_external_urls=True)
    r = filter_output("http://localhost:3000/foo", cfg)
    assert "localhost" in r.text


def test_url_filter_off_by_default() -> None:
    """redact_external_urls = False по умолчанию — обычный chat/RAG не должен ломаться."""
    r = filter_output("see https://example.com")
    assert "example.com" in r.text


# === Block mode =========================================================

def test_block_on_match_raises() -> None:
    cfg = DLPConfig(block_on_match=True)
    with pytest.raises(DLPViolation) as exc:
        filter_output("contact a@b.com please", cfg)
    assert exc.value.label == "email"


# === Custom patterns ====================================================

def test_custom_pattern() -> None:
    cfg = DLPConfig(custom_patterns=[(re.compile(r"INTERNAL-\d{4}"), "internal_id")])
    r = filter_output("ticket INTERNAL-1234 in queue", cfg)
    assert "[REDACTED:internal_id]" in r.text


# === Idempotency ========================================================

def test_idempotent() -> None:
    """Повторное применение фильтра не даёт новых редактирований."""
    r1 = filter_output("email is a@b.com")
    r2 = filter_output(r1.text)
    assert r1.text == r2.text
    assert r2.redactions == []


# === Empty / passthrough ================================================

def test_empty_string() -> None:
    r = filter_output("")
    assert r.text == ""
    assert r.redactions == []


def test_clean_text_passthrough() -> None:
    text = "Привет, как дела сегодня в офисе?"
    r = filter_output(text)
    assert r.text == text
    assert r.redactions == []


# === Internal host helper ===============================================

@pytest.mark.parametrize("host,expected", [
    ("localhost", True),
    ("127.0.0.1", True),
    ("::1", True),
    ("vllm", True),
    ("api.openai.com", False),
    ("ollama.svc.cluster.local", True),
    ("gw.mycorp.local", True),
    ("example.com", False),
])
def test_is_internal_host(host: str, expected: bool) -> None:
    assert _is_internal_host(host) is expected
