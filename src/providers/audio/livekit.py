"""
LiveKit Agent — real-time audio pipeline.

Pipeline:  Mic → VAD → STT → LLM → TTS → Speaker

The agent runs as a separate subprocess using the ``livekit-agents``
framework.
"""

import asyncio
import logging
import os
import subprocess
from typing import Optional

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent script template (embedded to avoid a separate file dependency)
# ---------------------------------------------------------------------------

_AGENT_SCRIPT = r'''"""
LiveKit Agent — launched as a subprocess by LiveKitAgent.
"""
import asyncio
import os

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import deepgram, openai, silero, elevenlabs


class LyraAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            vad=silero.VAD(),
            stt=deepgram.STT(),
            llm=openai.LLM(),
            tts=elevenlabs.TTS(),
        )

    async def on_enter(self) -> None:
        await self.session.say("Bonjour, je suis L.Y.R.A. Comment puis-je vous aider ?")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    session = AgentSession[LyraAgent]()

    @session.on("user_speech_committed")
    def on_speech(agent: LyraAgent, text: str):
        asyncio.ensure_future(agent.session.say(f"Vous avez dit : {text}"))

    await session.start(agent=LyraAgent(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
'''


# ---------------------------------------------------------------------------
# LiveKit Agent wrapper
# ---------------------------------------------------------------------------


class LiveKitAgent:
    """Manages a LiveKit voice pipeline as a separate OS process."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._script_path: str = ""

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self.config.get("LIVEKIT_URL"))

    # ------------------------------------------------------------------
    # Start the pipeline
    # ------------------------------------------------------------------

    async def start_pipeline(self) -> bool:
        """Start the LiveKit agent subprocess.

        Returns ``True`` if the process was started successfully.
        """
        if not self.is_available():
            logger.warning("LiveKitAgent: LIVEKIT_URL not configured")
            return False

        if self._process is not None:
            logger.warning("LiveKitAgent: already running")
            return False

        # Write the embedded agent script to a temp file
        import tempfile
        fd, self._script_path = tempfile.mkstemp(suffix=".py", prefix="lyra_livekit_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_AGENT_SCRIPT)

        env = os.environ.copy()
        env["LIVEKIT_URL"] = str(self.config.get("LIVEKIT_URL", ""))
        env["LIVEKIT_API_KEY"] = str(self.config.get("LIVEKIT_API_KEY", ""))
        env["LIVEKIT_API_SECRET"] = str(self.config.get("LIVEKIT_API_SECRET", ""))
        # Pass through inference keys
        for key in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
            val = self.config.get(key)
            if val:
                env[key] = str(val)

        try:
            self._process = subprocess.Popen(
                ["python", self._script_path],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("LiveKitAgent: pipeline started (PID %d)", self._process.pid)
            return True
        except Exception:
            logger.exception("LiveKitAgent: failed to start pipeline")
            self._process = None
            return False

    # ------------------------------------------------------------------
    # Stop the pipeline
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop the LiveKit agent subprocess and clean up."""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except Exception:
                logger.warning("LiveKitAgent: force kill")
                self._process.kill()
            self._process = None

        if self._script_path and os.path.isfile(self._script_path):
            try:
                os.remove(self._script_path)
            except Exception:
                pass
            self._script_path = ""

        logger.info("LiveKitAgent: stopped")
