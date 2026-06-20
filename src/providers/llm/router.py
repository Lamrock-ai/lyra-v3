"""L.Y.R.A v3 — LLM router (singleton) and SpeedRouter.

Routes messages to the appropriate provider based on SpeedTag:
  - [I]           → fastest available (Groq > Ollama)
  - [CF]          → fast or deep depending on tool complexity
  - [BG]          → fast (acknowledgement only)
  - [BG:PROJECT]  → deep (planning)
  - [voix]        → fast or deep depending on context
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.kernel.models import SpeedTag
from .base import LLMProvider, LLMResponse, ProviderUnavailable
from .providers import (
    AnthropicProvider,
    GroqProvider,
    OllamaProvider,
    OpenAIProvider,
    get_available_providers,
)

logger = logging.getLogger("lyra.providers.llm")


class LLMRouter:
    """Singleton router that selects the best provider by SpeedTag.

    Usage::

        router = LLMRouter()
        resp = await router.generate(SpeedTag.INSTANT, [{"role": "user", "content": "hi"}])
    """

    _instance: Optional[LLMRouter] = None

    def __new__(cls) -> LLMRouter:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialised") and self._initialised:
            return
        self._groq = GroqProvider()
        self._ollama = OllamaProvider()
        self._anthropic = AnthropicProvider()
        self._openai = OpenAIProvider()
        self._all_available: list[LLMProvider] = []
        self._refresh_available()
        self._initialised = True

    def _refresh_available(self) -> None:
        self._all_available = get_available_providers()

    # ── provider selection ──────────────────────────────────────────────────────

    def get_fast(self) -> Optional[LLMProvider]:
        """Return the fastest available provider (Groq > Ollama)."""
        for provider in [self._groq, self._ollama]:
            if provider.is_available():
                return provider
        return None

    def get_deep(self) -> Optional[LLMProvider]:
        """Return the deepest available provider (Anthropic > OpenAI > Groq > Ollama)."""
        for provider in [self._anthropic, self._openai, self._groq, self._ollama]:
            if provider.is_available():
                return provider
        return None

    def get_local(self) -> Optional[LLMProvider]:
        """Return the local (Ollama) provider if available."""
        return self._ollama if self._ollama.is_available() else None

    # ── generation ──────────────────────────────────────────────────────────────

    async def generate(
        self,
        tag: SpeedTag,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate a response using the provider best suited for *tag*.

        Args:
            tag: One of SpeedTag values.
            messages: Conversation history.
            system: Optional system prompt.
            tools: Optional tool definitions.
            **kwargs: Passed through to the underlying provider.

        Returns:
            LLMResponse with generated content.

        Raises:
            ProviderUnavailable: If no provider can handle the request.
        """
        provider: Optional[LLMProvider] = None

        if tag in (SpeedTag.INSTANT, SpeedTag.VOIX):
            provider = self.get_fast()
            kwargs.setdefault("max_tokens", 512)
            tools = None  # no tool calls on instant replies
        elif tag == SpeedTag.CONFIRM_FIRE:
            # fast for simple tools, deep for critical decisions
            if tools and self._has_critical_tools(tools):
                provider = self.get_deep()
            else:
                provider = self.get_fast()
        elif tag == SpeedTag.BACKGROUND:
            provider = self.get_fast()
            kwargs.setdefault("max_tokens", 256)
            tools = None
        elif tag == SpeedTag.AUTONOME:
            provider = self.get_deep()
        else:
            provider = self.get_fast()

        if provider is None:
            # Try any available provider as last resort
            if self._all_available:
                provider = self._all_available[0]
            else:
                raise ProviderUnavailable(
                    "No LLM provider available — check your API keys and network."
                )

        # Attempt with chosen provider; fallback chain on failure
        fallback_chain = self._build_fallback_chain(provider)
        last_error: Optional[Exception] = None

        for prov in fallback_chain:
            try:
                logger.debug("Routing %s → %s", tag.value, prov.name)
                return await prov.generate(
                    messages=messages,
                    system=system,
                    tools=tools,
                    **kwargs,
                )
            except (ProviderUnavailable, Exception) as exc:
                last_error = exc
                logger.warning(
                    "Provider %s failed for tag %s: %s — trying fallback",
                    prov.name, tag.value, exc,
                )
                continue

        raise ProviderUnavailable(
            f"All providers failed for tag {tag.value}"
        ) from last_error

    def is_any_available(self) -> bool:
        """Return True if at least one LLM provider is available."""
        self._refresh_available()
        return len(self._all_available) > 0

    # ── internals ───────────────────────────────────────────────────────────────

    @staticmethod
    def _has_critical_tools(tools: list[dict]) -> bool:
        """Heuristic: tools with 'write' or 'delete' in name are critical."""
        critical_keywords = ("write", "delete", "remove", "exec", "command")
        for tool in tools:
            name = tool.get("function", {}).get("name", "")
            if any(kw in name.lower() for kw in critical_keywords):
                return True
        return False

    def _build_fallback_chain(self, primary: LLMProvider) -> list[LLMProvider]:
        """Return ordered list of providers to try, starting with *primary*."""
        order = [self._groq, self._ollama, self._anthropic, self._openai]
        chain = [primary]
        for prov in order:
            if prov is not primary and prov.is_available():
                chain.append(prov)
        return chain


# ---------------------------------------------------------------------------
# SpeedRouter — tag parser + convenience wrapper
# ---------------------------------------------------------------------------

class SpeedRouter:
    """Parses SpeedTag prefixes from text and routes to LLMRouter.

    Usage::

        router = SpeedRouter()
        resp = await router.route("[I] Bonjour")
    """

    def __init__(self, llm_router: Optional[LLMRouter] = None) -> None:
        self._router = llm_router or LLMRouter()

    @staticmethod
    def parse_tag(text: str) -> Optional[SpeedTag]:
        """Return the SpeedTag if *text* starts with one, else ``None``."""
        stripped = text.strip()
        for tag in SpeedTag:
            if stripped.startswith(tag.value):
                return tag
        return None

    @staticmethod
    def strip_tag(text: str) -> str:
        """Remove the leading SpeedTag (and any surrounding whitespace) from *text*."""
        stripped = text.strip()
        for tag in SpeedTag:
            if stripped.startswith(tag.value):
                return stripped[len(tag.value):].strip()
        return stripped

    async def route(
        self,
        text: str,
        system: str = "",
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Parse tag from *text* and generate a response.

        The tag is stripped before sending to the LLM.
        """
        tag = self.parse_tag(text) or SpeedTag.BACKGROUND
        messages = [{"role": "user", "content": self.strip_tag(text)}]
        return await self._router.generate(tag, messages, system, tools, **kwargs)
