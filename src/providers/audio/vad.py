"""
Voice Activity Detection (VAD).

Provides a factory that tries Silero VAD first and falls back to a
simple energy-based detector.
"""

import abc
import logging
import math
import struct

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class VAD(abc.ABC):
    """Voice Activity Detection interface."""

    @abc.abstractmethod
    async def is_speech(self, audio_chunk: bytes) -> float:
        """Return a confidence score (0 … 1) that *audio_chunk* contains speech."""
        ...


# ---------------------------------------------------------------------------
# Silero VAD (GPU / ONNX — heavy dependencies)
# ---------------------------------------------------------------------------


class SileroVAD(VAD):
    """VAD based on Silero (ONNX runtime)."""

    def __init__(self) -> None:
        self._model = None  # lazy import


    async def _load_model(self) -> None:
        if self._model is not None:
            return
        try:
            import silero_vad  # type: ignore[import-untyped]
            self._model = silero_vad.load_silero_vad()
            logger.info("SileroVAD: model loaded")
        except ImportError:
            raise ImportError(
                "silero_vad is not installed. "
                "Install with: pip install silero-vad"
            )

    async def is_speech(self, audio_chunk: bytes) -> float:
        try:
            await self._load_model()
            # The actual silero_vad API expects a numpy array, float32, 16 kHz.
            # Convert bytes → numpy array.
            import numpy as np

            audio_array = (
                np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
            )
            speech_prob = self._model(audio_array)  # type: ignore[union-attr]
            return float(speech_prob)
        except Exception:
            logger.exception("SileroVAD: inference failed")
            return 0.0


# ---------------------------------------------------------------------------
# Energy-based VAD (lightweight fallback)
# ---------------------------------------------------------------------------


class EnergyVAD(VAD):
    """Simple energy-based VAD using RMS of the audio signal.

    Works on 16-bit PCM mono audio.  Adjust ``threshold`` to tune
    sensitivity.
    """

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    async def is_speech(self, audio_chunk: bytes) -> float:
        try:
            # Unpack 16-bit signed samples
            sample_count = len(audio_chunk) // 2
            fmt = "<" + "h" * sample_count
            samples = struct.unpack(fmt, audio_chunk[: sample_count * 2])

            # RMS
            sum_sq = sum(s * s for s in samples)
            rms = math.sqrt(sum_sq / max(len(samples), 1)) / 32768.0

            confidence = min(rms / self.threshold, 1.0)
            return confidence
        except Exception:
            logger.exception("EnergyVAD: failed")
            return 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_vad() -> VAD:
    """Return the best available VAD implementation.

    Tries (in order):
      1. SileroVAD
      2. EnergyVAD (always works)
    """
    try:
        vad = SileroVAD()
        # Trigger model load to verify it works
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(vad.is_speech(b"\x00" * 320))
        loop.close()
        logger.info("create_vad → SileroVAD")
        return vad
    except Exception:
        logger.info("create_vad → EnergyVAD (fallback)")
        return EnergyVAD()
