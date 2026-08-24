"""W33: рендер + отправка партнёру email-приглашения на shared bundle.

Renderer pure: даёт plaintext + HTML строки. Sender — оборачивает send_email.
"""
from __future__ import annotations

import html as _html
import logging
from typing import Optional

from backend.core.notifications.email import (
    EmailDeliveryResult,
    EmailMessage,
    EmailTransport,
    send_email,
)

logger = logging.getLogger(__name__)


_TEXT_TEMPLATE = """\
Привет.

{owner} ({org_or_email}) поделился(-ась) с тобой материалами в Tessbrain.

Пакет: {note}
Ресурсов: {resource_count}
Доступ открыт до: {expires_at}

Открой ссылку:
{public_url}

Тебе нужно будет ввести этот email чтобы подтвердить личность:
{grantee_email}

Если ссылка уже не нужна — просто проигнорируй это письмо.
Доступ ограничен только указанными ресурсами и автоматически отзывается
по истечении срока. Все просмотры логируются.

— Tessbrain
"""


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="ru">
<head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         color: #1a1a1a; max-width: 560px; margin: 24px auto; padding: 0 16px; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  .meta {{ background: #f4f4f6; padding: 12px 16px; border-radius: 6px;
           font-size: 14px; line-height: 1.6; }}
  .meta b {{ color: #555; font-weight: 500; }}
  .button {{ display: inline-block; background: #0066ff; color: #fff;
             padding: 10px 20px; border-radius: 6px; text-decoration: none;
             font-weight: 500; margin: 16px 0; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 24px; }}
</style></head>
<body>
  <h1>{owner} поделился(-ась) с тобой материалами</h1>
  <p>{org_or_email} открыл(а) доступ к пакету в Tessbrain.</p>
  <div class="meta">
    <div><b>Пакет:</b> {note_html}</div>
    <div><b>Ресурсов:</b> {resource_count}</div>
    <div><b>Доступ до:</b> {expires_at}</div>
  </div>
  <p><a href="{public_url}" class="button">Открыть пакет</a></p>
  <p>Чтобы подтвердить личность, на странице потребуется ввести этот email:
     <code>{grantee_email}</code></p>
  <div class="footer">
    Доступ ограничен указанными ресурсами и автоматически отзывается по истечении срока.
    Все просмотры логируются. Если ссылка не нужна — проигнорируй письмо.
  </div>
</body>
</html>
"""


def render_share_invite(
    *,
    owner_label: str,
    owner_org_or_email: str,
    note: str,
    resource_count: int,
    expires_at: str,
    public_url: str,
    grantee_email: str,
) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body)."""
    note_clean = (note or "").strip() or "(без описания)"
    subject = f"{owner_label} приглашает в Tessbrain: {note_clean[:60]}"
    text = _TEXT_TEMPLATE.format(
        owner=owner_label,
        org_or_email=owner_org_or_email,
        note=note_clean,
        resource_count=resource_count,
        expires_at=expires_at,
        public_url=public_url,
        grantee_email=grantee_email,
    )
    html = _HTML_TEMPLATE.format(
        owner=_html.escape(owner_label),
        org_or_email=_html.escape(owner_org_or_email),
        note_html=_html.escape(note_clean),
        resource_count=resource_count,
        expires_at=_html.escape(expires_at),
        public_url=_html.escape(public_url, quote=True),
        grantee_email=_html.escape(grantee_email),
    )
    return subject, text, html


async def send_share_invite(
    *,
    grantee_email: str,
    owner_label: str,
    owner_org_or_email: str,
    note: str,
    resource_count: int,
    expires_at: str,
    public_url: str,
    transport: Optional[EmailTransport] = None,
) -> EmailDeliveryResult:
    """Best-effort: ошибка не raises — caller получит result.ok=False."""
    if not grantee_email or "@" not in grantee_email:
        return EmailDeliveryResult(
            ok=False, transport="unknown", error="invalid grantee_email",
        )
    if not public_url:
        return EmailDeliveryResult(
            ok=False, transport="unknown", error="public_url required",
        )

    subject, text, html = render_share_invite(
        owner_label=owner_label or "Коллега",
        owner_org_or_email=owner_org_or_email or owner_label or "",
        note=note,
        resource_count=int(resource_count),
        expires_at=expires_at,
        public_url=public_url,
        grantee_email=grantee_email,
    )

    return await send_email(
        EmailMessage(
            to=grantee_email,
            subject=subject,
            text_body=text,
            html_body=html,
        ),
        transport=transport,
    )
