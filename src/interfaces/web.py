"""
L.Y.R.A v3 — Interface web L.Y.R.A-OS
Serveur HTTP + WebSocket via websockets library.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from websockets.asyncio.server import serve, ServerConnection
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

from src import __version__

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/vnd.microsoft.icon", ".ico")
mimetypes.add_type("font/otf", ".otf")


MIME_MAP: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".otf": "font/otf",
    ".woff2": "font/woff2",
}


class WebUI:
    """Interface web JARVIS pour L.Y.R.A v3."""

    def __init__(self, orchestrator, config):
        self.orchestrator = orchestrator
        self.config = config

    async def _ws_handler(self, ws: ServerConnection) -> None:
        """Gere les messages WebSocket du chat."""
        log.info("WebSocket connecte")
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"type": "error", "content": "JSON invalide."}))
                    continue

                message: str = data.get("message", "").strip()
                session_id: Optional[str] = data.get("session_id")

                if not message:
                    await ws.send(json.dumps({"type": "error", "content": "Message vide."}))
                    continue

                sid = session_id or "webui"
                await ws.send(json.dumps({"type": "start", "session_id": sid}))

                try:
                    response = await self.orchestrator.process_message(message)
                    content = str(response)
                    for i in range(0, len(content), 80):
                        chunk = content[i:i + 80]
                        await ws.send(json.dumps({"type": "chunk", "content": chunk}))
                        await asyncio.sleep(0.015)
                    await ws.send(json.dumps({"type": "done"}))
                except Exception as exc:
                    log.exception("Orchestrator error")
                    await ws.send(json.dumps({"type": "error", "content": str(exc)}))

        except Exception:
            pass
        log.info("WebSocket deconnecte")

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Intercepte les requetes HTTP pour servir les fichiers statiques.
        Retourne None pour laisser passer les WebSockets, une Response pour le HTTP."""
        path = request.path if isinstance(request.path, str) else request.path.decode("utf-8", errors="replace")

        # Laisser passer les WebSockets
        if "upgrade" in request.headers.get("Connection", "").lower() or \
           request.headers.get("Upgrade", "").lower() == "websocket":
            return None

        # API
        if path == "/health":
            body = json.dumps({"status": "ok", "version": __version__},
                              ensure_ascii=False).encode("utf-8")
            return Response(200, "OK", Headers([
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ]), body)

        # Stubs API Jarvis (retournent vides pour eviter les 404 dans la console)
        if path.startswith("/api/"):
            body = self._stub_api(path)
            headers = Headers([
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ])
            return Response(200, "OK", headers, body)

        # Fichiers statiques
        if path == "/":
            path = "/index.html"

        file_path = (STATIC_DIR / path.lstrip("/")).resolve()

        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            return Response(403, "Forbidden", Headers([("Connection", "close")]),
                            b"Forbidden")

        if not file_path.exists() or not file_path.is_file():
            return Response(404, "Not Found", Headers([("Connection", "close")]),
                            b"Not Found")

        ext = file_path.suffix.lower()
        content_type = MIME_MAP.get(ext, "application/octet-stream")
        body = file_path.read_bytes()
        return Response(200, "OK", Headers([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "public, max-age=3600"),
            ("Connection", "close"),
        ]), body)

    def _stub_api(self, path: str) -> bytes:
        """Retourne des reponses JSON vides pour les API Jarvis non implementees."""
        stubs: dict[str, dict] = {
            "/api/sessions": {"sessions": []},
            "/api/permissions": {"microphone": False, "screen": True, "camera": False, "files": True},
            "/api/tasks": {"tasks": []},
            "/api/events": {"events": []},
            "/api/projects": {"projects": []},
            "/api/music/status": {"provider": None, "connected": False},
            "/api/initiatives": {"initiatives": []},
            "/api/settings": {},
            "/api/wakeup/status": {"enabled": False},
            "/api/spotify/token": {"token": None},
            "/api/connectors/status": {"connectors": []},
        }
        data = stubs.get(path, {"error": "not_implemented"})
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    async def run(self, host: str = "0.0.0.0", port: int = 5000) -> None:
        """Demarre le serveur HTTP + WebSocket."""
        log.info("Demarrage interface L.Y.R.A sur http://%s:%d", host, port)
        print(f"\n  L.Y.R.A-OS UI: http://{host}:{port}")
        print(f"  Health:       http://{host}:{port}/health\n")

        stop = asyncio.get_running_loop().create_future()

        async with serve(
            self._ws_handler,
            host,
            port,
            process_request=self._process_request,
        ):
            await stop
