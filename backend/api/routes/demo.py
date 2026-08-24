import os

import httpx
import structlog
from litestar import Router, post
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

# --- Telegram Config ---
# Ensure these are set in your .env file
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

class DemoRequest(BaseModel):
    name: str
    company: str
    contact: str  # Email or Phone

class DemoResponse(BaseModel):
    success: bool
    message: str

async def send_telegram_message(message: str):
    """Sends a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram bot token or chat ID not configured. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("Telegram notification sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

@post("/request-demo")
async def request_demo(data: DemoRequest) -> DemoResponse:
    """
    Handles demo requests from the landing page and sends a notification to Telegram.
    """
    logger.info(f"Received demo request from {data.name} ({data.company})")

    message = (
        f"🚀 **New Demo Request!**\n\n"
        f"👤 **Name:** {data.name}\n"
        f"🏢 **Company:** {data.company}\n"
        f"📞 **Contact:** {data.contact}\n"
        f"\n"
        f"Please follow up with this lead ASAP."
    )

    await send_telegram_message(message)

    return DemoResponse(
        success=True,
        message="Request received. We will contact you shortly."
    )

router = Router(
    path="/demo",
    route_handlers=[request_demo],
    tags=["Public"],
)


