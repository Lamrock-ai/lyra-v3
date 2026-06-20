"""L.Y.R.A v3 — LLM provider base classes and data models."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("lyra.providers.llm")


class ProviderCapability(str, Enum):
    """Capabilities a provider can advertise."""
    FAST = "fast"
    DEEP = "deep"
    LOCAL = "local"
    CHEAP = "cheap"


class ProviderUnavailable(Exception):
    """Raised when a provider cannot fulfil a request (offline / missing key)."""
    pass


@dataclass
class LLMResponse:
    """Standardised response from any LLM provider."""
    content: str
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    latency_ms: float = 0.0
    provider: str = ""


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    name: str = "base"
    capabilities: list[ProviderCapability] = []

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a completion request and return the response.

        Args:
            messages: Conversation history [{"role": "user", "content": "..."}, ...]
            system: Optional system prompt.
            tools: Optional tool definitions (OpenAI tool-call format).
            stream: If True, parse SSE stream (still returns full LLMResponse).
            temperature: Sampling temperature.

        Returns:
            LLMResponse with generated content and metadata.

        Raises:
            ProviderUnavailable: If the provider can not handle the request.
        """
        ...

    def is_available(self) -> bool:
        """Quick synchronous check (key present, service reachable)."""
        return True

    def get_models(self) -> list[str]:
        """Return a list of supported model identifiers."""
        return []
