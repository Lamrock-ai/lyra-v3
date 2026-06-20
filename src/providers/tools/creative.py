"""L.Y.R.A v3 — Creative / maker tools.

Optional integrations for Fusion 360 (via MCP), 3D printers
(Bambu Lab API), and sub-agent spawning via the EventBus.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.providers.tools.registry import ApprovalLevel, Tool, ToolRegistry

logger = logging.getLogger("lyra.providers.tools.creative")

HTTP_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# Fusion 360 (optional — requires local MCP bridge)
# ---------------------------------------------------------------------------

async def fusion_360_execute(command: str, params: str = "{}") -> str:
    """Send a command to Fusion 360 via the MCP bridge.

    The MCP bridge URL is read from ``FUSION_MCP_URL`` env var
    (defaults to ``http://localhost:8080``).
    """
    from src.kernel.config import ConfigManager

    cfg = ConfigManager()
    base = cfg.get("FUSION_MCP_URL", "http://localhost:8080")
    url = f"{base.rstrip('/')}/execute"

    import json
    try:
        payload = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError as exc:
        return f"Invalid JSON params: {exc}"

    body = {"command": command, "params": payload}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code == 200:
            data = resp.json()
            result = data.get("result", "(no result)")
            return f"Fusion 360: {result}"
        return f"Fusion 360 MCP error HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.RequestError as exc:
        return f"Fusion 360 MCP unreachable ({exc}). Is the bridge running?"


# ---------------------------------------------------------------------------
# Bambu Lab 3D printer (optional)
# ---------------------------------------------------------------------------

async def printer_3d_status() -> str:
    """Fetch the status of a Bambu Lab printer.

    Requires ``BAMBU_PRINTER_IP`` and ``BAMBU_ACCESS_CODE`` env vars.
    """
    from src.kernel.config import ConfigManager

    cfg = ConfigManager()
    ip = cfg.get_or_none("BAMBU_PRINTER_IP")
    code = cfg.get_or_none("BAMBU_ACCESS_CODE")
    if not ip or not code:
        return "Bambu printer not configured (set BAMBU_PRINTER_IP and BAMBU_ACCESS_CODE)"

    # Bambu Lab MQTT / HTTP API — simplified REST-like query
    url = f"http://{ip}/api/v1/status"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {code}"})
        if resp.status_code == 200:
            data = resp.json()
            return (
                f"Printer status:\n"
                f"  State: {data.get('state', 'unknown')}\n"
                f"  Nozzle temp: {data.get('nozzle_temperature', 'N/A')}°C\n"
                f"  Bed temp: {data.get('bed_temperature', 'N/A')}°C\n"
                f"  Progress: {data.get('progress', 'N/A')}%"
            )
        return f"Printer API error HTTP {resp.status_code}"
    except Exception as exc:
        return f"Printer status check failed: {exc}"


async def printer_3d_print(file_path: str) -> str:
    """Send a G-code file to the Bambu Lab printer for printing.

    Requires ``BAMBU_PRINTER_IP`` and ``BAMBU_ACCESS_CODE`` env vars.
    """
    from src.kernel.config import ConfigManager

    cfg = ConfigManager()
    ip = cfg.get_or_none("BAMBU_PRINTER_IP")
    code = cfg.get_or_none("BAMBU_ACCESS_CODE")
    if not ip or not code:
        return "Bambu printer not configured (set BAMBU_PRINTER_IP and BAMBU_ACCESS_CODE)"

    url = f"http://{ip}/api/v1/print"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                url,
                json={"file": file_path},
                headers={"Authorization": f"Bearer {code}"},
            )
        if resp.status_code == 200:
            return f"Print job started: {file_path}"
        return f"Print error HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return f"Print failed: {exc}"


# ---------------------------------------------------------------------------
# Sub-agent spawn
# ---------------------------------------------------------------------------

async def subagent_spawn(task: str, context: str = "{}") -> str:
    """Spawn a sub-agent by publishing a ``subagent.spawn`` event on the EventBus.

    The ``task`` string describes what the sub-agent should do.
    """
    from src.kernel.eventbus import EventBus
    from src.kernel.models import Event, Priority

    import json
    try:
        ctx = json.loads(context) if isinstance(context, str) else context
    except json.JSONDecodeError as exc:
        return f"Invalid context JSON: {exc}"

    bus = EventBus()
    event = Event(
        type="subagent.spawn",
        payload={"task": task, "context": ctx},
        priority=Priority.HIGH,
        source="lyra.providers.tools.creative",
    )
    await bus.publish(event)
    logger.info("Sub-agent spawn event published: task='%s'", task[:80])
    return f"Sub-agent spawned for task: {task[:200]}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

async def register(registry: ToolRegistry) -> None:
    """Register creative / maker tools into *registry*."""
    registry.register(Tool(
        name="fusion_360_execute",
        description="Send a command to Fusion 360 via the MCP bridge (optional).",
        handler=fusion_360_execute,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Fusion 360 command name"},
                "params": {"type": "string", "description": "JSON params string (default: '{}')"},
            },
            "required": ["command"],
        },
        category="creative",
    ))

    registry.register(Tool(
        name="printer_3d_status",
        description="Get the current status of the Bambu Lab 3D printer.",
        handler=printer_3d_status,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {},
        },
        category="creative",
    ))

    registry.register(Tool(
        name="printer_3d_print",
        description="Send a G-code file to the Bambu Lab printer for printing.",
        handler=printer_3d_print,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the G-code file on the printer or accessible URL",
                },
            },
            "required": ["file_path"],
        },
        category="creative",
    ))

    registry.register(Tool(
        name="subagent_spawn",
        description="Spawn a sub-agent to execute a task asynchronously.",
        handler=subagent_spawn,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Description of the task for the sub-agent"},
                "context": {"type": "string", "description": "JSON context string (default: '{}')"},
            },
            "required": ["task"],
        },
        category="creative",
    ))

    logger.info("Creative tools registered")
