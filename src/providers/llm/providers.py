"""L.Y.R.A v3 — Concrete LLM provider implementations.

All providers use **httpx** directly (no heavy SDKs) to minimise
dependencies.  Each implements the :class:`LLMProvider` ABC and
provides a simple retry-on-5xx logic with optional SSE streaming.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from src.kernel.config import ConfigManager
from .base import LLMProvider, LLMResponse, ProviderCapability, ProviderUnavailable

logger = logging.getLogger("lyra.providers.llm")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = 30.0
MAX_RETRIES = 1


async def _sse_collect(body: str) -> str:
    """Parse a simple SSE stream and concatenate ``data: ...`` payloads."""
    chunks: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                delta = (
                    obj.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    chunks.append(delta)
            except json.JSONDecodeError:
                chunks.append(payload)
    return "".join(chunks)


def _count_usage(response_data: dict, provider: str) -> dict[str, Any]:
    """Extract or estimate token usage from a provider response."""
    usage = response_data.get("usage", {})
    if not usage:
        # rough estimate
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    return usage


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

class GroqProvider(LLMProvider):
    name = "groq"
    capabilities = [ProviderCapability.FAST, ProviderCapability.CHEAP]

    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    PRIMARY_MODEL = "llama-3.3-70b-versatile"
    FALLBACK_MODEL = "mixtral-8x7b-32768"

    def __init__(self) -> None:
        cfg = ConfigManager()
        self._api_key: Optional[str] = cfg.get_or_none("GROQ_API_KEY")
        self._model = self.PRIMARY_MODEL

    def is_available(self) -> bool:
        return bool(self._api_key)

    def get_models(self) -> list[str]:
        return [self.PRIMARY_MODEL, self.FALLBACK_MODEL]

    async def generate(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self._api_key:
            raise ProviderUnavailable("GROQ_API_KEY not configured")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + messages if system else messages,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(self.BASE_URL, headers=headers, json=body)
                elapsed = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    if stream:
                        content = await _sse_collect(resp.text)
                    else:
                        content = data["choices"][0]["message"]["content"]

                    return LLMResponse(
                        content=content,
                        usage=_count_usage(data, self.name),
                        model=data.get("model", self._model),
                        latency_ms=elapsed,
                        provider=self.name,
                    )

                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    logger.warning("Groq 5xx (attempt %d), retrying...", attempt + 1)
                    continue

                raise ProviderUnavailable(
                    f"Groq HTTP {resp.status_code}: {resp.text[:200]}"
                )

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("Groq timeout (attempt %d)", attempt + 1)
                if attempt < MAX_RETRIES:
                    continue
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning("Groq request error (attempt %d): %s", attempt + 1, exc)
                if attempt < MAX_RETRIES:
                    continue

        # Fallback model
        if self._model == self.PRIMARY_MODEL:
            logger.info("Groq primary model failed — falling back to %s", self.FALLBACK_MODEL)
            self._model = self.FALLBACK_MODEL
            return await self.generate(messages, system, tools, stream, temperature, **kwargs)

        raise ProviderUnavailable(f"Groq failed after {MAX_RETRIES + 1} attempts") from last_exc


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaProvider(LLMProvider):
    name = "ollama"
    capabilities = [ProviderCapability.LOCAL, ProviderCapability.CHEAP]

    PRIMARY_MODEL = "llama3:8b"
    FALLBACK_MODEL = "mistral:latest"

    def __init__(self) -> None:
        cfg = ConfigManager()
        self._base_url: str = cfg.get("OLLAMA_URL", "http://localhost:11434")
        self._base_url = self._base_url.rstrip("/")
        self._model = self.PRIMARY_MODEL

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=2)
            return resp.status_code < 500
        except (httpx.RequestError, httpx.TimeoutException):
            return False

    def get_models(self) -> list[str]:
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return [m["name"] for m in models]
        except Exception:
            pass
        return [self.PRIMARY_MODEL, self.FALLBACK_MODEL]

    async def generate(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + messages if system else messages,
            "stream": stream,
            "options": {"temperature": temperature},
        }
        if "max_tokens" in kwargs:
            body["options"]["num_predict"] = kwargs["max_tokens"]

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/chat",
                        json=body,
                    )
                elapsed = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    if stream:
                        content = await _sse_collect(resp.text)
                    else:
                        data = resp.json()
                        content = data.get("message", {}).get("content", "")

                    return LLMResponse(
                        content=content,
                        usage=_count_usage(resp.json() if not stream else {}, self.name),
                        model=self._model,
                        latency_ms=elapsed,
                        provider=self.name,
                    )

                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    continue
                raise ProviderUnavailable(
                    f"Ollama HTTP {resp.status_code}: {resp.text[:200]}"
                )

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue

        # Fallback model
        if self._model == self.PRIMARY_MODEL:
            logger.info("Ollama primary model failed — falling back to %s", self.FALLBACK_MODEL)
            self._model = self.FALLBACK_MODEL
            return await self.generate(messages, system, tools, stream, temperature, **kwargs)

        raise ProviderUnavailable(f"Ollama failed after {MAX_RETRIES + 1} attempts") from last_exc


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    name = "anthropic"
    capabilities = [ProviderCapability.DEEP]

    BASE_URL = "https://api.anthropic.com/v1/messages"
    PRIMARY_MODEL = "claude-sonnet-4-20250514"
    FALLBACK_MODEL = "claude-haiku-3-5-20241022"

    def __init__(self) -> None:
        cfg = ConfigManager()
        self._api_key: Optional[str] = cfg.get_or_none("ANTHROPIC_API_KEY")
        self._model = self.PRIMARY_MODEL

    def is_available(self) -> bool:
        return bool(self._api_key)

    def get_models(self) -> list[str]:
        return [self.PRIMARY_MODEL, self.FALLBACK_MODEL]

    async def generate(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self._api_key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY not configured")

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": kwargs.get("max_tokens", 1024),
            "stream": stream,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
            # Anthropic wants tool_choice
            body["tool_choice"] = {"type": "auto"}

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(self.BASE_URL, headers=headers, json=body)
                elapsed = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    if stream:
                        content = await _sse_collect(resp.text)
                    else:
                        content_blocks = data.get("content", [])
                        content = "".join(
                            b.get("text", "") for b in content_blocks if b.get("type") == "text"
                        )

                    return LLMResponse(
                        content=content,
                        usage=_count_usage(data, self.name),
                        model=data.get("model", self._model),
                        latency_ms=elapsed,
                        provider=self.name,
                    )

                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    logger.warning("Anthropic 5xx (attempt %d), retrying...", attempt + 1)
                    continue

                raise ProviderUnavailable(
                    f"Anthropic HTTP {resp.status_code}: {resp.text[:200]}"
                )

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue

        # Fallback model
        if self._model == self.PRIMARY_MODEL:
            logger.info("Anthropic primary model failed — falling back to %s", self.FALLBACK_MODEL)
            self._model = self.FALLBACK_MODEL
            return await self.generate(messages, system, tools, stream, temperature, **kwargs)

        raise ProviderUnavailable(f"Anthropic failed after {MAX_RETRIES + 1} attempts") from last_exc


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    name = "openai"
    capabilities = [ProviderCapability.DEEP]

    BASE_URL = "https://api.openai.com/v1/chat/completions"
    PRIMARY_MODEL = "gpt-4o"
    FALLBACK_MODEL = "gpt-4o-mini"

    def __init__(self) -> None:
        cfg = ConfigManager()
        self._api_key: Optional[str] = cfg.get_or_none("OPENAI_API_KEY")
        self._model = self.PRIMARY_MODEL

    def is_available(self) -> bool:
        return bool(self._api_key)

    def get_models(self) -> list[str]:
        return [self.PRIMARY_MODEL, self.FALLBACK_MODEL]

    async def generate(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self._api_key:
            raise ProviderUnavailable("OPENAI_API_KEY not configured")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + messages if system else messages,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(self.BASE_URL, headers=headers, json=body)
                elapsed = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    if stream:
                        content = await _sse_collect(resp.text)
                    else:
                        content = data["choices"][0]["message"]["content"]

                    return LLMResponse(
                        content=content,
                        usage=_count_usage(data, self.name),
                        model=data.get("model", self._model),
                        latency_ms=elapsed,
                        provider=self.name,
                    )

                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    logger.warning("OpenAI 5xx (attempt %d), retrying...", attempt + 1)
                    continue

                raise ProviderUnavailable(
                    f"OpenAI HTTP {resp.status_code}: {resp.text[:200]}"
                )

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    continue

        # Fallback model
        if self._model == self.PRIMARY_MODEL:
            logger.info("OpenAI primary model failed — falling back to %s", self.FALLBACK_MODEL)
            self._model = self.FALLBACK_MODEL
            return await self.generate(messages, system, tools, stream, temperature, **kwargs)

        raise ProviderUnavailable(f"OpenAI failed after {MAX_RETRIES + 1} attempts") from last_exc


# ---------------------------------------------------------------------------
# utility
# ---------------------------------------------------------------------------

def get_available_providers() -> list[LLMProvider]:
    """Return a list of provider *instances* that are currently available."""
    candidates: list[LLMProvider] = [
        GroqProvider(),
        OllamaProvider(),
        AnthropicProvider(),
        OpenAIProvider(),
    ]
    return [p for p in candidates if p.is_available()]
