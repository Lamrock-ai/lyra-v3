"""Orchestrator — central message processing hub."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from src.kernel.eventbus import EventBus
from src.kernel.models import Event
from src.providers.llm.router import LLMRouter, SpeedTag
from src.providers.tools.registry import ToolRegistry
from src.kernel.config import ConfigManager
from .memory import MemoryOrchestrator
from .consolidation import ConsolidationAgent

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(
    r"^(\[I\]|\[CF\]|\[BG\]|\[BG:PROJECT\]|\[voix\]|fast|deep|creative|code|research|agent)[:\s]\s*",
    re.IGNORECASE,
)
WORD_TO_TAG = {
    "fast": "[I]",
    "deep": "[BG:PROJECT]",
    "creative": "[BG]",
    "code": "[CF]",
    "research": "[BG:PROJECT]",
    "agent": "[BG:PROJECT]",
}


class Orchestrator:
    """Central orchestrator: receives messages, routes to LLM, executes tools,
    consolidates memory."""

    def __init__(
        self,
        eventbus: EventBus,
        router: LLMRouter,
        registry: ToolRegistry,
        memory: MemoryOrchestrator,
        config: ConfigManager,
    ) -> None:
        self.eventbus = eventbus
        self.router = router
        self.registry = registry
        self.memory = memory
        self.config = config
        self._system_prompt: Optional[str] = None
        self._consolidation: Optional[ConsolidationAgent] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_message(
        self,
        msg_text: str,
        tag: Optional[SpeedTag] = None,
        channel: str = "cli",
    ) -> str:
        """Process an incoming message end-to-end.

        1. Parse speed tag if not provided.
        2. Strip tag from text.
        3. Load system prompt + context.
        4. Route to the correct LLM backend.
        5. Execute any tool calls requested in the response.
        6. Fire-and-forget memory consolidation.
        """
        try:
            # --- 1 & 2: tag parsing ---
            if tag is None:
                tag, msg_text = self._parse_tag(msg_text)

            cleaned = msg_text.strip()
            if not cleaned:
                return "Je n'ai pas reçu de message à traiter."

            # --- 3: system prompt + context ---
            system = self._load_system_prompt()
            context = await self._build_context(channel)

            full_prompt = f"{system}\n\n{context}\n\n---\n\n{cleaned}"

            # --- 4: LLM call ---
            if not self.router.is_any_available():
                return self._degraded_response(tag)

            response = await self.router.generate(
                tag=tag,
                messages=[{"role": "user", "content": full_prompt}],
            )

            # --- 5: tool execution ---
            content = response.content
            content = await self._execute_tools(content)

            # --- 6: fire-and-forget consolidation ---
            asyncio.ensure_future(self._consolidate(cleaned, content, tag, channel))

            return content

        except Exception:
            logger.exception("Orchestrator.process_message failed")
            return "Désolé, une erreur interne s'est produite pendant le traitement."

    async def start(self) -> None:
        """Subscribe to the event bus for incoming messages."""
        self.eventbus.on("message.incoming", self._on_incoming_message)
        logger.info("Orchestrator started — listening on 'message.incoming'")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _on_incoming_message(self, event: Event) -> None:
        """Event bus handler."""
        msg = event.payload.get("text", "")
        tag = event.payload.get("tag")
        channel = event.payload.get("channel", "cli")
        response = await self.process_message(msg, tag=tag, channel=channel)
        await self.eventbus.emit("message.response", {"text": response, "channel": channel})

    def _parse_tag(self, text: str) -> tuple[SpeedTag, str]:
        """Extract a speed tag from the beginning of the message."""
        m = TAG_PATTERN.match(text)
        if m:
            raw = m.group(1)
            # Map word aliases to bracket tags
            mapped = WORD_TO_TAG.get(raw.lower(), raw)
            tag = SpeedTag(mapped) if mapped in SpeedTag._value2member_map_ else SpeedTag.INSTANT
            return tag, text[m.end():]
        return SpeedTag.INSTANT, text

    def _load_system_prompt(self) -> str:
        """Read the system prompt from disk (cached)."""
        if self._system_prompt is not None:
            return self._system_prompt
        try:
            path = self.config.get("prompts.system_path", "prompts/system.md")
            with open(path, encoding="utf-8") as f:
                self._system_prompt = f.read()
        except (FileNotFoundError, OSError):
            logger.warning("System prompt file not found, using fallback.")
            self._system_prompt = "Tu es L.Y.R.A, un assistant IA autonome et proactif."
        return self._system_prompt

    async def _build_context(self, channel: str) -> str:
        """Gather context snippets (available projects, notes, etc.)."""
        parts = [f"Canal d'entrée : {channel}"]

        try:
            projects = await self.memory.search_facts("sujet:projet")
            if projects:
                parts.append("Projets connus : " + ", ".join(p["objet"] for p in projects[:5]))
        except Exception:
            pass

        try:
            recent = await self.memory.get_recent_events(3)
            if recent:
                context_str = " | ".join(f"{e['event_type']}: {e['payload'][:80]}"
                                         for e in recent)
                parts.append(f"Événements récents : {context_str}")
        except Exception:
            pass

        return "\n".join(parts)

    async def _execute_tools(self, response: str) -> str:
        """If the LLM response contains tool invocations, execute them."""
        import json

        tool_pattern = re.compile(
            r"```tool\s*\n(.*?)\n```", re.DOTALL
        )

        async def _try_exec(match) -> str:
            block = match.group(1).strip()
            try:
                request = json.loads(block)
            except json.JSONDecodeError:
                return match.group(0)

            tool_name = request.get("tool")
            params = request.get("params", {})

            if not tool_name or tool_name not in self.registry:
                return f"? Outil inconnu : {tool_name}"

            result = await self.registry.execute_tool(tool_name, params)
            if result.success:
                return f"**{tool_name}** : {result.output}"
            return f"? Erreur {tool_name} : {result.error}"

        # Apply async sub to sync string — process matches sequentially
        matches = list(tool_pattern.finditer(response))
        for m in matches:
            replacement = await _try_exec(m)
            response = response.replace(m.group(0), replacement, 1)
        return response

    async def _consolidate(self, msg: str, response: str, tag: SpeedTag, channel: str) -> None:
        """Fire-and-forget memory consolidation."""
        try:
            metadata = {"tag": tag.value, "channel": channel}
            await self.memory.consolidate(f"{msg}\n{response}", metadata)
        except Exception:
            logger.exception("Background consolidation failed")

    def _degraded_response(self, tag: SpeedTag) -> str:
        """Return a helpful message when no LLM backend is available."""
        return (
            f"Aucun modèle LLM disponible pour le tag '{tag.value}'. "
            "Vérifie la configuration des providers dans config.yaml "
            "ou la disponibilité des endpoints."
        )


