"""Audio provider — VAD, STT, TTS and LiveKit pipeline."""


def is_available() -> bool:
    """Return ``True`` if at least one audio sub-module loaded successfully.

    Overridden by sub-modules at import time when they detect their
    dependencies.
    """
    return False
