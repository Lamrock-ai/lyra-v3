"""
LiveKit agent interface — voice pipeline as a subprocess.

Requires: livekit>=1.5, livekit-agents>=0.8 (optional).
"""

import asyncio
import logging

log = logging.getLogger(__name__)

try:
    from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli
    from livekit.agents.voice import Agent, AgentSession

    HAS_LIVEKIT = True
except ImportError:
    HAS_LIVEKIT = False
    log.info("LiveKit not installed — LiveKitAgent unavailable.")


class LiveKitAgent:
    """Voice pipeline: Mic → VAD → STT → LLM → TTS → Speaker.

    Runs as a separate asyncio process. Gracefully degrades if
    LiveKit dependencies are missing.
    """

    def __init__(self, config) -> None:
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False

    def is_available(self) -> bool:
        return HAS_LIVEKIT

    async def run(self) -> None:
        """Start the LiveKit agent pipeline."""
        if not HAS_LIVEKIT:
            log.warning("LiveKitAgent: dependencies not installed, cannot start.")
            return

        log.info("LiveKitAgent starting...")
        self._running = True

        try:
            await cli.run_app(
                WorkerOptions(
                    entrypoint_fnc=self._entrypoint,
                    agent_name="lyra-v3",
                )
            )
        except Exception:
            log.exception("LiveKitAgent failed")

    async def stop(self) -> None:
        """Signal shutdown."""
        self._running = False
        log.info("LiveKitAgent stopping.")

    async def _entrypoint(self, ctx: "JobContext") -> None:
        """LiveKit entrypoint: called when a new room session starts."""
        log.info("LiveKitAgent connected to room %s", ctx.room.name)

        session = AgentSession[Agent](
            ctx=ctx,
            auto_subscribe=AutoSubscribe.AUDIO_ONLY,
        )

        await session.start()

        log.info("LiveKitAgent session ended for %s", ctx.room.name)
