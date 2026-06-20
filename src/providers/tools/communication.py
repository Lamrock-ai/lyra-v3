"""L.Y.R.A v3 — Communication tools.

Optional integrations (Telegram, email, …) that gracefully degrade
when their API keys are not configured.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from src.kernel.config import ConfigManager
from src.providers.tools.registry import ApprovalLevel, Tool, ToolRegistry

logger = logging.getLogger("lyra.providers.tools.communication")

HTTP_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Telegram (optional)
# ---------------------------------------------------------------------------

_TELEGRAM_BASE = "https://api.telegram.org/bot{token}"


async def telegram_send(chat_id: str, text: str) -> str:
    """Send a message via Telegram bot API."""
    cfg = ConfigManager()
    token = cfg.get_or_none("TELEGRAM_BOT_TOKEN")
    if not token:
        return "Telegram bot token not configured. Set TELEGRAM_BOT_TOKEN in .env"

    url = f"{_TELEGRAM_BASE.format(token=token)}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            logger.info("Telegram message sent to %s", chat_id)
            return f"Message sent to {chat_id}"
        return f"Telegram API error HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return f"Telegram send failed: {exc}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

async def register(registry: ToolRegistry) -> None:
    """Register communication tools into *registry*."""
    registry.register(Tool(
        name="telegram_send",
        description="Send a message via Telegram to a specified chat ID.",
        handler=telegram_send,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "Telegram chat ID (numeric string, e.g. '-1001234567890')",
                },
                "text": {
                    "type": "string",
                    "description": "Message text (Markdown allowed)",
                },
            },
            "required": ["chat_id", "text"],
        },
        category="communication",
    ))

    logger.info("Communication tools registered")
