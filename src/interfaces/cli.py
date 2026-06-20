"""
CLI interface for L.Y.R.A v3
Simple REPL loop using asyncio and input().
"""

import asyncio
import signal
import sys
import shutil

# Detection des capacites ANSI
_ANSI = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    """Applique un code ANSI si le terminal le supporte."""
    if _ANSI:
        return f"{code}{text}\033[0m"
    return text


def green(text: str) -> str:
    return _style(text, "\033[92m")


def cyan(text: str) -> str:
    return _style(text, "\033[96m")


def yellow(text: str) -> str:
    return _style(text, "\033[93m")


def red(text: str) -> str:
    return _style(text, "\033[91m")


def bold(text: str) -> str:
    return _style(text, "\033[1m")


class CLI:
    """REPL interface utilisateur pour L.Y.R.A v3."""

    def __init__(self, orchestrator, config):
        self.orchestrator = orchestrator
        self.config = config
        self._running = False
        self._commands = {
            "/exit": self._cmd_exit,
            "/quit": self._cmd_exit,
            "/help": self._cmd_help,
            "/ls": self._cmd_ls,
            "/cat": self._cmd_cat,
            "/tree": self._cmd_tree,
        }

    # ── REPL loop ──────────────────────────────────────────────

    async def run(self):
        """Boucle REPL principale."""
        self._running = True

        # Capture Ctrl+C sans planter
        loop = asyncio.get_event_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self._signal_handler)
                except NotImplementedError:
                    pass

        print(bold(cyan("╔══════════════════════════════════════════╗")))
        print(bold(cyan("║     L.Y.R.A v3 — Intelligent Assistant  ║")))
        print(bold(cyan("╚══════════════════════════════════════════╝")))
        print(yellow("Tapez /help pour la liste des commandes."))
        print()

        while self._running:
            try:
                prompt = green("L.Y.R.A v3 > ")
                line = await asyncio.to_thread(input, prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            except OSError:
                # stdin ferme (pipe)
                break

            text = line.strip()
            if not text:
                continue

            # Commande speciale ?
            if text.startswith("/"):
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                handler = self._commands.get(cmd)
                if handler:
                    await handler(args)
                else:
                    print(red(f"Commande inconnue : {cmd}"))
                continue

            # Message normal → orchestrateur
            try:
                response = await self.orchestrator.process_message(text)
                print(cyan(str(response)))
            except Exception as exc:
                print(red(f"Erreur : {exc}"))

        print(yellow("\nAu revoir !"))

    # ── Commandes internes ─────────────────────────────────────

    async def _cmd_exit(self, _args: str) -> None:
        self._running = False

    async def _cmd_help(self, _args: str) -> None:
        print(bold("Commandes disponibles :"))
        print(f"  {green('/exit')}          Quitter L.Y.R.A")
        print(f"  {green('/help')}          Afficher cette aide")
        print(f"  {green('/ls')}            Lister les fichiers du projet")
        print(f"  {green('/cat <path>')}    Afficher le contenu d'un fichier")
        print(f"  {green('/tree')}          Afficher l'arborescence du projet")
        print()
        print("Toute autre entrée est envoyée à l'orchestrateur.")

    async def _cmd_ls(self, _args: str) -> None:
        """Liste les fichiers du repertoire courant ou du projet."""
        import os
        from pathlib import Path

        root = Path.cwd()
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for entry in entries:
            prefix = bold(cyan("[DIR]")) if entry.is_dir() else ""
            print(f"  {prefix} {entry.name}")

    async def _cmd_cat(self, path: str) -> None:
        """Affiche le contenu d'un fichier."""
        if not path:
            print(red("Usage: /cat <chemin>"))
            return
        from pathlib import Path

        fp = Path(path)
        if not fp.exists():
            print(red(f"Fichier introuvable : {fp}"))
            return
        if not fp.is_file():
            print(red(f"Ce n'est pas un fichier : {fp}"))
            return

        try:
            content = fp.read_text(encoding="utf-8")
            print(cyan(f"─── {fp} ───"))
            print(content)
            print(cyan(f"─── fin ───"))
        except Exception as exc:
            print(red(f"Impossible de lire {fp} : {exc}"))

    async def _cmd_tree(self, _args: str) -> None:
        """Affiche l'arborescence du projet."""
        from pathlib import Path

        root = Path.cwd()

        def _show(dir_path: Path, prefix: str = "") -> None:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
            for i, entry in enumerate(entries):
                is_last = i == len(entries) - 1
                connector = "└── " if is_last else "├── "
                print(f"{prefix}{connector}{entry.name}")
                if entry.is_dir():
                    extension = "    " if is_last else "│   "
                    _show(entry, prefix + extension)

        print(bold(cyan(f"📁 {root.name}/")))
        _show(root)

    # ── Signal handler ─────────────────────────────────────────

    def _signal_handler(self) -> None:
        self._running = False
