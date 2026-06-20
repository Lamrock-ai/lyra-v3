"""L.Y.R.A v3 — Configuration manager.

Thread-safe singleton that loads environment variables and
provides helpers to check which external providers are available.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigManager:
    """Thread-safe singleton configuration manager.

    Usage::

        cfg = ConfigManager()
        api_key = cfg.get("GROQ_API_KEY")
        if cfg.is_provider_available("groq"):
            ...
    """

    _instance: Optional[ConfigManager] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj.__init__()
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        """Initialise (idempotent after first call thanks to singleton)."""
        if hasattr(self, "_initialised") and self._initialised:
            return
        self._data: dict[str, str] = {}
        self._env_path: Optional[Path] = None
        self._load_env()
        self._initialised = True

    # ── loading ────────────────────────────────────────────────────────────────

    def _load_env(self) -> None:
        """Search for a .env file and load it."""
        candidates = [
            Path.cwd() / ".env",
            Path.cwd() / "config" / ".env",
            Path(__file__).resolve().parent.parent.parent / ".env",
        ]
        for candidate in candidates:
            if candidate.exists():
                self._env_path = candidate
                load_dotenv(candidate, override=True)
                logger.info("Loaded environment from %s", candidate)
                break
        else:
            logger.warning("No .env file found — relying on system environment.")

        # Snapshot current env so get/get_all work without re-reading os.environ
        self._data = dict(os.environ)

    # ── public API ─────────────────────────────────────────────────────────────

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return the value for *key* or *default* if absent."""
        return self._data.get(key, default)

    def get_or_none(self, key: str) -> Optional[str]:
        """Return the value for *key* or ``None``."""
        return self._data.get(key)

    def get_all(self) -> dict[str, str]:
        """Return a copy of the entire environment snapshot."""
        return dict(self._data)

    def reload(self) -> None:
        """Re-load the .env file and refresh the internal snapshot."""
        self._load_env()

    def available_providers(self) -> dict[str, bool]:
        """Return a dict mapping every known provider to its availability."""
        return {
            "groq": self.is_provider_available("groq"),
            "ollama": self.is_provider_available("ollama"),
            "anthropic": self.is_provider_available("anthropic"),
            "openai": self.is_provider_available("openai"),
            "telegram": self.is_provider_available("telegram"),
            "obsidian": self.is_provider_available("obsidian"),
        }

    def is_provider_available(self, name: str) -> bool:
        """Check whether a given external provider is available.

        Supported providers:
          - ``groq``       → GROQ_API_KEY must be set
          - ``ollama``     → always True (try ping local Ollama instance)
          - ``anthropic``  → ANTHROPIC_API_KEY must be set
          - ``openai``     → OPENAI_API_KEY must be set
          - ``telegram``   → TELEGRAM_BOT_TOKEN must be set
          - ``obsidian``   → OBSIDIAN_VAULT_PATH must be set
        """
        name = name.lower().strip()

        if name == "groq":
            return bool(self._data.get("GROQ_API_KEY"))
        if name == "ollama":
            return self._ping_ollama()
        if name == "anthropic":
            return bool(self._data.get("ANTHROPIC_API_KEY"))
        if name == "openai":
            return bool(self._data.get("OPENAI_API_KEY"))
        if name == "telegram":
            return bool(self._data.get("TELEGRAM_BOT_TOKEN"))
        if name == "obsidian":
            return bool(self._data.get("OBSIDIAN_VAULT_PATH"))

        logger.warning("Unknown provider '%s' — returning False", name)
        return False

    @staticmethod
    def _ping_ollama() -> bool:
        """Quick health-check for a local Ollama instance (HTTP ping)."""
        import http.client
        import socket

        try:
            conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2)
            conn.request("GET", "/")
            resp = conn.getresponse()
            conn.close()
            return resp.status < 500  # any response means alive
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False
