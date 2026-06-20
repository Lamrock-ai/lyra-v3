"""
Speech-to-Text (STT).

Provides a factory that tries Deepgram API first, then falls back to
local Whisper (faster-whisper).
"""

import abc
import logging
import os
from typing import Optional

import httpx

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class STT(abc.ABC):
    """Speech-to-Text interface."""

    @abc.abstractmethod
    async def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file and return the text."""
        ...


# ---------------------------------------------------------------------------
# Deepgram API
# ---------------------------------------------------------------------------


class DeepgramSTT(STT):
    """STT via Deepgram's HTTP API."""

    def __init__(self, config: ConfigManager) -> None:
        self._api_key: Optional[str] = config.get("DEEPGRAM_API_KEY")
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def transcribe(self, audio_path: str) -> str:
        if not self._api_key:
            logger.warning("DeepgramSTT: no API key")
            return ""

        if not os.path.isfile(audio_path):
            logger.warning("DeepgramSTT: file not found — %s", audio_path)
            return ""

        try:
            client = await self._get_client()
            url = "https://api.deepgram.com/v1/listen"
            headers = {"Authorization": f"Token {self._api_key}"}

            with open(audio_path, "rb") as fh:
                resp = await client.post(url, headers=headers, content=fh)

            resp.raise_for_status()
            data = resp.json()
            # Deepgram response shape
            transcript = (
                data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
            )
            return transcript.strip()

        except Exception:
            logger.exception("DeepgramSTT: transcription failed")
            return ""


# ---------------------------------------------------------------------------
# Whisper local (faster-whisper)
# ---------------------------------------------------------------------------


class WhisperSTT(STT):
    """Local STT using faster-whisper with the ``tiny`` model."""

    def __init__(self) -> None:
        self._model = None

    async def _load_model(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]

            self._model = WhisperModel("tiny", device="cpu", compute_type="int8")
            logger.info("WhisperSTT: model loaded (tiny, int8)")
        except ImportError:
            raise ImportError(
                "faster-whisper is not installed. "
                "Install with: pip install faster-whisper"
            )

    async def transcribe(self, audio_path: str) -> str:
        if not os.path.isfile(audio_path):
            logger.warning("WhisperSTT: file not found — %s", audio_path)
            return ""

        try:
            await self._load_model()
            segments, _ = self._model.transcribe(audio_path)  # type: ignore[union-attr]
            text = " ".join(seg.text for seg in segments)
            return text.strip()
        except Exception:
            logger.exception("WhisperSTT: transcription failed")
            return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_stt(config: Optional[ConfigManager] = None) -> Optional[STT]:
    """Return the best available STT implementation.

    Tries (in order):
      1. DeepgramSTT (if API key present)
      2. WhisperSTT  (if faster-whisper installed)
      3. ``None``
    """
    # 1 – Deepgram
    if config is not None:
        api_key = config.get("DEEPGRAM_API_KEY")
        if api_key:
            try:
                stt = DeepgramSTT(config)
                logger.info("create_stt → DeepgramSTT")
                return stt
            except Exception:
                logger.warning("create_stt: DeepgramSTT init failed")

    # 2 – Whisper
    try:
        stt = WhisperSTT()
        logger.info("create_stt → WhisperSTT")
        return stt
    except Exception:
        logger.warning("create_stt: WhisperSTT init failed")

    # 3 – None
    logger.warning("create_stt → None (no usable STT backend)")
    return None
