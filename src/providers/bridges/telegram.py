"""
Telegram bridge — sends and receives messages via the Telegram Bot API.
"""

import asyncio
import logging
from typing import Callable, Optional

import httpx

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


class TelegramBridge:
    """Async bridge to a Telegram bot using HTTP long-polling."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._token: Optional[str] = config.get("TELEGRAM_BOT_TOKEN")
        self._api_base = f"https://api.telegram.org/bot{self._token}" if self._token else ""
        self._offset: Optional[int] = None
        self._stop_requested = False
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._token)

    # ------------------------------------------------------------------
    # Client lazy-initialisation
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Send a plain-text message to *chat_id*.

        Returns ``True`` on success, ``False`` on any API error.
        """
        if not self.is_available():
            logger.warning("TelegramBridge: cannot send — no token configured")
            return False

        try:
            client = await self._get_client()
            url = f"{self._api_base}/sendMessage"
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("TelegramBridge: send_message failed")
            return False

    # ------------------------------------------------------------------
    # Get updates (long-polling)
    # ------------------------------------------------------------------

    async def get_updates(self, timeout: int = 10) -> list:
        """Fetch new updates from Telegram (long-poll).

        Returns a list of update dicts (each contains ``message``, etc.).
        """
        if not self.is_available():
            logger.warning("TelegramBridge: cannot poll — no token")
            return []

        try:
            client = await self._get_client()
            url = f"{self._api_base}/getUpdates"
            params: dict = {"timeout": timeout}
            if self._offset is not None:
                params["offset"] = self._offset

            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                logger.error("Telegram API returned ok=False: %s", data)
                return []

            updates = data.get("result", [])
            if updates:
                # Advance offset to acknowledge received updates
                self._offset = updates[-1]["update_id"] + 1

            return updates
        except Exception:
            logger.exception("TelegramBridge: get_updates failed")
            return []

    # ------------------------------------------------------------------
    # Long-polling loop
    # ------------------------------------------------------------------

    async def start_polling(self, handler: Callable) -> None:
        """Start an infinite polling loop.

        For every incoming message the *handler* is called as::

            await handler(chat_id: str, text: str)

        Set :meth:`stop_polling` to exit the loop gracefully.
        """
        self._stop_requested = False
        logger.info("TelegramBridge: polling started")

        while not self._stop_requested:
            updates = await self.get_updates(timeout=10)
            for upd in updates:
                msg = upd.get("message")
                if msg is None:
                    continue

                chat_id = str(msg["chat"]["id"])
                text = msg.get("text", "")
                if not text:
                    continue

                try:
                    await handler(chat_id, text)
                except Exception:
                    logger.exception("TelegramBridge: handler raised")

        logger.info("TelegramBridge: polling stopped")

    # ------------------------------------------------------------------
    # Stop polling
    # ------------------------------------------------------------------

    def stop_polling(self) -> None:
        """Signal the polling loop to stop."""
        self._stop_requested = True
        logger.info("TelegramBridge: stop requested")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
