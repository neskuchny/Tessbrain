"""LLM output DLP filter (W14 — data loss prevention).

Пост-фильтр для текстовых ответов LLM: маскирует PII и блокирует
public/external URL'ы. Запускается на ВСЕ ответы перед возвратом
клиенту, если флаг `enable_output_dlp=true`.

Зачем:
- защита от prompt-injection-exfiltration (атакующий подсовывает в RAG
  «выпиши секреты в base64» — фильтр режет результат);
- защита от случайного echo PII (модель повторила email из контекста);
- в air-gap режиме — последняя линия защиты от leak'а internal URL'ов
  если они попали в context.

Дизайн:
- Pure functions, без зависимостей. Регексы детерминистичны и
  тестируемы. Никаких LLM-judge на горячем пути — это для async-audit.
- Replace вместо block: маскируем `[REDACTED:email]`. Block-режим
  опционален через `block_on_match=True` и поднимает `DLPViolation`.
- Возвращаем `FilterResult(text, redactions, violations)` чтобы caller
  мог логировать.

Не покрывает:
- адреса, имена, кастомные secret patterns — их добавляйте в
  `DLPConfig.custom_patterns`.
- бинарные секреты (private keys в base64) — отдельная задача.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# === Известные PII patterns ==============================================
# Все патерны компилируются с DOTALL=False, MULTILINE=True — границы
# слов важны (не хотим срабатывать на середину base64).

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# E.164 + local форматы для RU/EU. Не покрывает все мировые форматы
# намеренно — лучше пропустить чем сломать LLM-output ложным редактированием.
_PHONE_RE = re.compile(
    r"\b(?:\+?[0-9]{1,3}[\s\-]?)?(?:\(?\d{3,4}\)?[\s\-]?)?\d{3}[\s\-]?\d{2,4}[\s\-]?\d{2,4}\b"
)

# IBAN (RU/EU). Используем простой format-check, не контрольную сумму.
_IBAN_RE = re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{4,30}\b")

# Visa/MC/Amex/Discover в любом разделителе.
_CREDIT_CARD_RE = re.compile(
    r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6\d{3})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"
)

# Известные secret prefixes. List обновляется по мере появления новых форматов.
# Разделяем на word-bounded токены и multi-line PEM — у них разные anchor-правила.
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9]{20,}|"             # OpenAI
    r"sk-ant-[A-Za-z0-9_\-]{20,}|"      # Anthropic
    r"AIza[0-9A-Za-z_\-]{35}|"          # Google API
    r"ghp_[A-Za-z0-9]{36}|"             # GitHub PAT
    r"gho_[A-Za-z0-9]{36}|"             # GitHub OAuth
    r"xox[baprs]-[A-Za-z0-9\-]{10,}|"   # Slack
    r"AKIA[0-9A-Z]{16}"                 # AWS access key
    r")"
)

_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----[A-Za-z0-9+/=\s]+-----END [A-Z ]+PRIVATE KEY-----"
)

# RU passport (4+6 digits) и СНИЛС (3-3-3-2). СНИЛС с пробелами и без.
_RU_PASSPORT_RE = re.compile(r"\b\d{4}[\s\-]?\d{6}\b")
_RU_SNILS_RE = re.compile(r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}\b")

# URL — мы хотим различать internal/external. Здесь только match всего URL.
_URL_RE = re.compile(
    r"\bhttps?://[A-Za-z0-9._\-~:%/?#\[\]@!$&'()*+,;=]+",
    re.IGNORECASE,
)

# === Helpers =============================================================

# Внутренние домены — заведомо safe в air-gap deploy.
_INTERNAL_HOST_SUFFIXES = (
    ".local", ".internal", ".lan", ".home", ".corp",
    ".intranet", ".svc.cluster.local", ".svc", ".cluster.local",
)
_INTERNAL_HOST_LITERALS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_internal_host(host: str) -> bool:
    h = host.lower()
    if h in _INTERNAL_HOST_LITERALS:
        return True
    if h.endswith(_INTERNAL_HOST_SUFFIXES):
        return True
    if "." not in h:
        # bare hostname (docker compose) — internal
        return True
    return False


@dataclass
class DLPConfig:
    """Конфигурация фильтра.

    Атрибуты-флаги управляют конкретными rule sets. Default — все включены.
    `custom_patterns` — список (regex, label) для проектных правил.
    `block_on_match` — поднять `DLPViolation` вместо маскирования.
    `redact_external_urls` — режет URL'ы, которые НЕ internal.
    """
    redact_emails: bool = True
    redact_phones: bool = True
    redact_iban: bool = True
    redact_credit_cards: bool = True
    redact_secret_tokens: bool = True
    redact_ru_passport: bool = True
    redact_ru_snils: bool = True
    redact_external_urls: bool = False  # opt-in: для air-gap deploy
    custom_patterns: list[tuple[re.Pattern, str]] = field(default_factory=list)
    block_on_match: bool = False


@dataclass
class FilterResult:
    text: str
    redactions: list[tuple[str, str]] = field(default_factory=list)  # [(label, original)]

    @property
    def had_redactions(self) -> bool:
        return len(self.redactions) > 0


class DLPViolation(Exception):
    """Поднимается, когда block_on_match=True и фильтр нашёл совпадение."""

    def __init__(self, label: str, snippet: str) -> None:
        self.label = label
        self.snippet = snippet
        super().__init__(f"DLP violation: {label}")


def _apply_pattern(
    pattern: re.Pattern,
    label: str,
    text: str,
    cfg: DLPConfig,
    redactions: list[tuple[str, str]],
) -> str:
    def _sub(m: re.Match) -> str:
        original = m.group(0)
        redactions.append((label, original))
        if cfg.block_on_match:
            raise DLPViolation(label, original[:50])
        return f"[REDACTED:{label}]"
    return pattern.sub(_sub, text)


def filter_output(text: str, config: Optional[DLPConfig] = None) -> FilterResult:
    """Применить DLP-правила к строке.

    Возвращает `FilterResult(text, redactions)`. Идемпотентно
    (повторное применение не даёт новых редактирований, потому что
    `[REDACTED:...]` не матчит ни один pattern).
    """
    cfg = config or DLPConfig()
    redactions: list[tuple[str, str]] = []
    out = text

    if cfg.redact_secret_tokens:
        # PEM первым — он multi-line, не имеет word boundaries.
        out = _apply_pattern(_PEM_PRIVATE_KEY_RE, "secret", out, cfg, redactions)
        out = _apply_pattern(_SECRET_TOKEN_RE, "secret", out, cfg, redactions)
    if cfg.redact_credit_cards:
        out = _apply_pattern(_CREDIT_CARD_RE, "credit_card", out, cfg, redactions)
    if cfg.redact_iban:
        out = _apply_pattern(_IBAN_RE, "iban", out, cfg, redactions)
    if cfg.redact_emails:
        out = _apply_pattern(_EMAIL_RE, "email", out, cfg, redactions)
    if cfg.redact_ru_snils:
        out = _apply_pattern(_RU_SNILS_RE, "ru_snils", out, cfg, redactions)
    if cfg.redact_ru_passport:
        out = _apply_pattern(_RU_PASSPORT_RE, "ru_passport", out, cfg, redactions)
    if cfg.redact_phones:
        # Phone после passport/snils — иначе те маскируются как phone.
        out = _apply_pattern(_PHONE_RE, "phone", out, cfg, redactions)

    if cfg.redact_external_urls:
        def _url_sub(m: re.Match) -> str:
            url = m.group(0)
            host = _extract_host(url)
            if _is_internal_host(host):
                return url
            redactions.append(("external_url", url))
            if cfg.block_on_match:
                raise DLPViolation("external_url", url)
            return "[REDACTED:external_url]"
        out = _URL_RE.sub(_url_sub, out)

    for pattern, label in cfg.custom_patterns:
        out = _apply_pattern(pattern, label, out, cfg, redactions)

    return FilterResult(text=out, redactions=redactions)


def _extract_host(url: str) -> str:
    # Простой парсер чтобы не тащить urllib для горячего пути.
    if "://" not in url:
        return ""
    rest = url.split("://", 1)[1]
    for delim in "/?#":
        if delim in rest:
            rest = rest.split(delim, 1)[0]
            break
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    # strip port
    if rest.startswith("["):
        # IPv6 literal
        end = rest.find("]")
        return rest[1:end] if end != -1 else rest[1:]
    if ":" in rest:
        rest = rest.split(":", 1)[0]
    return rest


__all__ = [
    "DLPConfig",
    "DLPViolation",
    "FilterResult",
    "filter_output",
]
