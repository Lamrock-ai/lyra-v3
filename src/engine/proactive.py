"""ProactiveEngine — periodic data collection and initiative generation."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.kernel.eventbus import EventBus
from src.providers.llm.router import LLMRouter, SpeedTag
from src.kernel.config import ConfigManager
from .memory import MemoryOrchestrator

logger = logging.getLogger(__name__)

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    logger.warning("httpx not installed — weather & news collectors disabled.")


class ProactiveEngine:
    """Periodically collect external data and propose initiatives to the user."""

    def __init__(
        self,
        eventbus: EventBus,
        router: LLMRouter,
        memory: MemoryOrchestrator,
        config: ConfigManager,
    ) -> None:
        self.eventbus = eventbus
        self.router = router
        self.memory = memory
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, interval: int = 1800) -> None:
        """Start the periodic collection loop (default: every 30 min)."""
        if not HAS_HTTPX:
            logger.warning("httpx unavailable — proactive engine cannot start.")
            return
        self._http_client = httpx.AsyncClient(timeout=15.0)
        self._task = asyncio.create_task(self._run_loop(interval))
        logger.info("ProactiveEngine started (interval=%ds).", interval)

    async def stop(self) -> None:
        """Stop the periodic loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http_client is not None:
            await self._http_client.aclose()
        logger.info("ProactiveEngine stopped.")

    # ------------------------------------------------------------------
    # Collection loop
    # ------------------------------------------------------------------

    async def _run_loop(self, interval: int) -> None:
        while True:
            try:
                data = await self._collect_all()
                initiatives = await self._generate_initiatives(data)
                if initiatives:
                    await self.eventbus.emit("proactive.initiatives", {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "initiatives": initiatives,
                    })
                    logger.info("Emitted %d proactive initiative(s).", len(initiatives))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Proactive collection cycle failed.")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------

    async def _collect_all(self) -> dict[str, Any]:
        """Run all collectors concurrently."""
        weather_task = self._collect_weather()
        news_task = self._collect_news()
        fs_task = self._collect_filesystem()

        results = await asyncio.gather(
            weather_task, news_task, fs_task,
            return_exceptions=True,
        )

        data: dict[str, Any] = {}
        labels = ["weather", "news", "filesystem"]
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                logger.warning("Collector '%s' failed: %s", label, result)
                data[label] = {"error": str(result)}
            else:
                data[label] = result
        return data

    async def _collect_weather(self) -> dict[str, Any]:
        """Fetch weather from Open-Meteo (free, no API key)."""
        if self._http_client is None:
            return {"error": "HTTP client not available"}
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=48.8566&longitude=2.3522"  # Paris default
            "&current_weather=true"
            "&timezone=auto"
        )
        resp = await self._http_client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def _collect_news(self) -> dict[str, Any]:
        """Fetch news from a public RSS feed."""
        if self._http_client is None:
            return {"error": "HTTP client not available"}

        feeds = self.config.get("proactive.rss_feeds", [
            "https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr",
            "https://hnrss.org/frontpage",
        ])
        items = []
        for feed_url in feeds:
            try:
                resp = await self._http_client.get(feed_url, headers={"User-Agent": "LYRA/1.0"})
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for entry in root.iter("item") if root.find(".//item") is not None else root.iter("entry"):
                    title = entry.findtext("title", "")
                    link = entry.findtext("link", "")
                    if title:
                        items.append({"title": title, "link": link, "source": feed_url})
            except Exception as exc:
                logger.debug("RSS feed %s failed: %s", feed_url, exc)
        return {"articles": items[:20]}

    async def _collect_filesystem(self) -> dict[str, Any]:
        """Scan project directories for recent changes."""
        base_dir = self.config.get("projects_dir", ".")
        projects = []
        try:
            p = Path(base_dir)
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        stats = {
                            "name": child.name,
                            "modified": datetime.fromtimestamp(
                                child.stat().st_mtime, tz=timezone.utc
                            ).isoformat(),
                        }
                        projects.append(stats)
        except OSError as exc:
            return {"error": str(exc)}
        return {"projects": projects}

    # ------------------------------------------------------------------
    # Initiative generation
    # ------------------------------------------------------------------

    async def _generate_initiatives(self, data: dict) -> list[dict[str, Any]]:
        """Use a fast LLM call to generate actionable initiatives from collected data."""
        prompt = (
            "Tu es un assistant proactif. Voici des données collectées :\n"
            f"{data}\n\n"
            "Propose 1 à 3 initiatives pertinentes que tu pourrais suggérer "
            "à l'utilisateur. Réponds UNIQUEMENT avec un JSON valide sous la forme :\n"
            '```json\n[{"title": "...", "description": "...", "priority": 1}]\n```'
        )
        raw = await self.router.generate(prompt, tag=SpeedTag.FAST)
        import json, re
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
        try:
            initiatives = json.loads(raw)
            if isinstance(initiatives, list):
                return initiatives
        except (json.JSONDecodeError, TypeError):
            logger.warning("Could not parse initiatives from LLM output.")
        return []


