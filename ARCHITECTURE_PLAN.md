# Plan d'Architecture — L.Y.R.A v3

**Date :** 20 juin 2026
**Couches :** L0 Kernel → L1 Providers → L2 Engine → L3 Interfaces
**Total :** ~38 fichiers Python + pyproject.toml + config

---

## 1. Ordre d'implémentation (phases)

| Phase | Couche | Fichiers | Dépendances externes |
|-------|--------|----------|---------------------|
| **P1** | L0 Kernel | 5 | stdlib + pydantic + python-dotenv |
| **P2** | L1 LLM + Tools | 12 | httpx + SDKs LLM (optionnels) |
| **P3** | L1 Bridges + Audio + Vision | 8 | python-telegram-bot + LiveKit + (optionnels) |
| **P4** | L2 Engine | 8 | aiosqlite + sentence-transformers + FAISS |
| **P5** | L3 Interfaces + main | 6 | fastapi + uvicorn + prompt_toolkit |
| **P6** | Build + Config | 3 | pyproject.toml, .env.example, config |

---

## 2. Liste complète des fichiers et contenu

### Phase 1 — L0 Kernel (5 fichiers)

#### `src/kernel/__init__.py`
Exports publics :
```python
from .models import Event, Fact, Message, SpeedTag, ApprovalLevel, Priority
from .config import ConfigManager
from .eventbus import EventBus
from .supervision import SupervisionAgent
```

#### `src/kernel/models.py`
Dataclasses Pydantic :
- `Priority` (enum) : LOW, MEDIUM, HIGH, CRITICAL
- `SpeedTag` (enum) : INSTANT `[I]`, CONFIRM_FIRE `[CF]`, BACKGROUND `[BG]`, AUTONOME `[BG:PROJECT]`, VOIX `[voix]`
- `ApprovalLevel` (enum) : ALWAYS, ASK, NEVER
- `Event` : id (UUID), type (str), payload (dict), priority (Priority), timestamp (datetime), source (str)
- `Fact` : id, sujet, predicat, objet, categorie, statut, confiance (0-1), importance (0-1), timestamp
- `Message` : role (system/user/assistant/tool), content (str), tags (list[SpeedTag]), metadata (dict)
- `ToolResult` : success (bool), output (str), error (str|None), tool_name, duration_ms
- `ProviderStatus` : available (bool), name, latency_ms, model
- `HealthStatus` : component, alive (bool), last_check, error (str|None)

#### `src/kernel/config.py`
Classe `ConfigManager` (singleton) :
- `load()` : lit `.env` (python-dotenv), charge dans `_data` dict
- `get(key, default=None)` → valeur
- `get_or_none(key)` → str|None
- `is_provider_available(name)` → bool (vérifie les clés nécessaires)
- `get_all()` → dict
- `reload()` → rechargement à chaud
- Détection auto des providers :
  - GROQ_API_KEY présente → Groq dispo
  - OLLAMA_URL accessible (test ping) → Ollama dispo
  - ANTHROPIC_API_KEY présente → Claude dispo
  - OPENAI_API_KEY présente → GPT dispo
- Validation au démarrage : avertit si aucun LLM dispo

#### `src/kernel/eventbus.py`
Classe `EventBus` :
- `__init__()` : `asyncio.PriorityQueue` interne
- `publish(event: Event)` : met dans la queue
- `subscribe(event_type: str, handler: callable, priority: int=0)` : enregistre handler
- `unsubscribe(event_type, handler)` : retire handler
- `_process_loop()` : tâche asyncio continue, dépile les events, dispatch aux handlers via `asyncio.create_task`
- `start()` / `stop()` : gestion du cycle de vie
- Handler reçoit `(event: Event)` et retourne None (fire-and-forget par défaut)
- Support des handlers async et sync

#### `src/kernel/supervision.py`
Classe `SupervisionAgent` :
- `register(component: str, health_callback: callable, timeout: float=5.0)`
- `unregister(component: str)`
- `check_all()` → dict[str, HealthStatus]
- `start_monitoring(interval: float=30.0)` → tâche asyncio périodique
- Sur échec : log + event `health.failure` sur EventBus + jusqu'à 3 tentatives de restart
- Émet `health.ok` / `health.failure` / `health.restarted` sur EventBus

---

### Phase 2 — L1 LLM & Tools (12 fichiers)

#### `src/providers/__init__.py`
Exports : depuis `.llm`, `.tools`, `.bridges`, `.audio`, `.vision`

#### `src/providers/llm/__init__.py`
Exports : `LLMProvider`, `LLMResponse`, `GroqProvider`, `OllamaProvider`, `AnthropicProvider`, `OpenAIProvider`, `LLMRouter`, `SpeedRouter`

#### `src/providers/llm/base.py`
- `LLMProvider` (Protocol) : `async generate(system_prompt, messages, tools=None, stream=False, temperature=0.7)` → `LLMResponse`
- `LLMResponse` (dataclass) : `content, usage(dict), model(str), latency_ms, provider`
- `ProviderCapability` (enum) : FAST, DEEP, LOCAL, CHEAP
- `ProviderUnavailable` (Exception)

#### `src/providers/llm/providers.py`
4 providers implémentant LLMProvider :

- **GroqProvider** : httpx → `https://api.groq.com/openai/v1/chat/completions`. Modèle: `llama-3.3-70b-versatile`. Fallback: `mixtral-8x7b-32768`. Détecte GROQ_API_KEY.
- **OllamaProvider** : httpx → `{OLLAMA_URL}/api/chat`. Auto-détection des modèles installés via `/api/tags`. Modèle par défaut: `llama3.2`. Fonctionne sans clé.
- **AnthropicProvider** : SDK anthropic. Modèle: `claude-sonnet-4-20250514`. Fallback: `claude-haiku-3-5`. Détecte ANTHROPIC_API_KEY.
- **OpenAIProvider** : SDK openai. Modèle: `gpt-4o`. Fallback: `gpt-4o-mini`. Détecte OPENAI_API_KEY.

Chaque provider a `is_available()` → bool qui vérifie config + ping.

#### `src/providers/llm/router.py`
- `LLMRouter` (singleton) :
  - `__init__()` : scanne les providers disponibles via Config
  - `get_fast()` → provider le plus rapide dispo (Groq > Ollama)
  - `get_deep()` → provider le plus profond dispo (Claude > GPT > Groq > Ollama)
  - `get_local()` → OllamaProvider
  - `generate(tag: SpeedTag, ...)` → routing intelligent
  - Fallback chain: si le provider principal fail, essaie le suivant
- `SpeedRouter` :
  - `parse_tag(text: str)` → `SpeedTag` (analyse le premier token)
  - `route(tag, system_prompt, messages)` → appelle LLMRouter avec les bons paramètres
  - `[I]` → get_fast(), pas d'outils, max_tokens=512
  - `[CF]` → get_fast() pour routage, get_deep() si critique
  - `[BG]` → get_fast() pour accusé, file d'attente
  - `[BG:PROJECT]` → MissionEngine (pas LLM direct)

#### `src/providers/tools/__init__.py`
Exports : `ToolRegistry`, outils individuels

#### `src/providers/tools/registry.py`
- `Tool` (dataclass) : name, description, handler(callable), approval(ApprovalLevel), params(dict), category
- `ApprovalGuard` : `check(tool, context)` → approuvé/refusé selon niveau
- `ToolRegistry` (singleton) :
  - `register(tool)` / `unregister(name)` / `get_tool(name)` / `list_tools(category=None)`
  - `execute_tool(name, params, context)` → ToolResult (avec approval check)
  - `get_tools_for_prompt()` → liste formatée pour le system prompt
  - Intègre BudgetGuard (coût API) et UsageTracker

#### `src/providers/tools/browser.py`
- `web_search(query, num_results=5)` → Résultats texte (httpx + scraping)
- `web_scrape(url)` → Contenu texte (beautifulsoup4)
- Approval: ALWAYS

#### `src/providers/tools/communication.py`
- `gmail_send(to, subject, body)` / `gmail_list(max_results=10)` (optionnel, google-api)
- `calendar_list(max_results=10)` / `calendar_create(summary, start, end)` (optionnel)
- `notion_search(query)` / `notion_read(page_id)` (optionnel)
- `telegram_send(chat_id, text)` (utilise TelegramBridge)
- Approval: ASK pour envoi, ALWAYS pour lecture

#### `src/providers/tools/filesystem.py`
- `file_read(path)` / `file_write(path, content)` / `file_delete(path)`
- `file_search(pattern)` / `file_glob(pattern)`
- `cli_run(command, timeout=30)` → stdout/stderr (liste blanche de commandes autorisées)
- `tree(path)` / `ls(path)`
- Approval: ALWAYS pour lecture, ASK pour écriture/suppression, NEVER pour rm -rf

#### `src/providers/tools/automation.py`
- `weather_get(lat, lon)` → Open-Meteo API (gratuit, sans clé)
- `n8n_trigger(workflow_id, payload)` → webhook n8n (optionnel)
- `preset_run(name)` → exécute une macro (lit presets/*.json)
- `show_view(name, params)` → affiche une vue UI
- `spotify_play(uri)` / `spotify_pause()` / `spotify_skip()` → API Spotify (optionnel)

#### `src/providers/tools/creative.py`
- `fusion_360_execute(command, params)` → MCP Fusion 360 (optionnel)
- `printer_3d_status()` / `printer_3d_print(file)` → Bambu Lab API (optionnel)
- `subagent_spawn(task, context)` → spawn sous-agent asynchrone
- Approval: NEVER pour Fusion 360, ASK pour printer

---

### Phase 3 — L1 Bridges & Audio & Vision (8 fichiers)

#### `src/providers/bridges/__init__.py`
Exports : TelegramBridge, ObsidianBridge, MCPClient

#### `src/providers/bridges/telegram.py`
- `TelegramBridge` : utilise `python-telegram-bot`
- `start_polling()` / `stop_polling()` / `send_message(chat_id, text)`
- Handler de messages : reçoit → crée Event → publie sur EventBus
- Commandes intégrées : /start, /help, /chat, ls, cat, tree
- Fonctionne sans TOKEN (vérifie Config)

#### `src/providers/bridges/obsidian.py`
- `ObsidianBridge` : sync unidirectionnel mémoire → Obsidian
- `sync_project(name, content)` → écrit dans `{VAULT_PATH}/01 - Projets/{name}.md`
- `sync_facts(faits)` → génère une note structurée
- Fonctionne sans VAULT_PATH (vérifie Config)

#### `src/providers/bridges/mcp.py`
- `MCPClient` : connexion à des serveurs MCP
- `connect_stdio(command, args)` / `connect_sse(url)`
- `list_tools()` / `call_tool(name, arguments)`
- Fonctionne sans serveur MCP (graceful fallback)

#### `src/providers/audio/__init__.py`
Exports conditionnels : `is_available()`, `VAD`, `STT`, `TTS`, `LiveKitAgent`

#### `src/providers/audio/vad.py`
- `VAD` interface : `is_speech(audio_chunk)` → float (probabilité)
- `SileroVAD` : utilise silero-vad si installé
- `EnergyVAD` : fallback simple basé sur l'énergie RMS
- Optionnel : silero-vad pas requis

#### `src/providers/audio/stt.py`
- `STT` interface : `transcribe(audio_path)` → str
- `DeepgramSTT` : API Deepgram (clé requise)
- `WhisperSTT` : faster-whisper local (modèle tiny, pas de GPU requis)
- Retourne None si aucun dispo

#### `src/providers/audio/tts.py`
- `TTS` interface : `synthesize(text)` → bytes (audio)
- `ElevenLabsTTS` : API ElevenLabs (clé requise)
- `PiperTTS` : Piper local (si binaire dispo)
- Retourne None si aucun dispo

#### `src/providers/audio/livekit.py`
- `LiveKitAgent` : Processus séparé (asyncio subprocess)
- Pipeline : Microphone → VAD → STT → LLM → TTS → Speaker
- Fonctionne sans LiveKit : `is_available()` → False
- Configuration via LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET

#### `src/providers/vision/detector.py`
- `VisionDetector` : capture écran/caméra → analyse
- `detect_objects(image)` → liste d'objets avec boîtes
- `estimate_pose(image)` → landmarks MediaPipe
- Import conditionnel : ultralytics + mediapipe optionnels
- Retourne liste vide si pas dispo

---

### Phase 4 — L2 Engine (8 fichiers)

#### `src/engine/__init__.py`
Exports : Orchestrator, MemoryOrchestrator, ProactiveEngine, MissionEngine, SkillRegistry, Pipeline

#### `src/engine/orchestrator.py`
- `Orchestrator` : noyau du flux
- `process_message(msg: Message)` → `async` :
  1. Parse tag via SpeedRouter
  2. Route vers le bon provider LLM
  3. Exécute outils si nécessaire
  4. Consolide en mémoire (ConsolidationAgent fire-and-forget)
  5. Retourne réponse formatée
- `start()` : abonne aux events `message.incoming` sur EventBus
- Gère le mode dégradé (pas de LLM → messages d'erreur explicatifs)

#### `src/engine/pipeline.py`
- `Pipeline` : pipeline multi-agent complet
- `run(goal, context)` → `async` :
  1. `Architect` : planifie (LLM avec prompt architecte)
  2. `Builder` : génère code en JSON mode strict (LLM avec prompt builder)
  3. `Reviewer` : vérifie syntaxe/sécurité (LLM + pylint si dispo)
  4. `Tester` : exécute tests unitaires (subprocess pytest)
  5. `Archivist` : archive dans Obsidian
  6. `Learner` : extrait leçons
- Retry max 3 sur échec
- `StrictMode` : double review + snapshot pour code sensible

#### `src/engine/memory.py`
- `MemoryOrchestrator` : 3 niveaux
- **Flux** : `dict` en mémoire. `get_flux()`, `update_flux(key, value)`, `clear_flux()`
- **SQLite** : `aiosqlite`. Tables : `events(id, type, payload, timestamp)`, `facts(id, sujet, predicat, objet, categorie, statut, confiance, importance, timestamp)`, `facts_fts` (FTS5). `store_event()`, `store_fact()`, `search_facts(query)`, `get_recent_events(n)`
- **FAISS** : `sentence-transformers` → embeddings → index FAISS. `store_embedding(text, metadata)`, `search_similar(query, k=5)`. Fallback : keyword search sur SQLite si pas dispo.
- `consolidate()` : extrait faits du Flux → SQLite

#### `src/engine/proactive.py`
- `ProactiveEngine` : initiatives toutes les 30 minutes
- Collecteurs :
  - `WeatherCollector` : Open-Meteo (gratuit, sans clé)
  - `EmailCollector` : Gmail API (optionnel)
  - `NewsCollector` : RSS feeds (feedparser)
  - `CalendarCollector` : Google Calendar (optionnel)
  - `FilesystemCollector` : scan des projets actifs
- `InitiativeGenerator` : LLM prend les données → produit initiatives (NOTIFY/VALIDATE/AUTO)
- `start(interval=1800)` / `stop()`
- Niveaux d'autonomie 0-5 vérifiés avant action

#### `src/engine/mission.py`
- `MissionEngine` : projets autonomes [BG:PROJECT]
- `ProjectOrchestrator` : reçoit mission, instancie Manager + Worker + Verifier
- `ProjectManager` : planification (LLM deep)
- `WorkerAgent` : exécution pas-à-pas
- `Verifier` : validation de chaque étape
- `Governance3Axes` : Risk(low/med/high/critical) × Category(code/hardware/network/data/system) × Budget(micro/small/medium/large/unlimited)
- `BudgetGuard` : hard-stop, alerte si dépassement
- `AuditLog` : JSONL immuable dans `data/audit/`
- `ReflexionPostMission` : leçon → skill candidat

#### `src/engine/skills.py`
- `SkillBase` : classe abstraite. `name`, `description`, `async execute(params)`, `version`
- `SkillRegistry` : singleton. `register(skill)`, `load_from_dir(path)`, `get_active_skills()`, `get_combined_system_prompt()`, `cycle_lifecycle(skill_id, new_state)`
- États : CANDIDATE → SANDBOXED_PASS → ACTIVE → STALE → ARCHIVED
- `SkillSynthesizer` : LLM génère une skill à partir d'une trajectoire. Prompt spécialisé. Sortie JSON.
- `CapabilityEngine` : analyse requête utilisateur, détecte capabilities manquantes, propose synthèse

#### `src/engine/consolidation.py`
- `ConsolidationAgent` : subscribe à `message.processed`. Extrait faits du message → `store_fact()`. Fire-and-forget.
- `CuratorAgent` : subscribe à `schedule.daily`. Déduplication des faits, VACUUM SQLite, archivage des vieux events dans Obsidian.
- `AutoDream` : programmé 3h00. Nettoyage profond : résolution de contradictions, extraction de connaissances clés, suggestions de nouveaux skills. Émet `autodream.complete` event.

---

### Phase 5 — L3 Interfaces + main (6 fichiers)

#### `src/interfaces/__init__.py`
Exports : CLI, WebUI, TelegramBot, LiveKitAgent

#### `src/interfaces/cli.py`
- CLI interactive avec `prompt_toolkit`
- Boucle REPL : prompt → Orchestrator.process_message() → affichage
- Commandes : `/chat`, `/ls`, `/cat`, `/tree`, `/help`, `/exit`
- Coloration syntaxique (Pygments)
- Historique des commandes
- Autocomplétion basique
- Marque les tags SpeedRouter en couleur

#### `src/interfaces/web.py`
- Serveur FastAPI / aiohttp
- Routes :
  - `GET /` → page HTML statique (chat UI minimal)
  - `POST /chat` → {message: str} → réponse JSON
  - `GET /health` → status des composants
  - `GET /stream` → SSE streaming des réponses
- Markdown rendering côté serveur (ou JS client)
- Démarrage sur `localhost:5000`

#### `src/interfaces/telegram_bot.py`
- Basé sur TelegramBridge
- Boucle polling dédiée
- Messages reçus → EventBus
- Réponses envoyées via bridge

#### `src/interfaces/livekit_agent.py`
- Processus séparé (exécutable via `python -m src.interfaces.livekit_agent`)
- Pipeline vocal complet : VAD → STT → LLM → TTS → sortie audio
- Configuration via ConfigManager
- Fonctionne seulement si LiveKit configuré

#### `src/main.py`
Point d'entrée principal :
```python
async def main():
    config = ConfigManager.load()
    bus = EventBus()
    await bus.start()
    sup = SupervisionAgent(bus)
    registry = ToolRegistry(config)
    router = LLMRouter(config)
    orch = Orchestrator(bus, router, registry)
    
    if args.interface == "cli":
        await CLI(bus, orch).run()
    elif args.interface == "web":
        await WebUI(bus, orch).run()
    # ...
```
- Parse argparse : `--interface {cli,web,telegram,livekit}`, `--config`, `--debug`
- Initialise tout dans le bon ordre
- Gère Ctrl+C graceful shutdown

#### `src/__init__.py`
`__version__ = "3.0.0"`

---

### Phase 6 — Build & Config (3 fichiers)

#### `pyproject.toml`
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "lyra-v3"
version = "3.0.0"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.0",
    "python-dotenv>=1.0",
    "httpx>=0.27",
    "aiosqlite>=0.20",
]

[project.optional-dependencies]
llm-groq = ["groq>=0.10"]
llm-anthropic = ["anthropic>=0.40"]
llm-openai = ["openai>=1.30"]
audio = [
    "livekit>=1.5",
    "livekit-agents>=0.8",
    "silero-vad>=5.0",
    "faster-whisper>=1.0",
    "elevenlabs>=1.0",
]
vision = ["ultralytics>=8.0", "mediapipe>=0.10"]
telegram = ["python-telegram-bot>=21.0"]
google = ["google-api-python-client>=2.0"]
web = ["fastapi>=0.115", "uvicorn>=0.30", "aiofiles>=24.0"]
cli = ["prompt-toolkit>=3.0", "pygments>=2.18"]
memory = ["sentence-transformers>=3.0", "faiss-cpu>=1.8"]
mcp = ["mcp>=1.0"]
all = [
    "lyra-v3[llm-groq,llm-anthropic,llm-openai]",
    "lyra-v3[audio,vision,telegram,google,web,cli,memory,mcp]",
]

[project.scripts]
lyra = "src.main:main"

[tool.hatch.build.targets.wheel]
packages = ["src"]
```

#### `config/default.yaml`
Configuration par défaut :
```yaml
llm:
  default_provider: auto  # auto | groq | ollama | anthropic | openai
  temperature: 0.7
  max_tokens: 4096
audio:
  enabled: false
  vad_threshold: 0.5
  stt_model: whisper-tiny
  tts_voice: default
proactive:
  enabled: true
  interval_seconds: 1800
  autonomy_level: 1  # 0-5
  collectors: [weather, news, filesystem]
memory:
  sqlite_path: data/lyra.db
  vector_dimension: 384
tools:
  approval_overrides: {}  # tool_name: always|ask|never
```

---

## 3. Interfaces entre modules (matrice)

| Module | Expose | Consomme |
|--------|--------|----------|
| `kernel.models` | Event, Fact, Message, enums | Rien (stdlib only) |
| `kernel.config` | ConfigManager.get(), is_provider_available() | python-dotenv |
| `kernel.eventbus` | publish(), subscribe(), start(), stop() | models.Event |
| `kernel.supervision` | register_component(), health_statuses | eventbus, models |
| `providers.llm.base` | LLMProvider Protocol, LLMResponse | models.Message |
| `providers.llm.providers` | GroqProvider, OllamaProvider, etc. | base, config |
| `providers.llm.router` | LLMRouter.generate(), SpeedRouter.route() | base, providers, config |
| `providers.tools.registry` | ToolRegistry.execute_tool() | config, models |
| `providers.tools.*` | Handlers d'outils individuels | registry (callback) |
| `providers.bridges.*` | start(), stop(), send() | config, eventbus |
| `providers.audio.*` | transcribe(), synthesize(), is_speech() | config |
| `providers.vision.*` | detect_objects(), estimate_pose() | config |
| `engine.orchestrator` | process_message() | eventbus, router, registry, memory |
| `engine.pipeline` | run(goal) | router, tools, memory |
| `engine.memory` | store_event(), search_facts(), search_similar() | config, models |
| `engine.proactive` | start(), stop() | eventbus, memory, router |
| `engine.mission` | launch_mission(), get_status() | eventbus, memory, router, tools |
| `engine.skills` | register(), synthesize(), list_skills() | config, models |
| `engine.consolidation` | consolidate(), curate() | eventbus, memory |
| `interfaces.cli` | run() | orchestrator, config |
| `interfaces.web` | run() | orchestrator, config |
| `interfaces.*` | run() | config, eventbus, bridges |
| `main.py` | main() | tout |

---

## 4. Risques et mitigation

| Risque | Impact | Mitigation |
|--------|--------|-----------|
| Aucun LLM dispo (ni Groq, ni Ollama) | **BLOCKANT** | Message explicite au démarrage. Mode dégradé avec réponses pré-enregistrées. |
| Dépendances optionnelles qui échouent à l'import | Modéré | `ImportGuard` pattern : try/except ImportError partout avec fallback. `ImportError` logs niveau debug. |
| SQLite corrompu | Moyen | WAL mode, VACUUM régulier, backup automatique dans CuratorAgent |
| FAISS incompatible Windows | Moyen | Fallback vers numpy + cosine similarity. Détection OS au chargement. |
| LiveKit consomme trop de ressources | Faible | Processus séparé, kill si mémoire > 500MB, mode texte seulement si pas assez de RAM |
| Race conditions EventBus | Moyen | asyncio.Lock sur subscribe/publish, PriorityQueue thread-safe |
| Fuite mémoire du Flux dict | Faible | Taille max du Flux (1000 entrées), purge LRU |

---

## 5. Dépendances clés (gradient optionnel)

```
REQUIRED (toujours):
  pydantic, python-dotenv, httpx, aiosqlite

LLM (au moins un):
  groq / anthropic / openai / ollama (httpx only)

RECOMMENDED:
  prompt-toolkit, pygments (CLI)
  fastapi, uvicorn (Web UI)

OPTIONAL (graceful fallback):
  python-telegram-bot, google-api-python-client
  livekit, livekit-agents, silero-vad, faster-whisper, elevenlabs
  ultralytics, mediapipe, sentence-transformers, faiss-cpu
  beautifulsoup4, feedparser, mcp
```

---

## 6. Structure finale des dossiers

```
E:\lyra_v3\
├── prompts/
│   └── system.md                    # Déjà créé
├── config/
│   └── default.yaml                 # P6
├── scripts/                         # Scripts utilitaires (optionnel)
├── src/
│   ├── __init__.py                  # P5 - version
│   ├── main.py                      # P5 - entry point
│   ├── kernel/
│   │   ├── __init__.py              # P1
│   │   ├── models.py                # P1
│   │   ├── config.py                # P1
│   │   ├── eventbus.py              # P1
│   │   └── supervision.py           # P1
│   ├── providers/
│   │   ├── __init__.py              # P2
│   │   ├── llm/
│   │   │   ├── __init__.py          # P2
│   │   │   ├── base.py              # P2
│   │   │   ├── providers.py         # P2
│   │   │   └── router.py            # P2
│   │   ├── tools/
│   │   │   ├── __init__.py          # P2
│   │   │   ├── registry.py          # P2
│   │   │   ├── browser.py           # P2
│   │   │   ├── communication.py     # P2
│   │   │   ├── filesystem.py        # P2
│   │   │   ├── automation.py        # P2
│   │   │   └── creative.py          # P2
│   │   ├── bridges/
│   │   │   ├── __init__.py          # P3
│   │   │   ├── telegram.py          # P3
│   │   │   ├── obsidian.py          # P3
│   │   │   └── mcp.py               # P3
│   │   ├── audio/
│   │   │   ├── __init__.py          # P3
│   │   │   ├── vad.py               # P3
│   │   │   ├── stt.py               # P3
│   │   │   ├── tts.py               # P3
│   │   │   └── livekit.py           # P3
│   │   └── vision/
│   │       ├── __init__.py          # P3
│   │       └── detector.py          # P3
│   ├── engine/
│   │   ├── __init__.py              # P4
│   │   ├── orchestrator.py          # P4
│   │   ├── pipeline.py              # P4
│   │   ├── memory.py                # P4
│   │   ├── proactive.py             # P4
│   │   ├── mission.py               # P4
│   │   ├── skills.py                # P4
│   │   └── consolidation.py         # P4
│   └── interfaces/
│       ├── __init__.py              # P5
│       ├── cli.py                   # P5
│       ├── web.py                   # P5
│       ├── telegram_bot.py          # P5
│       └── livekit_agent.py         # P5
├── .env.example                     # Déjà créé
├── .gitignore                       # Déjà créé
├── README.md                        # Déjà créé
├── pyproject.toml                   # P6
└── ARCHITECTURE_PLAN.md             # Ce fichier
```

**Total fichiers Python : 37** (sans compter config/.env.exemple/lisez-moi déjà existants)
**Total phases : 6** → P1→P2→P3→P4→P5→P6
