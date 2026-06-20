"""L.Y.R.A v3 — Supervision agent.

Periodically checks the health of registered components and
emits events on the event bus when status changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from .eventbus import EventBus
from .models import Event, HealthStatus, Priority

logger = logging.getLogger(__name__)

HealthCallback = Callable[[], Awaitable[bool]]


class SupervisionAgent:
    """Monitors component health and publishes status events.

    Usage::

        agent = SupervisionAgent(event_bus)
        agent.register("database", db_health_check, timeout=3.0)
        await agent.start_monitoring(interval=15.0)
    """

    def __init__(self, eventbus: EventBus) -> None:
        self._eventbus = eventbus
        self._checks: dict[str, tuple[HealthCallback, float]] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── registration ───────────────────────────────────────────────────────────

    def register(
        self,
        component: str,
        health_callback: HealthCallback,
        timeout: float = 5.0,
    ) -> None:
        """Register a component for periodic health checks.

        Args:
            component: Unique component name (e.g. ``"database"``).
            health_callback: Async callable returning ``True`` if healthy.
            timeout: Max seconds to wait for the callback.
        """
        if component in self._checks:
            logger.warning("Component '%s' already registered — overwriting", component)
        self._checks[component] = (health_callback, timeout)
        logger.info("Registered health check for '%s' (timeout=%.1fs)", component, timeout)

    def unregister(self, component: str) -> None:
        """Remove a previously registered component."""
        self._checks.pop(component, None)
        logger.info("Unregistered health check for '%s'", component)

    # ── health checks ──────────────────────────────────────────────────────────

    async def check_all(self) -> dict[str, HealthStatus]:
        """Run health checks for all registered components.

        Returns:
            A dict mapping component name to its ``HealthStatus``.
        """
        results: dict[str, HealthStatus] = {}

        async def _check_one(component: str) -> tuple[str, HealthStatus]:
            callback, timeout = self._checks[component]
            start = time.perf_counter()
            try:
                alive = await asyncio.wait_for(callback(), timeout=timeout)
                elapsed = (time.perf_counter() - start) * 1000
                status = HealthStatus(
                    component=component,
                    alive=alive,
                    last_check=datetime.now(),
                    error=None if alive else "Callback returned False",
                )
                logger.debug("Health '%s': alive=%s (%.0fms)", component, alive, elapsed)
            except asyncio.TimeoutError:
                status = HealthStatus(
                    component=component,
                    alive=False,
                    last_check=datetime.now(),
                    error=f"Health check timed out after {timeout}s",
                )
                logger.warning("Health '%s': TIMEOUT (%.1fs)", component, timeout)
            except Exception as exc:
                status = HealthStatus(
                    component=component,
                    alive=False,
                    last_check=datetime.now(),
                    error=str(exc),
                )
                logger.exception("Health '%s': exception", component)
            return component, status

        tasks = [_check_one(c) for c in self._checks]
        for coro in asyncio.as_completed(tasks):
            component, status = await coro
            results[component] = status
        return results

    # ── monitoring loop ────────────────────────────────────────────────────────

    async def start_monitoring(self, interval: float = 30.0) -> None:
        """Start a periodic background task that checks all components.

        Args:
            interval: Seconds between check cycles.
        """
        if self._running:
            logger.warning("SupervisionAgent already monitoring")
            return
        self._running = True

        async def _loop() -> None:
            logger.info(
                "SupervisionAgent monitoring started (interval=%.1fs)", interval
            )
            while self._running:
                try:
                    results = await self.check_all()
                    await self._emit_events(results)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("SupervisionAgent check cycle failed")
                await asyncio.sleep(interval)
            logger.info("SupervisionAgent monitoring stopped")

        self._task = asyncio.create_task(_loop())

    async def stop_monitoring(self) -> None:
        """Stop the periodic monitoring task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("SupervisionAgent monitoring stopped")

    # ── event emission ─────────────────────────────────────────────────────────

    async def _emit_events(self, results: dict[str, HealthStatus]) -> None:
        """Publish health events based on check results."""
        for component, status in results.items():
            if status.alive:
                await self._eventbus.publish(
                    Event(
                        type="health.ok",
                        payload={"component": component, "status": status.model_dump()},
                        priority=Priority.LOW,
                        source="supervision",
                    )
                )
            else:
                await self._eventbus.publish(
                    Event(
                        type="health.failure",
                        payload={
                            "component": component,
                            "status": status.model_dump(),
                            "error": status.error,
                        },
                        priority=Priority.HIGH,
                        source="supervision",
                    )
                )
                # Also emit a generic restart suggestion event
                await self._eventbus.publish(
                    Event(
                        type="health.restarted",
                        payload={"component": component},
                        priority=Priority.MEDIUM,
                        source="supervision",
                    )
                )
