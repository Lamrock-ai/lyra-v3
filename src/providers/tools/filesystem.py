"""L.Y.R.A v3 — Filesystem tools.

Read, write, list, and search files within allowed project directories.
CLI execution is gated behind an ``ASK`` approval level.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from src.providers.tools.registry import ApprovalLevel, Tool, ToolRegistry

logger = logging.getLogger("lyra.providers.tools.filesystem")

# Whitelist of allowed command prefixes for ``cli_run``
ALLOWED_COMMANDS = (
    "git", "python", "pip", "npm", "npx", "node", "cargo", "make",
    "dir", "echo", "cat", "ls", "pwd",
)

# Root project directory (used to restrict path traversal)
_PROJECT_ROOT = Path.cwd().resolve()


def _safe_path(path_str: str) -> Path:
    """Resolve *path_str* and ensure it stays inside the project root."""
    p = Path(path_str).resolve()
    # Allow subdirs of project root
    if _PROJECT_ROOT not in p.parents and p != _PROJECT_ROOT:
        raise PermissionError(f"Path '{p}' is outside the project root ({_PROJECT_ROOT})")
    return p


# ---------------------------------------------------------------------------
# tool handlers
# ---------------------------------------------------------------------------

async def file_read(path: str) -> str:
    """Read a text file and return its contents."""
    safe = _safe_path(path)
    try:
        text = safe.read_text(encoding="utf-8")
        return f"Contents of {safe}:\n\n{text}"
    except Exception as exc:
        return f"Error reading {safe}: {exc}"


async def file_write(path: str, content: str) -> str:
    """Write *content* to a text file (overwrites existing)."""
    safe = _safe_path(path)
    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        logger.info("Wrote %d bytes to %s", len(content), safe)
        return f"Written {len(content)} bytes to {safe}"
    except Exception as exc:
        return f"Error writing {safe}: {exc}"


async def file_delete(path: str) -> str:
    """Delete a file."""
    safe = _safe_path(path)
    try:
        safe.unlink()
        logger.warning("Deleted file %s", safe)
        return f"Deleted {safe}"
    except Exception as exc:
        return f"Error deleting {safe}: {exc}"


async def file_glob(pattern: str) -> str:
    """Find files matching a glob pattern (relative to project root)."""
    try:
        matches = list(_PROJECT_ROOT.rglob(pattern))
        if not matches:
            return f"No files matching '{pattern}'"
        lines = [f"Files matching '{pattern}':"]
        for m in matches[:50]:
            lines.append(f"  {m.relative_to(_PROJECT_ROOT)}")
        if len(matches) > 50:
            lines.append(f"  ... and {len(matches) - 50} more")
        return "\n".join(lines)
    except Exception as exc:
        return f"Glob error: {exc}"


async def ls(path: str = ".") -> str:
    """List directory contents."""
    safe = _safe_path(path)
    try:
        entries = list(safe.iterdir())
        if not entries:
            return f"Directory {safe} is empty."
        lines = [f"Contents of {safe}:"]
        for e in sorted(entries):
            marker = "/" if e.is_dir() else ""
            lines.append(f"  {e.name}{marker}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing {safe}: {exc}"


async def tree(path: str = ".", max_depth: int = 3) -> str:
    """Show a recursive directory tree (limited depth)."""
    safe = _safe_path(path)

    def _walk(dir_path: Path, depth: int = 0) -> list[str]:
        if depth > max_depth:
            return ["  " * depth + "..."]
        lines: list[str] = []
        try:
            entries = sorted(dir_path.iterdir())
        except PermissionError:
            return [f"  {dir_path} (permission denied)"]
        for e in entries:
            indent = "  " * depth
            marker = "/" if e.is_dir() else ""
            lines.append(f"{indent}{e.name}{marker}")
            if e.is_dir():
                lines.extend(_walk(e, depth + 1))
        return lines

    try:
        result = [f"Tree of {safe}:"]
        result.extend(_walk(safe))
        return "\n".join(result)
    except Exception as exc:
        return f"Error building tree: {exc}"


async def cli_run(command: str, timeout: int = 30) -> str:
    """Run a shell command (whitelisted) and return stdout+stderr."""
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return "Empty command."

    whitelisted = any(cmd_parts[0].startswith(allowed) for allowed in ALLOWED_COMMANDS)
    if not whitelisted:
        return (
            f"Command '{cmd_parts[0]}' is not in the allowed list: "
            f"{', '.join(ALLOWED_COMMANDS)}"
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            output += "\n--- stderr ---\n" + stderr.decode("utf-8", errors="replace")
        return output or "(no output)"
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout}s"
    except Exception as exc:
        return f"Command error: {exc}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

async def register(registry: ToolRegistry) -> None:
    """Register filesystem tools into *registry*."""
    registry.register(Tool(
        name="file_read",
        description="Read the contents of a text file.",
        handler=file_read,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative or absolute)"},
            },
            "required": ["path"],
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="file_write",
        description="Write content to a text file (overwrites existing).",
        handler=file_write,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="file_delete",
        description="Delete a file permanently.",
        handler=file_delete,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to delete"},
            },
            "required": ["path"],
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="file_glob",
        description="Find files matching a glob pattern (relative to project root).",
        handler=file_glob,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            },
            "required": ["pattern"],
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="ls",
        description="List files and directories in a given path.",
        handler=ls,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: .)"},
            },
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="tree",
        description="Show a recursive directory tree (up to 3 levels deep).",
        handler=tree,
        approval=ApprovalLevel.ALWAYS,
        params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: .)"},
                "max_depth": {"type": "integer", "description": "Maximum depth (default: 3)"},
            },
        },
        category="filesystem",
    ))

    registry.register(Tool(
        name="cli_run",
        description="Run a shell command (whitelisted: git, python, pip, npm, npx, node, cargo, make, dir, echo, cat, ls, pwd).",
        handler=cli_run,
        approval=ApprovalLevel.ASK,
        params={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
            },
            "required": ["command"],
        },
        category="filesystem",
    ))

    logger.info("Filesystem tools registered")
