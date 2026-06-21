"""Consolidation agents — ConsolidationAgent, CuratorAgent, AutoDream."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.kernel.eventbus import EventBus
from src.kernel.config import ConfigManager
from src.kernel.models import Event
from .memory import MemoryOrchestrator

logger = logging.getLogger(__name__)

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False


# ------------------------------------------------------------------
# ConsolidationAgent
# ------------------------------------------------------------------

class ConsolidationAgent:
    """Listens for 'message.processed' events and extracts facts into memory."""

    def __init__(
        self,
        eventbus: EventBus,
        memory: MemoryOrchestrator,
    ) -> None:
        self.eventbus = eventbus
        self.memory = memory
        self._running = False

    async def start(self) -> None:
        """Subscribe to the event bus."""
        self._running = True
        self.eventbus.on("message.processed", self._handle_message)
        logger.info("ConsolidationAgent started — listening on 'message.processed'")

    async def stop(self) -> None:
        """Unsubscribe."""
        self._running = False
        self.eventbus.off("message.processed", self._handle_message)
        logger.info("ConsolidationAgent stopped.")

    async def _handle_message(self, event: Event) -> None:
        """Extract facts from a processed message."""
        if not self._running:
            return
        try:
            text = event.payload.get("text", "")
            source = event.payload.get("source", "conversation")

            # Delegate to memory.consolidate for basic fact extraction
            await self.memory.consolidate(text, {"channel": source})

            # Additional extraction: look for project references
            await self._extract_project_refs(text, source)

            # Additional extraction: look for technical terms
            await self._extract_technical_terms(text, source)

        except Exception:
            logger.exception("ConsolidationAgent._handle_message failed")

    async def _extract_project_refs(self, text: str, source: str) -> None:
        """Extract patterns like 'projet X', 'project Y' from text."""
        for match in re.finditer(r"(?:projet|project)\s+(\w[\w_-]*)", text, re.IGNORECASE):
            project_name = match.group(1).lower()
            try:
                await self.memory.store_fact(
                    sujet=project_name,
                    predicat="est_un_projet",
                    objet="true",
                    categorie="project",
                    source=source,
                    confidence=0.7,
                )
            except Exception:
                pass

    async def _extract_technical_terms(self, text: str, source: str) -> None:
        """Extract known technical terms."""
        terms = re.findall(r"(python|javascript|typescript|docker|kubernetes|react|"
                           r"fastapi|flask|django|sqlite|postgresql|redis|nginx)", text, re.IGNORECASE)
        for term in set(t.lower() for t in terms):
            try:
                await self.memory.store_fact(
                    sujet="lyra",
                    predicat="connaît",
                    objet=term,
                    categorie="technical",
                    source=source,
                    confidence=0.8,
                )
            except Exception:
                pass


# ------------------------------------------------------------------
# CuratorAgent
# ------------------------------------------------------------------

class CuratorAgent:
    """Daily curation: deduplication, SQLite maintenance, Obsidian archival."""

    def __init__(
        self,
        eventbus: EventBus,
        memory: MemoryOrchestrator,
        obsidian_bridge: Any = None,
    ) -> None:
        self.eventbus = eventbus
        self.memory = memory
        self.obsidian_bridge = obsidian_bridge
        self._task: Optional[asyncio.Task] = None

    async def start(self, interval: int = 86400) -> None:
        """Start daily curation loop (default: every 24h)."""
        self._task = asyncio.create_task(self._run_loop(interval))
        logger.info("CuratorAgent started (interval=%ds).", interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("CuratorAgent stopped.")

    async def _run_loop(self, interval: int) -> None:
        while True:
            try:
                await self._curate()
            except Exception:
                logger.exception("Curation cycle failed.")
            await asyncio.sleep(interval)

    async def _curate(self) -> None:
        """Run all curation tasks."""
        logger.info("Starting daily curation...")

        await self._deduplicate_facts()
        await self._vacuum_sqlite()
        await self._archive_to_obsidian()

        await self.eventbus.emit("curation.complete", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Daily curation complete.")

    async def _deduplicate_facts(self) -> None:
        """Remove duplicate facts from SQLite."""
        if not HAS_AIOSQLITE:
            return
        try:
            db = self.memory._db
            if db is None:
                return
            cursor = await db.execute("""
                DELETE FROM facts
                WHERE id NOT IN (
                    SELECT MIN(id) FROM facts
                    GROUP BY sujet, predicat, objet, categorie
                )
            """)
            deleted = cursor.rowcount
            await db.commit()
            if deleted > 0:
                logger.info("Deduplication removed %d fact(s).", deleted)
        except Exception as exc:
            logger.warning("Deduplication failed: %s", exc)

    async def _vacuum_sqlite(self) -> None:
        """Run VACUUM to reclaim space."""
        if not HAS_AIOSQLITE:
            return
        try:
            db = self.memory._db
            if db is None:
                return
            await db.execute("VACUUM")
            logger.info("SQLite VACUUM complete.")
        except Exception as exc:
            logger.warning("VACUUM failed: %s", exc)

    async def _archive_to_obsidian(self) -> None:
        """Export recent facts to Obsidian if bridge is available."""
        if self.obsidian_bridge is None:
            return
        try:
            recent = await self.memory.get_recent_events(50)
            if recent:
                await self.obsidian_bridge.sync(recent)
                logger.info("Archived %d events to Obsidian.", len(recent))
        except Exception as exc:
            logger.warning("Obsidian archival failed: %s", exc)


# ------------------------------------------------------------------
# AutoDream
# ------------------------------------------------------------------

class AutoDream:
    """Deep nightly maintenance: clean, resolve contradictions, suggest skills.

    Scheduled at 03:00.
    """

    def __init__(
        self,
        eventbus: EventBus,
        memory: MemoryOrchestrator,
        config: ConfigManager,
    ) -> None:
        self.eventbus = eventbus
        self.memory = memory
        self.config = config
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Schedule the nightly maintenance at 03:00."""
        self._task = asyncio.create_task(self._run_scheduled())
        logger.info("AutoDream started — scheduled for 03:00 daily.")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AutoDream stopped.")

    async def _run_scheduled(self) -> None:
        """Run immediately on start, then every 24h, targeting 03:00."""
        while True:
            now = datetime.now(timezone.utc)
            # Calculate seconds until next 03:00 UTC
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now.hour >= 3:
                # Next day
                target = target.replace(day=target.day + 1)

            delay = (target - now).total_seconds()
            if delay < 0:
                delay += 86400

            logger.info("AutoDream next run in %.0f seconds (at %s).", delay, target.isoformat())
            await asyncio.sleep(delay)

            try:
                await self._dream_cycle()
            except Exception:
                logger.exception("AutoDream cycle failed.")

    async def _dream_cycle(self) -> None:
        """Execute the nightly dream cycle."""
        logger.info("🌙 AutoDream cycle starting...")

        # 1. Deep cleanup
        await self._deep_clean()

        # 2. Resolve contradictions
        contradictions = await self._find_contradictions()
        if contradictions:
            logger.info("Found %d contradiction(s).", len(contradictions))
            # In a full implementation, we could use LLM to resolve them

        # 3. Suggest skills based on fact patterns
        suggestions = await self._suggest_skills()
        if suggestions:
            await self.eventbus.emit("autodream.skills_suggested", {
                "suggestions": suggestions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # 4. Emit completion
        await self.eventbus.emit("autodream.complete", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "contradictions_found": len(contradictions),
            "skills_suggested": len(suggestions),
        })

        logger.info("🌙 AutoDream cycle complete.")

    async def _deep_clean(self) -> None:
        """Deep cleanup: remove very old events, trim large payloads."""
        if not HAS_AIOSQLITE:
            return
        try:
            db = self.memory._db
            if db is None:
                return

            # Remove events older than 90 days
            cursor = await db.execute(
                "DELETE FROM events WHERE timestamp < datetime('now', '-90 days')"
            )
            deleted = cursor.rowcount
            await db.commit()
            if deleted > 0:
                logger.info("Deep clean removed %d old event(s).", deleted)

            # Remove low-confidence facts older than 30 days
            cursor = await db.execute(
                "DELETE FROM facts WHERE confidence < 0.3 "
                "AND timestamp < datetime('now', '-30 days')"
            )
            deleted_facts = cursor.rowcount
            await db.commit()
            if deleted_facts > 0:
                logger.info("Deep clean removed %d low-confidence fact(s).", deleted_facts)

        except Exception as exc:
            logger.warning("Deep clean failed: %s", exc)

    async def _find_contradictions(self) -> list[dict[str, Any]]:
        """Find contradictory facts (same sujet, same predicat, different objet)."""
        if not HAS_AIOSQLITE:
            return []
        try:
            db = self.memory._db
            if db is None:
                return []
            cursor = await db.execute("""
                SELECT sujet, predicat, COUNT(DISTINCT objet) as cnt,
                       GROUP_CONCAT(DISTINCT objet) as valeurs
                FROM facts
                GROUP BY sujet, predicat
                HAVING cnt > 1
                LIMIT 20
            """)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Contradiction search failed: %s", exc)
            return []

    async def _suggest_skills(self) -> list[dict[str, str]]:
        """Analyze frequent fact patterns and suggest skill candidates.

        For now, uses a simple heuristic: groups facts by category.
        A full implementation would use the LLM.
        """
        if not HAS_AIOSQLITE:
            return []
        try:
            db = self.memory._db
            if db is None:
                return []
            cursor = await db.execute("""
                SELECT categorie, COUNT(*) as cnt
                FROM facts
                GROUP BY categorie
                ORDER BY cnt DESC
                LIMIT 5
            """)
            rows = await cursor.fetchall()
            suggestions = []
            for r in rows:
                d = dict(r)
                if d["cnt"] >= 5:
                    suggestions.append({
                        "category": d["categorie"],
                        "frequency": str(d["cnt"]),
                        "suggestion": f"Envisager une skill pour le domaine '{d['categorie']}' "
                                      f"({d['cnt']} faits enregistrés)",
                    })
            return suggestions
        except Exception as exc:
            logger.warning("Skill suggestion failed: %s", exc)
            return []


