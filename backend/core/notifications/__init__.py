"""W33: outbound email transports + share invite delivery."""
from backend.core.notifications.email import (
    EmailDeliveryResult,
    EmailMessage,
    EmailTransport,
    NoopTransport,
    SendGridTransport,
    SMTPTransport,
    build_default_transport,
    send_email,
)
from backend.core.notifications.share_invite import (
    render_share_invite,
    send_share_invite,
)

__all__ = [
    "EmailDeliveryResult",
    "EmailMessage",
    "EmailTransport",
    "NoopTransport",
    "SMTPTransport",
    "SendGridTransport",
    "build_default_transport",
    "render_share_invite",
    "send_email",
    "send_share_invite",
]
