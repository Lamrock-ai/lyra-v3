"""
MCP (Model Context Protocol) client.

Connects to an MCP server via stdio or SSE and exposes tool listing /
invocation.
"""

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

import httpx

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


class MCPClient:
    """Low-level client for the Model Context Protocol."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._sse_url: Optional[str] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if a server is currently connected."""
        return self._connected

    # ------------------------------------------------------------------
    # Connection — stdio
    # ------------------------------------------------------------------

    async def connect_stdio(self, command: str, args: List[str]) -> None:
        """Spawn an MCP server as a subprocess and connect via stdio."""
        if self._connected:
            logger.warning("MCPClient: already connected, disconnect first")
            return

        try:
            self._process = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._connected = True
            logger.info("MCPClient: connected via stdio — %s %s", command, " ".join(args))
        except Exception:
            logger.exception("MCPClient: connect_stdio failed")
            self._connected = False

    # ------------------------------------------------------------------
    # Connection — SSE
    # ------------------------------------------------------------------

    async def connect_sse(self, url: str) -> None:
        """Connect to an MCP server that exposes an SSE endpoint."""
        if self._connected:
            logger.warning("MCPClient: already connected, disconnect first")
            return

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0)
            # Simple connectivity check
            resp = await self._http_client.get(url)
            resp.raise_for_status()
            self._sse_url = url
            self._connected = True
            logger.info("MCPClient: connected via SSE — %s", url)
        except Exception:
            logger.exception("MCPClient: connect_sse failed")
            self._connected = False

    # ------------------------------------------------------------------
    # List available tools
    # ------------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Query the server for available tools.

        Returns a list of tool descriptors (dicts with ``name``,
        ``description``, ``inputSchema``).
        """
        if not self._connected:
            logger.warning("MCPClient: not connected")
            return []

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
            result = await self._send(payload)
            return result.get("tools", [])
        except Exception:
            logger.exception("MCPClient: list_tools failed")
            return []

    # ------------------------------------------------------------------
    # Call a tool
    # ------------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool on the server.

        Returns the full response dict.
        """
        if not self._connected:
            logger.warning("MCPClient: not connected")
            return {}

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            return await self._send(payload)
        except Exception:
            logger.exception("MCPClient: call_tool('%s') failed", name)
            return {}

    # ------------------------------------------------------------------
    # Internal send / receive
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> dict:
        """Send a JSON-RPC request and return the response.

        Works both for stdio and SSE connections.
        """
        if self._process is not None:
            # --- stdio path ---
            raw = (json.dumps(payload) + "\n").encode("utf-8")
            assert self._process.stdin is not None
            self._process.stdin.write(raw)
            self._process.stdin.flush()

            assert self._process.stdout is not None
            line = self._process.stdout.readline()
            if not line:
                return {}
            return json.loads(line.decode("utf-8"))
        elif self._http_client is not None and self._sse_url is not None:
            # --- SSE / HTTP path ---
            resp = await self._http_client.post(self._sse_url, json=payload)
            resp.raise_for_status()
            return resp.json()
        else:
            logger.error("MCPClient: no transport available")
            return {}

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Close the connection and clean up resources."""
        self._connected = False

        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None

        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

        self._sse_url = None
        logger.info("MCPClient: disconnected")
