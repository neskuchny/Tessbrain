# -*- coding: utf-8 -*-
"""Push-уведомления шины данных: webhook «появилось новое».

Закрывает разрыв из бизнес-карты: «потребитель может только сам опрашивать
шину». Теперь потребитель может оставить адрес — и шина сама постучится,
когда у владельца появились новые данные.

Принцип, на котором всё держится: ПУШ НЕ НЕСЁТ ДАННЫХ. В уведомлении —
только тип события и счётчики («обработана встреча, извлечено N фактов»).
За самими данными потребитель приходит обычным путём — через конвейер
выдачи со срезом, редакцией и аудитом. Так у данных остаётся один выход,
и push не становится вторым, нефильтрованным.

Безопасность:
- подпись HMAC-SHA256 телом запроса (заголовок X-Tessbrain-Signature) —
  секрет выдаётся при регистрации канала и виден один раз;
- адрес проверяется при регистрации: в закрытом контуре (enterprise_mode)
  внешние адреса запрещены — уведомление тоже трафик наружу;
- доставка best-effort: таймаут 10с, после N подряд ошибок канал
  отключается сам (защита от вечного долбления мёртвого адреса).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# После скольких ПОДРЯД неудачных доставок канал отключается сам.
MAX_CONSECUTIVE_FAILURES = 10
DELIVERY_TIMEOUT_S = 10.0

# События, на которые можно подписаться.
KNOWN_EVENTS = ("meeting_processed", "dataset_refreshed", "snapshot_updated")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WebhookChannel:
    """Webhook-канал потребителя шины. Секрет хранится хэшем."""
    id: str
    tenant_id: str
    consumer_id: str
    endpoint_url: str
    events: List[str] = field(default_factory=list)
    secret_hash: str = ""          # sha256 от секрета — сверка без хранения
    secret_hint: str = ""          # последние 4 символа — для UI
    is_active: bool = True
    consecutive_failures: int = 0
    last_delivery_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WebhookChannel":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


def validate_endpoint(url: str) -> Optional[str]:
    """Причина, по которой адрес не годится, или None.

    Обычный режим: только https (уведомление подписано, но канал всё равно
    не должен ходить открытым текстом); локальные http-адреса разрешены —
    это интеграции внутри своей сети.
    Закрытый контур: только внутренние адреса — пуш наружу это тот же
    выход за периметр, что и запрос к облачной модели.
    """
    u = (url or "").strip()
    if not u:
        return "адрес пуст"
    from backend.core.security.perimeter import (
        enterprise_mode_enabled,
        is_internal_url,
    )
    internal = is_internal_url(u)
    if enterprise_mode_enabled():
        if not internal:
            return ("закрытый контур: уведомления наружу запрещены — "
                    "адрес должен быть внутри вашей сети")
        return None
    if u.lower().startswith("https://"):
        return None
    if u.lower().startswith("http://") and internal:
        return None
    return "нужен https:// (или http:// на адрес внутри вашей сети)"


def new_channel(*, tenant_id: str, consumer_id: str, endpoint_url: str,
                events: Optional[List[str]] = None) -> Dict[str, Any]:
    """Создать канал. Возвращает {ok, channel, secret} — секрет виден
    ОДИН раз, дальше хранится только хэш."""
    reason = validate_endpoint(endpoint_url)
    if reason:
        return {"ok": False, "error": reason}
    wanted = [e for e in (events or []) if e in KNOWN_EVENTS] or list(KNOWN_EVENTS)
    secret = secrets.token_urlsafe(32)
    ch = WebhookChannel(
        id=f"wh_{uuid.uuid4().hex[:12]}",
        tenant_id=str(tenant_id),
        consumer_id=str(consumer_id),
        endpoint_url=endpoint_url.strip(),
        events=wanted,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        secret_hint=secret[-4:],
    )
    return {"ok": True, "channel": ch, "secret": secret}


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── Хранилище каналов (per-tenant JSON) ─────────────────────────────────

def _path(tenant_id: str) -> str:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        root = str(_DATA_ROOT)
    except Exception:
        root = os.getenv("TESSENT_DATA_DIR", "data")
    safe = "".join(c for c in str(tenant_id) if c.isalnum() or c in "-_")
    return os.path.join(root, "data_bus_webhooks", f"{safe or 'default'}.json")


class WebhookStore:
    def __init__(self, tenant_id: str, path: Optional[str] = None):
        self._tenant = str(tenant_id)
        self._path = path or _path(tenant_id)

    def _load(self) -> List[dict]:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return list(d.get("channels") or [])
        except Exception:
            logger.warning("webhook store load failed", exc_info=True)
        return []

    def _write(self, channels: List[dict]) -> None:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with file_lock(self._path):
            atomic_write_json(self._path, {"channels": channels})

    def list(self, *, only_active: bool = False) -> List[dict]:
        out = self._load()
        if only_active:
            out = [c for c in out if c.get("is_active")]
        return out

    def save(self, ch: WebhookChannel) -> None:
        channels = self._load()
        for i, raw in enumerate(channels):
            if raw.get("id") == ch.id:
                channels[i] = ch.to_dict()
                self._write(channels)
                return
        channels.append(ch.to_dict())
        self._write(channels)

    def get(self, channel_id: str) -> Optional[WebhookChannel]:
        for raw in self._load():
            if raw.get("id") == channel_id:
                return WebhookChannel.from_dict(raw)
        return None

    def delete(self, channel_id: str) -> bool:
        channels = self._load()
        rest = [c for c in channels if c.get("id") != channel_id]
        if len(rest) == len(channels):
            return False
        self._write(rest)
        return True


# ── Доставка ────────────────────────────────────────────────────────────

async def notify(tenant_id: str, event_type: str,
                 summary: Optional[Dict[str, Any]] = None,
                 *, store: Optional[WebhookStore] = None,
                 secret_lookup=None) -> Dict[str, Any]:
    """Разослать событие активным каналам владельца. Никогда не raises.

    В теле — только тип события, время и счётчики из summary (числа и
    короткие строки). Ни фактов, ни текстов, ни имён: данные потребитель
    забирает через конвейер выдачи, который его срежет и запишет в аудит.

    `secret_lookup(channel_id) -> str|None` — способ получить секрет для
    подписи. По умолчанию секрета у нас нет (хранится только хэш), поэтому
    подпись ставится ключом развёртывания WEBHOOK_SIGNING_KEY, если задан;
    иначе уведомление уходит без подписи, и это честно видно в теле.
    """
    st = store or WebhookStore(tenant_id)
    sent, failed = 0, 0
    clean_summary = _sanitize_summary(summary or {})
    for raw in st.list(only_active=True):
        ch = WebhookChannel.from_dict(raw)
        if event_type not in (ch.events or []):
            continue
        body_obj = {
            "event": event_type,
            "tenant_id": str(tenant_id),
            "channel_id": ch.id,
            "at": _now(),
            "summary": clean_summary,
            "note": "данные не включены — заберите их через шину: "
                    "пуш только сообщает, что появилось новое",
        }
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "X-Tessbrain-Event": event_type}
        secret = None
        if callable(secret_lookup):
            try:
                secret = secret_lookup(ch.id)
            except Exception:
                secret = None
        if not secret:
            secret = os.getenv("WEBHOOK_SIGNING_KEY") or None
        if secret:
            headers["X-Tessbrain-Signature"] = sign_payload(secret, body)
        ok, err = await _deliver(ch.endpoint_url, body, headers)
        if ok:
            ch.consecutive_failures = 0
            ch.last_delivery_at = _now()
            ch.last_error = ""
            sent += 1
        else:
            ch.consecutive_failures += 1
            ch.last_error = err[:200]
            failed += 1
            if ch.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ch.is_active = False
                logger.warning(
                    "webhook %s отключён после %d подряд ошибок (%s)",
                    ch.id, ch.consecutive_failures, err[:80])
        st.save(ch)
    return {"sent": sent, "failed": failed}


def _sanitize_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """В пуш проходят только числа, булевы и короткие строки-метки.

    Это страховка от соблазна «ну положим сюда и сам текст» — длинные
    строки режутся, вложенные структуры выбрасываются. Единственный путь
    данных наружу — конвейер выдачи.
    """
    out: Dict[str, Any] = {}
    for k, v in list(summary.items())[:12]:
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[str(k)[:40]] = v
        elif isinstance(v, str):
            out[str(k)[:40]] = v[:80]
    return out


async def _deliver(url: str, body: bytes, headers: Dict[str, str]):
    """POST с таймаутом. Возвращает (ok, error_text)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
            r = await client.post(url, content=body, headers=headers)
            if 200 <= r.status_code < 300:
                return True, ""
            return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


__all__ = [
    "KNOWN_EVENTS",
    "MAX_CONSECUTIVE_FAILURES",
    "WebhookChannel",
    "WebhookStore",
    "new_channel",
    "notify",
    "sign_payload",
    "validate_endpoint",
]
