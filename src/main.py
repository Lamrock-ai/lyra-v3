"""
L.Y.R.A v3 — Your Intelligent Robot Assistant
Usage: python -m src.main [--interface cli|web] [--debug]
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ajoute le dossier parent au path pour les imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    parser = argparse.ArgumentParser(description="L.Y.R.A v3")
    parser.add_argument("--interface", "-i", choices=["cli", "web"], default="cli")
    parser.add_argument("--debug", "-d", action="store_true")
    parser.add_argument("--port", "-p", type=int, default=5000)
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from src.kernel.config import ConfigManager
    from src.kernel.eventbus import EventBus
    from src.kernel.supervision import SupervisionAgent
    from src.providers.llm.router import LLMRouter, SpeedRouter
    from src.providers.tools.registry import ToolRegistry
    from src.providers.tools import browser, filesystem, automation
    from src.engine.memory import MemoryOrchestrator
    from src.engine.orchestrator import Orchestrator
    from src.engine.proactive import ProactiveEngine
    from src.engine.consolidation import ConsolidationAgent

    config = ConfigManager()
    bus = EventBus()
    await bus.start()
    sup = SupervisionAgent(bus)
    router = LLMRouter()
    speed = SpeedRouter()
    registry = ToolRegistry()

    # Enregistre les outils
    await browser.register(registry)
    await filesystem.register(registry)
    await automation.register(registry)

    memory = MemoryOrchestrator(config)
    await memory.start()
    orch = Orchestrator(bus, router, registry, memory, config)
    proactive = ProactiveEngine(bus, router, memory, config)
    consolidation = ConsolidationAgent(bus, memory)

    # Vérifie la disponibilité des LLMs
    llms = [p.name for p in router._all_available]
    logging.info(f"LLMs disponibles: {llms or 'AUCUN — mode degradé'}")

    if args.interface == "cli":
        from src.interfaces.cli import CLI

        cli = CLI(orch, config)
        await cli.run()
    elif args.interface == "web":
        from src.interfaces.web import WebUI

        web = WebUI(orch, config)
        await web.run(port=args.port)


if __name__ == "__main__":
    asyncio.run(main())
