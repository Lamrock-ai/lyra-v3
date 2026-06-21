"""
Text-to-Speech (TTS).

Provides a factory that tries ElevenLabs API first, then falls back to
local Piper TTS.
"""

import abc
import logging
import os
import subprocess
from typing import Optional

import httpx

from src.kernel.config import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class TTS(abc.ABC):
    """Text-to-Speech interface."""

    @abc.abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Synthesise *text* and return audio bytes (WAV / MP3 / raw PCM)."""
        ...


# ---------------------------------------------------------------------------
# ElevenLabs API
# ---------------------------------------------------------------------------


class ElevenLabsTTS(TTS):
    """TTS via the ElevenLabs HTTP API."""

    DEFAULT_VOICE = "EXAVITQu4vr2nV3VrY5V"  # Rachel

    def __init__(self, config: ConfigManager) -> None:
        self._api_key: Optional[str] = config.get("ELEVENLABS_API_KEY")
        self._voice: str = config.get("ELEVENLABS_VOICE_ID", self.DEFAULT_VOICE)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def synthesize(self, text: str) -> bytes:
        if not self._api_key:
            logger.warning("ElevenLabsTTS: no API key")
            return b""

        try:
            client = await self._get_client()
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice}"
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": self._api_key,
            }
            payload = {
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
            }
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.content
        except Exception:
            logger.exception("ElevenLabsTTS: synthesis failed")
            return b""


# ---------------------------------------------------------------------------
# Piper TTS (local)
# ---------------------------------------------------------------------------


class PiperTTS(TTS):
    """Local TTS using `piper` via subprocess.

    Expects the ``piper`` binary and a voice model on disk.  Configure
    via config keys:

    * ``PIPER_BINARY``  — path to the piper executable (default ``piper``)
    * ``PIPER_MODEL``   — path to the ``.onnx`` voice model
    """

    def __init__(self, config: ConfigManager) -> None:
        self._binary: str = config.get("PIPER_BINARY", "piper")
        self._model: str = config.get("PIPER_MODEL", "")

    async def synthesize(self, text: str) -> bytes:
        if not self._model or not os.path.isfile(self._model):
            logger.warning("PiperTTS: model not found (%s)", self._model)
            return b""

        try:
            proc = subprocess.Popen(
                [self._binary, "--model", self._model, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            out, _ = proc.communicate(input=text.encode("utf-8"), timeout=60)
            return out
        except Exception:
            logger.exception("PiperTTS: synthesis failed")
            return b""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_tts(config: Optional[ConfigManager] = None) -> Optional[TTS]:
    """Return the best available TTS implementation.

    Tries (in order):
      1. ElevenLabsTTS (if API key present)
      2. PiperTTS      (if model file exists)
      3. ``None``
    """
    # 1 – ElevenLabs
    if config is not None:
        api_key = config.get("ELEVENLABS_API_KEY")
        if api_key:
            try:
                tts = ElevenLabsTTS(config)
                logger.info("create_tts → ElevenLabsTTS")
                return tts
            except Exception:
                logger.warning("create_tts: ElevenLabsTTS init failed")

    # 2 – Piper
    if config is not None:
        model_path = config.get("PIPER_MODEL", "")
        if model_path and os.path.isfile(model_path):
            try:
                tts = PiperTTS(config)
                logger.info("create_tts → PiperTTS")
                return tts
            except Exception:
                logger.warning("create_tts: PiperTTS init failed")

    # 3 – None
    logger.warning("create_tts → None (no usable TTS backend)")
    return None
