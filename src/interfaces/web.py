"""
Web UI interface for L.Y.R.A v3
Minimal HTTP server using only stdlib (asyncio + http.server).
Self-contained HTML chat page, no external templates.
"""

import asyncio
import json
import logging
from urllib.parse import parse_qs
from src import __version__

log = logging.getLogger(__name__)

PAGE_HTML = """\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>L.Y.R.A v3</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; justify-content: center; min-height: 100vh; }}
  .container {{ max-width: 720px; width: 100%; padding: 2rem 1rem; display: flex; flex-direction: column; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; color: #58a6ff; }}
  .subtitle {{ color: #8b949e; margin-bottom: 1.5rem; }}
  #chat {{ flex: 1; overflow-y: auto; border: 1px solid #30363d; border-radius: 8px; padding: 1rem; background: #161b22; min-height: 300px; max-height: 60vh; margin-bottom: 1rem; display: flex; flex-direction: column; gap: 0.75rem; }}
  .msg {{ padding: 0.5rem 0.75rem; border-radius: 8px; max-width: 85%; line-height: 1.4; }}
  .msg.user {{ align-self: flex-end; background: #1f6feb; color: #fff; }}
  .msg.bot {{ align-self: flex-start; background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }}
  .msg.error {{ align-self: flex-start; background: #3d1f1f; color: #f85149; border: 1px solid #f85149; }}
  .msg .time {{ font-size: 0.7rem; opacity: 0.6; margin-top: 0.25rem; }}
  .input-row {{ display: flex; gap: 0.5rem; }}
  #input {{ flex: 1; padding: 0.6rem 0.8rem; border-radius: 6px; border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 1rem; outline: none; }}
  #input:focus {{ border-color: #58a6ff; }}
  #send {{ padding: 0.6rem 1.2rem; border-radius: 6px; border: none; background: #238636; color: #fff; font-size: 1rem; cursor: pointer; }}
  #send:hover {{ background: #2ea043; }}
  #send:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .status {{ text-align: center; font-size: 0.8rem; color: #8b949e; margin-top: 0.5rem; }}
</style>
</head>
<body>
<div class="container">
  <h1>L.Y.R.A v3</h1>
  <div class="subtitle">Your Intelligent Robot Assistant</div>
  <div id="chat"></div>
  <div class="input-row">
    <input type="text" id="input" placeholder="Tapez votre message..." autofocus>
    <button id="send" onclick="sendMessage()">Envoyer</button>
  </div>
  <div class="status" id="status">Prêt</div>
</div>
<script>
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const status = document.getElementById('status');

  function addMessage(text, role) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    const now = new Date();
    div.innerHTML = text.replace(/\\n/g, '<br>') + '<div class="time">' + now.toLocaleTimeString() + '</div>';
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendBtn.disabled = true;
    status.textContent = 'L.Y.R.A réfléchit...';

    addMessage(text, 'user');

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text })
      });
      const data = await res.json();
      if (data.response) {
        addMessage(data.response, 'bot');
      } else if (data.error) {
        addMessage(data.error, 'error');
      }
    } catch (err) {
      addMessage('Erreur de connexion au serveur.', 'error');
    }

    sendBtn.disabled = false;
    status.textContent = 'Prêt';
    input.focus();
  }

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(); });
</script>
</body>
</html>
"""


class _RequestHandler:
    """Protocole asyncio pour lire les requetes HTTP et y repondre."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            writer.close()
            return

        request_line = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = request_line.split()
        if len(parts) < 2:
            writer.close()
            return

        method = parts[0].upper()
        path = parts[1]

        # Lecture du corps si Content-Length present
        body = b""
        headers_raw = raw.decode("utf-8", errors="replace")
        clen = 0
        for hdr in headers_raw.split("\r\n")[1:]:
            if hdr.lower().startswith("content-length:"):
                try:
                    clen = int(hdr.split(":", 1)[1].strip())
                except ValueError:
                    pass
                break

        if clen > 0:
            try:
                body = await reader.readexactly(clen)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                pass

        # Routage
        if method == "GET" and path == "/":
            await self._send_html(writer, PAGE_HTML)
        elif method == "GET" and path == "/health":
            await self._send_json(writer, {"status": "ok", "version": __version__})
        elif method == "POST" and path == "/chat":
            await self._handle_chat(writer, body)
        else:
            await self._send_status(writer, 404, "Not Found")

    # ── helpers ────────────────────────────────────────────────

    async def _send_html(self, writer: asyncio.StreamWriter, html: str,
                         status: int = 200) -> None:
        data = html.encode("utf-8")
        resp = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + data
        writer.write(resp)
        await writer.drain()
        writer.close()

    async def _send_json(self, writer: asyncio.StreamWriter, obj: dict,
                         status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        resp = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + data
        writer.write(resp)
        await writer.drain()
        writer.close()

    async def _send_status(self, writer: asyncio.StreamWriter, status: int,
                           msg: str) -> None:
        await self._send_json(writer, {"error": msg}, status=status)

    async def _handle_chat(self, writer: asyncio.StreamWriter,
                           body: bytes) -> None:
        try:
            payload = json.loads(body)
            text = payload.get("message", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_status(writer, 400, "Invalid JSON")
            return

        if not text or not isinstance(text, str):
            await self._send_status(writer, 400, "Missing 'message' field")
            return

        try:
            response = await self.orchestrator.process_message(text)
            await self._send_json(writer, {"response": str(response)})
        except Exception as exc:
            log.exception("Chat error")
            await self._send_json(writer, {"error": str(exc)}, status=500)


class WebUI:
    """Interface web minimaliste pour L.Y.R.A v3."""

    def __init__(self, orchestrator, config):
        self.orchestrator = orchestrator
        self.config = config

    async def run(self, host: str = "0.0.0.0", port: int = 5000) -> None:
        """Demarre le serveur HTTP asynchrone."""
        handler = _RequestHandler(self.orchestrator)
        server = await asyncio.start_server(handler.handle, host, port)

        addr = server.sockets[0].getsockname()
        log.info("Web UI demarre sur http://%s:%d", addr[0], addr[1])
        print(f"\n  🌐 Web UI: http://{addr[0]}:{addr[1]}")
        print(f"  📡 Health: http://{addr[0]}:{addr[1]}/health\n")

        async with server:
            await server.serve_forever()
