"""W33: email transports — SMTP / SendGrid / Noop.

Дизайн:
- `EmailMessage` — простой dataclass (to / from / subject / text / html)
- `EmailTransport` Protocol с одним методом `send(message) -> EmailDeliveryResult`
- 3 реализации:
  * `SMTPTransport` — stdlib smtplib (поддерживает STARTTLS / SSL)
  * `SendGridTransport` — REST API через httpx (без python-sendgrid SDK)
  * `NoopTransport` — для dev / тестов: записывает messages в memory
- Best-effort: `send_email` никогда не raises, возвращает result с
  ok=False при failure
- `build_default_transport()` — выбирает реализацию по env:
  * SENDGRID_API_KEY → SendGrid
  * SMTP_HOST → SMTP
  * иначе → Noop (с warning'ом)
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as StdEmailMessage
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    text_body: str
    html_body: Optional[str] = None
    from_addr: Optional[str] = None        # если None — берётся из transport defaults
    reply_to: Optional[str] = None
    # Вложения: [{"filename", "content_b64", "mime"}]. Пусто → письмо без файлов.
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def normalized_to(self) -> str:
        return (self.to or "").strip().lower()

    def validate(self) -> None:
        if "@" not in self.normalized_to():
            raise ValueError(f"invalid recipient email: {self.to!r}")
        if not self.subject.strip():
            raise ValueError("subject required")
        if not self.text_body.strip():
            raise ValueError("text_body required")


@dataclass
class EmailDeliveryResult:
    ok: bool
    transport: str
    message_id: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EmailTransport(Protocol):
    name: str

    async def send(self, message: EmailMessage) -> EmailDeliveryResult: ...


# === SMTP transport ====================================================


class SMTPTransport:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        from_addr: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.use_tls = use_tls and not use_ssl
        self.use_ssl = use_ssl
        self.from_addr = from_addr
        self.timeout = float(timeout)

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        try:
            message.validate()
        except ValueError as exc:
            return EmailDeliveryResult(ok=False, transport=self.name, error=str(exc))

        sender = message.from_addr or self.from_addr
        if not sender:
            return EmailDeliveryResult(
                ok=False, transport=self.name,
                error="from_addr required (set SMTP_FROM or pass per-message)",
            )

        msg = StdEmailMessage()
        msg["From"] = sender
        msg["To"] = message.normalized_to()
        msg["Subject"] = message.subject
        if message.reply_to:
            msg["Reply-To"] = message.reply_to
        msg.set_content(message.text_body)
        if message.html_body:
            msg.add_alternative(message.html_body, subtype="html")
        for att in (message.attachments or []):
            try:
                import base64 as _b64
                raw = _b64.b64decode(str(att.get("content_b64") or ""))
                mime = str(att.get("mime") or "application/octet-stream")
                maintype, _, subtype = mime.partition("/")
                msg.add_attachment(raw, maintype=maintype or "application",
                                   subtype=subtype or "octet-stream",
                                   filename=str(att.get("filename") or "file"))
            except Exception:
                logger.debug("smtp attachment skipped", exc_info=True)

        # smtplib — sync, оборачиваем в run_in_executor чтобы не блокировать loop.
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._send_sync, msg, sender, message)
        except Exception as exc:
            logger.warning("SMTPTransport failed: %s", exc)
            return EmailDeliveryResult(
                ok=False, transport=self.name, error=str(exc),
            )
        return EmailDeliveryResult(
            ok=True,
            transport=self.name,
            message_id=msg.get("Message-Id"),
            metadata={"host": self.host, "port": self.port},
        )

    def _send_sync(
        self, msg: StdEmailMessage, sender: str, message: EmailMessage,
    ) -> None:
        if self.use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx) as s:
                if self.username:
                    s.login(self.username, self.password or "")
                s.send_message(msg, from_addr=sender, to_addrs=[message.normalized_to()])
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                s.ehlo()
                if self.use_tls:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if self.username:
                    s.login(self.username, self.password or "")
                s.send_message(msg, from_addr=sender, to_addrs=[message.normalized_to()])


# === SendGrid transport ================================================


class SendGridTransport:
    name = "sendgrid"

    def __init__(
        self,
        *,
        api_key: str,
        from_addr: str,
        from_name: str = "",
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("SendGrid api_key required")
        if not from_addr:
            raise ValueError("from_addr required")
        self.api_key = api_key
        self.from_addr = from_addr
        self.from_name = from_name
        self.timeout = float(timeout)

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        try:
            message.validate()
        except ValueError as exc:
            return EmailDeliveryResult(ok=False, transport=self.name, error=str(exc))

        try:
            import httpx
        except ImportError as exc:
            return EmailDeliveryResult(
                ok=False, transport=self.name,
                error=f"httpx not installed: {exc}",
            )

        sender = {"email": message.from_addr or self.from_addr}
        if self.from_name:
            sender["name"] = self.from_name
        content = [{"type": "text/plain", "value": message.text_body}]
        if message.html_body:
            content.append({"type": "text/html", "value": message.html_body})

        payload = {
            "personalizations": [{"to": [{"email": message.normalized_to()}]}],
            "from": sender,
            "subject": message.subject,
            "content": content,
        }
        if message.reply_to:
            payload["reply_to"] = {"email": message.reply_to}
        if message.attachments:
            payload["attachments"] = [{
                "content": str(a.get("content_b64") or ""),
                "filename": str(a.get("filename") or "file"),
                "type": str(a.get("mime") or "application/octet-stream"),
                "disposition": "attachment",
            } for a in message.attachments if a.get("content_b64")]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            logger.warning("SendGridTransport failed: %s", exc)
            return EmailDeliveryResult(
                ok=False, transport=self.name, error=str(exc),
            )
        if response.status_code >= 400:
            return EmailDeliveryResult(
                ok=False, transport=self.name,
                error=f"sendgrid http {response.status_code}: {response.text[:200]}",
            )
        return EmailDeliveryResult(
            ok=True,
            transport=self.name,
            message_id=response.headers.get("X-Message-Id"),
            metadata={"status_code": response.status_code},
        )


# === Noop transport (dev/tests) =========================================


class NoopTransport:
    name = "noop"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        try:
            message.validate()
        except ValueError as exc:
            return EmailDeliveryResult(ok=False, transport=self.name, error=str(exc))
        self.sent.append(message)
        logger.info(
            "NoopTransport: would send to=%s subject=%r (set SMTP_HOST or SENDGRID_API_KEY for real delivery)",
            message.normalized_to(), message.subject,
        )
        return EmailDeliveryResult(
            ok=True, transport=self.name,
            metadata={"queued_count": len(self.sent)},
        )


# === Builder ============================================================


def build_default_transport() -> EmailTransport:
    """Выбор transport'а по environment.

    Приоритет: SendGrid → SMTP → Noop (с warning).
    """
    try:
        from backend.config import get_settings
        s = get_settings()
    except Exception as exc:
        logger.debug("settings unavailable, falling back to Noop: %s", exc)
        return NoopTransport()

    sg_key = (getattr(s, "sendgrid_api_key", "") or "").strip()
    if sg_key:
        from_addr = (getattr(s, "email_from_addr", "") or "").strip()
        from_name = (getattr(s, "email_from_name", "") or "").strip()
        if not from_addr:
            logger.warning("SENDGRID_API_KEY set but EMAIL_FROM_ADDR empty — using noop")
            return NoopTransport()
        return SendGridTransport(api_key=sg_key, from_addr=from_addr, from_name=from_name)

    smtp_host = (getattr(s, "smtp_host", "") or "").strip()
    if smtp_host:
        from_addr = (getattr(s, "email_from_addr", "") or "").strip()
        if not from_addr:
            logger.warning("SMTP_HOST set but EMAIL_FROM_ADDR empty — using noop")
            return NoopTransport()
        return SMTPTransport(
            host=smtp_host,
            port=int(getattr(s, "smtp_port", 587)),
            username=(getattr(s, "smtp_username", "") or None),
            password=(getattr(s, "smtp_password", "") or None),
            use_tls=bool(getattr(s, "smtp_use_tls", True)),
            use_ssl=bool(getattr(s, "smtp_use_ssl", False)),
            from_addr=from_addr,
        )

    logger.info(
        "No email transport configured (SENDGRID_API_KEY / SMTP_HOST empty); "
        "using NoopTransport — emails will be logged but not delivered"
    )
    return NoopTransport()


async def send_email(
    message: EmailMessage,
    *,
    transport: Optional[EmailTransport] = None,
) -> EmailDeliveryResult:
    """Best-effort send. Caller передаёт transport или используется default."""
    t = transport or build_default_transport()
    try:
        return await t.send(message)
    except Exception as exc:
        logger.warning("send_email unexpected error: %s", exc)
        return EmailDeliveryResult(
            ok=False, transport=getattr(t, "name", "unknown"), error=str(exc),
        )
