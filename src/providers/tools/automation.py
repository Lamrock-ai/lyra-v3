"""L.Y.R.A v3 — Automation tools.

Weather lookup (Open-Meteo, no API key), n8n webhook triggers,
and preset execution.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from src.providers.tools.registry import ApprovalLevel, Tool, ToolRegistry

logger = logging.getLogger("lyra.providers.tools.automation")

HTTP_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# weather — Open-Meteo (free, no key)
# ---------------------------------------------------------------------------

async def weather_get(lat: float, lon: float) -> str:
    """Get current weather for *lat*, *lon* from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            cw = data.get("current_weather", {})
            return (
                f"Weather at {lat}, {lon}:\n"
                f"  Temperature: {cw.get('temperature', 'N/A')}°C\n"
                f"  Wind speed: {cw.get('windspeed', 'N/A')} km/h\n"
                f"  Conditions code: {cw.get('weathercode', 'N/A')}\n"
                f"  Time: {cw.get('time', 'N/A')}"
            )
        return f"Open-Meteo HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return f"Weather fetch failed: {exc}"


# ---------------------------------------------------------------------------
# n8n webhook trigger
# ---------------------------------------------------------------------------

async def n8n_trigger(workflow_id: str, payload: str = "{}") -> str:
    """Trigger an n8n workflow via its webhook URL.

    The webhook base URL can be set via the ``N8N_WEBHOOK_BASE`` env var
    (defaults to ``http://localhost:5678/webhook``).
    """
    from src.kernel.config import ConfigManager

    cfg = ConfigManager()
    base = cfg.get("N8N_WEBHOOK_BASE", "http://localhost:5678/webhook")
    url = f"{base.rstrip('/')}/{workflow_id}"

    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        return f"Invalid JSON payload: {exc}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=data)
        if resp.status_code < 500:
            return f"n8n workflow '{workflow_id}' triggered (HTTP {resp.status_code})"
        return f"n8n error HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return f"n8n trigger failed: {exc}"


# ---------------------------------------------------------------------------
# preset runner
# ---------------------------------------------------------------------------

_PRESETS_DIR = Path.cwd() / "presets"


async def preset_run(name: str) -> str:
    """Load and execute a preset JSON file from ``presets/``."""
    preset_path = _PRESETS_DIR / f"{name}.json"
    if not preset_path.exists():
        return f"Preset '{name}' not found at {preset_path}"

    try:
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Error loading preset '{name}': {exc}"

    results: list[str] = []
    actions = preset.get("actions", [])

    if not actions:
        return f"Preset '{name}' has no actions."

    # Import here to avoid circular dependency
    from src.providers.tools.registry import ToolRegistry

    registry = ToolRegistry()

    for action in actions:
        tool_name = action.get("tool")
        params = action.get("params", {})
        if not tool_name:
            results.append("  [WARN] action missing 'tool' field")
            continue

        result = await registry.execute_tool(tool_name, params, {"source": f"preset:{name}"})
        status = "✓" if result.success else "✗"
        results.append(f"  {status} {tool_name}: {result.output or result.error}")

    return f"Preset '{name}' executed:\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

async def register(registry: ToolRegistry) -> None:
    """Register automation tools into *registry*."""
    registry.register(Tool(
        name="weather_get",
        description="Get current weather for a given latitude/longitude (Open-Meteo, free).",
        handler=weather_get,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude (e.g. 48.8566)"},
                "lon": {"type": "number", "description": "Longitude (e.g. 2.3522)"},
            },
            "required": ["lat", "lon"],
        },
        category="automation",
    ))

    registry.register(Tool(
        name="n8n_trigger",
        description="Trigger an n8n workflow by ID with an optional JSON payload.",
        handler=n8n_trigger,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": "n8n workflow webhook ID (e.g. 'my-workflow')",
                },
                "payload": {
                    "type": "string",
                    "description": "JSON payload string (default: '{}')",
                },
            },
            "required": ["workflow_id"],
        },
        category="automation",
    ))

    registry.register(Tool(
        name="preset_run",
        description="Execute a preset (named sequence of tools) from the presets/ directory.",
        handler=preset_run,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Preset name (without .json extension)",
                },
            },
            "required": ["name"],
        },
        category="automation",
    ))

    logger.info("Automation tools registered")
