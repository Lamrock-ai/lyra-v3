"""
Telegram bot interface for L.Y.R.A v3.
Relies on a TelegramBridge for the actual polling/sending.
"""

import logging

from src.kernel.models import Event, Priority

log = logging.getLogger(__name__)


class TelegramBot:
    """Interface Telegram qui pont via TelegramBridge."""

    def __init__(self, bridge, orchestrator):
        """
        Args:
            bridge: Instance de TelegramBridge (gère l'API Telegram).
            orchestrator: Orchestrateur principal.
        """
        self.bridge = bridge
        self.orchestrator = orchestrator
        self._running = False

    async def run(self) -> None:
        """Lance le polling via le bridge."""
        self._running = True
        log.info("TelegramBot demarre via bridge=%s", self.bridge.__class__.__name__)

        try:
            await self.bridge.start_polling(self._handle_message)
        except Exception:
            log.exception("TelegramBot polling error")
        finally:
            self._running = False

    async def _handle_message(self, chat_id: str, text: str) -> None:
        """Callback interne : reçoit un message du bridge et le traite."""
        log.debug("Message Telegram de %s: %s", chat_id, text[:80])

        # Publish event on bus
        event = Event(
            type="message.incoming",
            payload={
                "chat_id": chat_id,
                "text": text,
                "channel": "telegram",
            },
            priority=Priority.MEDIUM,
            source="telegram",
        )

        try:
            response = await self.orchestrator.process_message(
                text, channel="telegram"
            )
            reply = str(response) if response else "Je n'ai pas de réponse à te donner."
        except Exception as exc:
            log.exception("Erreur dans le traitement du message Telegram")
            reply = f"Désolé, une erreur s'est produite : {exc}"

        # Envoie la réponse via le bridge
        await self.bridge.send_message(chat_id, reply)
