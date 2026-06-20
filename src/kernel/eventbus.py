"""L.Y.R.A v3 — Event bus (async publish / subscribe).

Events flow through a priority queue and are dispatched
concurrently to all registered subscribers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from .models import Event, Priority

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Asynchronous publish / subscribe event bus.

    Usage::

        bus = EventBus()
        bus.subscribe("llm.request", my_handler)
        await bus.start()
        await bus.publish(Event(type="llm.request", payload={"text": "hi"}))
        ...
        await bus.stop()
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, Event]] = (
            asyncio.PriorityQueue()
        )
        self._subscribers: dict[str, list[Handler]] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── priority mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _priority_value(priority: Priority) -> int:
        """Return a numeric value so that CRITICAL is lowest (highest prio)."""
        mapping = {
            Priority.CRITICAL: 0,
            Priority.HIGH: 1,
            Priority.MEDIUM: 2,
            Priority.LOW: 3,
        }
        return mapping.get(priority, 2)

    # ── publish / subscribe ────────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """Enqueue an event for asynchronous dispatch."""
        prio = self._priority_value(event.priority)
        await self._queue.put((prio, event))
        logger.debug("Published event %s (prio=%s)", event.type, event.priority.value)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register a coroutine handler for a given event type."""
        self._subscribers.setdefault(event_type, []).append(handler)
        logger.debug("Subscribed handler %s for '%s'", handler.__name__, event_type)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove a previously registered handler."""
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug("Unsubscribed handler %s from '%s'", handler.__name__, event_type)

    # ── processing loop ────────────────────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Infinite loop: dequeue events and dispatch them concurrently."""
        logger.info("EventBus processing loop started")
        while self._running:
            try:
                _, event = await self._queue.get()
                await self._dispatch(event)
            except asyncio.CancelledError:
                logger.info("EventBus processing loop cancelled")
                break
            except Exception:
                logger.exception("Error processing event")
        logger.info("EventBus processing loop ended")

    async def _dispatch(self, event: Event) -> None:
        """Call all handlers subscribed to *event.type*."""
        handlers = self._subscribers.get(event.type, [])
        # Also notify wildcard subscribers
        wildcard_handlers = self._subscribers.get("*", [])
        all_handlers = handlers + wildcard_handlers

        if not all_handlers:
            logger.debug("No handlers for event '%s'", event.type)
            return

        tasks = [asyncio.create_task(h(event)) for h in all_handlers]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background processing task."""
        if self._running:
            logger.warning("EventBus already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Gracefully stop the background processing task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("EventBus stopped")
