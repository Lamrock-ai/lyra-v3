"""
Obsidian bridge — synchronises project notes and facts into an Obsidian vault.
"""

import logging
import os
from datetime import datetime
from typing import List

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


class ObsidianBridge:
    """Writes structured markdown notes into an Obsidian vault directory."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._vault_path: str = config.get("OBSIDIAN_VAULT_PATH", "")

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._vault_path) and os.path.isdir(self._vault_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self, subdir: str) -> str:
        """Create *subdir* inside the vault if it does not exist."""
        path = os.path.join(self._vault_path, subdir)
        os.makedirs(path, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Sync a single project note
    # ------------------------------------------------------------------

    async def sync_project(self, name: str, content: str) -> bool:
        """Write (or overwrite) a project markdown file.

        File is saved to ``{vault}/01 - Projets/{name}.md``.

        Returns ``True`` on success.
        """
        if not self.is_available():
            logger.warning("ObsidianBridge: vault not available (%s)", self._vault_path)
            return False

        try:
            projects_dir = self._ensure_dir("01 - Projets")
            path = os.path.join(projects_dir, f"{name}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            logger.info("ObsidianBridge: synced project '%s' → %s", name, path)
            return True
        except Exception:
            logger.exception("ObsidianBridge: sync_project failed for '%s'", name)
            return False

    # ------------------------------------------------------------------
    # Sync structured facts
    # ------------------------------------------------------------------

    async def sync_facts(self, faits: List[dict]) -> bool:
        """Generate a structured facts note inside the vault.

        *faits* is a list of dicts, each with at least ``"label"`` and
        ``"value"`` keys.

        File is saved to ``{vault}/01 - Projets/_facts.md``.
        """
        if not self.is_available():
            logger.warning("ObsidianBridge: vault not available (%s)", self._vault_path)
            return False

        try:
            projects_dir = self._ensure_dir("01 - Projets")
            path = os.path.join(projects_dir, "_facts.md")

            lines = [
                f"# Faits — mis à jour le {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "| Sujet | Valeur |",
                "|-------|--------|",
            ]
            for f in faits:
                label = f.get("label", "?")
                value = f.get("value", "")
                lines.append(f"| {label} | {value} |")

            lines.append("")
            content = "\n".join(lines)

            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)

            logger.info("ObsidianBridge: synced %d facts → %s", len(faits), path)
            return True
        except Exception:
            logger.exception("ObsidianBridge: sync_facts failed")
            return False
