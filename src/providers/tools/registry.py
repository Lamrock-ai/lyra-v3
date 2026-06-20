"""L.Y.R.A v3 — Tool registry (singleton), approval guard, and data model.

Tools are registered by category modules and can be invoked by the
LLM or by internal agents.  Each tool declares an *approval level*
that the :class:`ApprovalGuard` checks before execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from src.kernel.models import ApprovalLevel, ToolResult

logger = logging.getLogger("lyra.providers.tools")

Handler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    """Descriptor for a single tool."""

    name: str
    description: str
    handler: Handler
    approval: ApprovalLevel = ApprovalLevel.ALWAYS
    params: dict[str, Any] = field(default_factory=dict)
    category: str = "general"


# ---------------------------------------------------------------------------
# Approval guard
# ---------------------------------------------------------------------------

class ApprovalGuard:
    """Checks whether a tool may be executed without human approval.

    The default policy:
      - ALWAYS → execute immediately
      - NEVER → block unconditionally
      - ASK → request human approval (delegated to caller via callback)
    """

    def __init__(
        self,
        ask_callback: Optional[Callable[[str, str, dict], Awaitable[bool]]] = None,
    ) -> None:
        self._ask_callback = ask_callback

    async def check(self, tool_name: str, level: ApprovalLevel, context: dict) -> bool:
        """Return ``True`` if execution is allowed."""
        if level == ApprovalLevel.ALWAYS:
            return True
        if level == ApprovalLevel.NEVER:
            logger.warning("Tool '%s' blocked (NEVER)", tool_name)
            return False
        # ASK — delegate to callback
        if self._ask_callback:
            return await self._ask_callback(tool_name, level.value, context)
        logger.warning(
            "Tool '%s' requires approval but no callback registered — blocking",
            tool_name,
        )
        return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Singleton registry of available tools.

    Usage::

        registry = ToolRegistry()
        registry.register(Tool(name="web_search", ...))
        result = await registry.execute_tool("web_search", {"query": "..."}, {})
    """

    _instance: Optional[ToolRegistry] = None

    def __new__(cls) -> ToolRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialised") and self._initialised:
            return
        self._tools: dict[str, Tool] = {}
        self._guard: ApprovalGuard = ApprovalGuard()
        self._initialised = True

    # ── management ──────────────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        """Register a tool. Overwrites if a tool with the same name exists."""
        self._tools[tool.name] = tool
        logger.debug("Registered tool '%s' (category=%s)", tool.name, tool.category)

    def unregister(self, name: str) -> None:
        """Remove a previously registered tool."""
        self._tools.pop(name, None)
        logger.debug("Unregistered tool '%s'", name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Optional[Tool]:
        """Look up a tool by name."""
        return self._tools.get(name)

    def list_tools(self, category: Optional[str] = None) -> list[Tool]:
        """Return all tools, optionally filtered by category."""
        if category is None:
            return list(self._tools.values())
        return [t for t in self._tools.values() if t.category == category]

    def set_approval_guard(self, guard: ApprovalGuard) -> None:
        """Replace the approval guard (e.g. with one wired to the UI)."""
        self._guard = guard

    # ── execution ───────────────────────────────────────────────────────────────

    async def execute_tool(
        self,
        name: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Look up, approve, and execute a tool.

        Args:
            name: Tool name.
            params: Parameters passed to the handler.
            context: Execution context (user id, chat id, …).

        Returns:
            ToolResult with success / error information.
        """
        tool = self.get_tool(name)
        if tool is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool '{name}'",
                tool_name=name,
            )

        context = context or {}
        allowed = await self._guard.check(name, tool.approval, context)
        if not allowed:
            return ToolResult(
                success=False,
                error=f"Tool '{name}' blocked by approval guard ({tool.approval.value})",
                tool_name=name,
            )

        t0 = time.perf_counter()
        try:
            result = await tool.handler(**params)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("Tool '%s' succeeded in %.0f ms", name, elapsed)
            return ToolResult(
                success=True,
                output=str(result) if result is not None else "",
                tool_name=name,
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.exception("Tool '%s' failed after %.0f ms", name, elapsed)
            return ToolResult(
                success=False,
                error=str(exc),
                tool_name=name,
                duration_ms=elapsed,
            )

    # ── prompt helpers ──────────────────────────────────────────────────────────

    def get_tools_for_prompt(self) -> str:
        """Return a human-readable description of all registered tools.

        This is intended to be injected into the system prompt so the
        LLM knows what tools are available.
        """
        lines = ["## Available Tools\n"]
        for tool in self._tools.values():
            lines.append(f"### {tool.name}")
            lines.append(f"  Description: {tool.description}")
            lines.append(f"  Category: {tool.category}")
            lines.append(f"  Approval: {tool.approval.value}")
            if tool.params:
                lines.append(f"  Parameters: {tool.params}")
            lines.append("")
        return "\n".join(lines)
