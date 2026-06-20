# L.Y.R.A. v3

**Your Intelligent Robot Assistant** — Noyau cognitif d'un système cyber-physique.

Fusion de **lyra-engine** (pipeline multi-agent, mémoire distribuée, ponts Telegram/Obsidian) et **Jarvis-OS** (architecture 4 couches, LiveKit, gouvernance).

## Architecture

```
L0 — Kernel      EventBus, Supervision, Config/Secrets
L1 — Providers   LLM (Groq/Claude/Ollama), Audio (LiveKit), Vision, Ponts, Tools
L2 — Engine      Orchestrator, MemoryOrch., ProactiveEngine, MissionEngine, Skills
L3 — Interfaces  LiveKit Audio, CLI, Web UI, Telegram
```

## Prompt système

`prompts/system.md` — 9 sections, ~3600 tokens :
1. Identité & Rôle
2. Architecture en couches
3. SpeedRouter & priorités
4. Pipeline multi-agent & experts
5. Mémoire & consolidation
6. Moteurs autonomes
7. Extensibilité (Skills/Presets/Vues)
8. Gouvernance & sécurité
9. Directives de réponse
