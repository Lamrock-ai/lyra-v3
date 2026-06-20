"""
Telegram bot interface for L.Y.R.A v3.
Relies on a TelegramBridge for the actual polling/sending.
"""

import logging
from src.kernel.event import Event

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

        # Enregistre le handler sur le bridge
        self.bridge.on_message(self._handle_message)

        try:
            await self.bridge.start_polling()
        except Exception:
            log.exception("TelegramBot polling error")
        finally:
            self._running = False

    async def _handle_message(self, chat_id: int, text: str,
                              message_id: int = None) -> None:
        """Callback interne : reçoit un message du bridge et le traite."""
        log.debug("Message Telegram de %s: %s", chat_id, text[:80])

        # Crée un événement
        event = Event(
            source="telegram",
            payload={
                "chat_id": chat_id,
                "text": text,
                "message_id": message_id,
            },
        )

        try:
            response = await self.orchestrator.process_message(text, event=event)
            reply = str(response) if response else "Je n'ai pas de réponse à te donner."
        except Exception as exc:
            log.exception("Erreur dans le traitement du message Telegram")
            reply = f"Désolé, une erreur s'est produite : {exc}"

        # Envoie la réponse via le bridge
        await self.bridge.send_message(chat_id, reply)
