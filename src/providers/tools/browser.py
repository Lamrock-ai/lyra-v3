"""L.Y.R.A v3 — Web / browser tools.

Provides web search (DuckDuckGo / fallback) and web scraping
capabilities via **httpx**.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from src.providers.tools.registry import ApprovalLevel, Tool, ToolRegistry

logger = logging.getLogger("lyra.providers.tools.browser")

HTTP_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _extract_text(html: str) -> str:
    """Strip HTML tags and return visible text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    # limit to first 8000 chars
    return text[:8000]


# ---------------------------------------------------------------------------
# duckduckgo (no API key needed)
# ---------------------------------------------------------------------------

async def _ddg_search(query: str, num_results: int = 5) -> list[dict]:
    """Search DuckDuckGo (lite HTML version)."""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    headers = {"User-Agent": _USER_AGENT}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(url, data=params, headers=headers)

    results: list[dict] = []
    if resp.status_code == 200:
        # naive extraction of result links from the HTML
        for match in re.finditer(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.IGNORECASE,
        ):
            results.append({
                "url": match.group(1),
                "title": re.sub(r"<[^>]+>", "", match.group(2)).strip(),
            })
            if len(results) >= num_results:
                break
    return results


# ---------------------------------------------------------------------------
# google fallback (using a scraping approach — no official API key)
# ---------------------------------------------------------------------------

async def _google_search(query: str, num_results: int = 5) -> list[dict]:
    """Fallback Google search via scraping."""
    url = "https://www.google.com/search"
    params = {"q": query, "num": min(num_results, 10)}
    headers = {"User-Agent": _USER_AGENT}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=headers)

    results: list[dict] = []
    if resp.status_code == 200:
        for match in re.finditer(
            r'<a[^>]*href="https?://([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.IGNORECASE,
        ):
            href = match.group(1)
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if title and not href.startswith("google"):
                results.append({"url": f"https://{href}", "title": title})
                if len(results) >= num_results:
                    break
    return results


# ---------------------------------------------------------------------------
# public tool handlers
# ---------------------------------------------------------------------------

async def web_search(query: str, num_results: int = 5) -> str:
    """Search the web for *query* and return formatted results."""
    try:
        results = await _ddg_search(query, num_results)
        if not results:
            logger.info("DuckDuckGo returned no results — trying Google fallback")
            results = await _google_search(query, num_results)
    except Exception as exc:
        logger.warning("DuckDuckGo failed (%s) — trying Google fallback", exc)
        try:
            results = await _google_search(query, num_results)
        except Exception as exc2:
            return f"Web search failed: {exc2}"

    if not results:
        return "No search results found."

    lines = [f"Web search results for '{query}':\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', 'N/A')}")
        lines.append(f"   {r.get('url', 'N/A')}")
    return "\n".join(lines)


async def web_scrape(url: str) -> str:
    """Fetch a URL and return its visible text content."""
    headers = {"User-Agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            text = _extract_text(resp.text)
            return f"Content from {url}:\n\n{text}" if text else "No visible content."
        return f"HTTP {resp.status_code} fetching {url}"
    except Exception as exc:
        return f"Error scraping {url}: {exc}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

async def register(registry: ToolRegistry) -> None:
    """Register browser / web tools into *registry*."""
    registry.register(Tool(
        name="web_search",
        description="Search the web for a given query. Returns a list of result titles and URLs.",
        handler=web_search,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        category="browser",
    ))

    registry.register(Tool(
        name="web_scrape",
        description="Fetch a URL and extract its visible text content.",
        handler=web_scrape,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to scrape",
                },
            },
            "required": ["url"],
        },
        category="browser",
    ))

    logger.info("Browser tools registered")
