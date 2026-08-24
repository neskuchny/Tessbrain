"""Bridge — user→user транспорт внутри Tessbrain (P2 §5).

Даёт Mini Tess (EPIC D) маршрут «сообщение от одного пользователя другому»
через durable outbox/inbox + опциональный push в reactive signal_queue
(→ drain → Telegram). См. docs/TESSENT_ANSWERS.md §P2.
"""
from backend.core.bridge.store import (
    BridgeMessage,
    list_inbox,
    list_outbox,
    mark_read,
    send_message,
)

__all__ = [
    "BridgeMessage",
    "list_inbox",
    "list_outbox",
    "mark_read",
    "send_message",
]
