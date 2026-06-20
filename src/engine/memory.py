"""MemoryOrchestrator — three-tier memory: Flux (in-memory dict), SQLite (persistent), FAISS/Vector (semantic)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.kernel.config import ConfigManager

logger = logging.getLogger(__name__)

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False
    logger.warning("aiosqlite not installed — SQLite memory tier disabled.")

try:
    import numpy as np
    import faiss
    from sentence_transformers import SentenceTransformer
    HAS_VECTOR = True
except ImportError:
    HAS_VECTOR = False
    logger.info("FAISS / sentence-transformers not installed — vector tier disabled (keyword fallback).")


class MemoryOrchestrator:
    """Three-tier memory system.

    Tiers
    -----
    * Flux — ephemeral in-memory key-value store.
    * SQLite — persistent storage with FTS5 full-text search.
    * Vector — optional FAISS index + sentence-transformers embeddings.
    """

    def __init__(self, config: ConfigManager) -> None:
        self.config = config

        # --- Flux (ephemeral) ---
        self._flux: dict[str, Any] = {}

        # --- SQLite ---
        self._db_path: Optional[Path] = None
        self._db: Optional[aiosqlite.Connection] = None

        # --- Vector ---
        self._embedder: Any = None
        self._faiss_index: Any = None
        self._vector_store: list[dict[str, Any]] = []
        self._vector_dim: int = 384  # default for all-MiniLM-L6-v2

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all tiers."""
        await self._init_db()
        await self._init_vector()
        logger.info("MemoryOrchestrator started.")

    async def close(self) -> None:
        """Close all connections gracefully."""
        if self._db is not None:
            await self._db.close()
        logger.info("MemoryOrchestrator closed.")

    # ------------------------------------------------------------------
    # Flux tier
    # ------------------------------------------------------------------

    def get_flux(self, key: str, default: Any = None) -> Any:
        return self._flux.get(key, default)

    def update_flux(self, key: str, value: Any) -> None:
        self._flux[key] = value

    def clear_flux(self) -> None:
        self._flux.clear()

    # ------------------------------------------------------------------
    # SQLite tier
    # ------------------------------------------------------------------

    async def _init_db(self) -> None:
        if not HAS_AIOSQLITE:
            logger.warning("aiosqlite unavailable — skipping SQLite init.")
            return

        db_dir = self.config.get("memory.db_dir", "data/memory")
        Path(db_dir).mkdir(parents=True, exist_ok=True)
        self._db_path = Path(db_dir) / "lyra_memory.db"

        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row

        await self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                payload     TEXT,
                timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS facts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sujet       TEXT NOT NULL,
                predicat    TEXT NOT NULL,
                objet       TEXT NOT NULL,
                categorie   TEXT DEFAULT 'general',
                source      TEXT DEFAULT 'manual',
                confidence  REAL DEFAULT 1.0,
                timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_facts_sujet ON facts(sujet);
            CREATE INDEX IF NOT EXISTS idx_facts_categorie ON facts(categorie);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        """)

        # FTS5 full-text search on facts
        try:
            await self._db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                    sujet, predicat, objet, categorie,
                    content='facts',
                    content_rowid='id',
                    tokenize='unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, sujet, predicat, objet, categorie)
                    VALUES (new.id, new.sujet, new.predicat, new.objet, new.categorie);
                END;

                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, sujet, predicat, objet, categorie)
                    VALUES ('delete', old.id, old.sujet, old.predicat, old.objet, old.categorie);
                END;

                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, sujet, predicat, objet, categorie)
                    VALUES ('delete', old.id, old.sujet, old.predicat, old.objet, old.categorie);
                    INSERT INTO facts_fts(rowid, sujet, predicat, objet, categorie)
                    VALUES (new.id, new.sujet, new.predicat, new.objet, new.categorie);
                END;
            """)
        except Exception:
            logger.warning("FTS5 init failed (maybe already exists) — continuing.")

        logger.info("SQLite memory ready at %s", self._db_path)

    async def store_event(self, event_type: str, payload: Any) -> int:
        """Insert an event into the events table. Returns row id."""
        if self._db is None:
            logger.warning("SQLite not available — event not stored.")
            return -1
        payload_str = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        cursor = await self._db.execute(
            "INSERT INTO events (event_type, payload) VALUES (?, ?)",
            (event_type, payload_str),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def store_fact(
        self,
        sujet: str,
        predicat: str,
        objet: str,
        categorie: str = "general",
        source: str = "manual",
        confidence: float = 1.0,
    ) -> int:
        """Insert a fact into the facts table. Returns row id."""
        if self._db is None:
            return -1
        cursor = await self._db.execute(
            "INSERT INTO facts (sujet, predicat, objet, categorie, source, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (sujet, predicat, objet, categorie, source, confidence),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def search_facts(self, query: str) -> list[dict[str, Any]]:
        """Full-text search over facts using FTS5."""
        if self._db is None:
            return []

        # Support simple key:value syntax for field-specific search
        fts_query = self._build_fts_query(query)
        try:
            cursor = await self._db.execute(
                "SELECT f.* FROM facts_ftsfts JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ? ORDER BY f.timestamp DESC LIMIT 50",
                (fts_query,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("FTS search failed: %s — falling back to LIKE", exc)
            return await self._search_facts_like(query)

    async def _search_facts_like(self, query: str) -> list[dict[str, Any]]:
        """Fallback LIKE-based search when FTS fails."""
        if self._db is None:
            return []
        like = f"%{query}%"
        cursor = await self._db.execute(
            "SELECT * FROM facts WHERE sujet LIKE ? OR predicat LIKE ? OR objet LIKE ? "
            "ORDER BY timestamp DESC LIMIT 50",
            (like, like, like),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_events(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the most recent events."""
        if self._db is None:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
            (n,),
        )
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(d)
        return result

    @staticmethod
    def _build_fts_query(raw: str) -> str:
        """Transform 'key:value' pairs into FTS5 column queries."""
        terms = []
        for token in raw.split():
            if ":" in token:
                col, val = token.split(":", 1)
                if col in ("sujet", "predicat", "objet", "categorie"):
                    terms.append(f"{col}:{val}")
                else:
                    terms.append(val)
            else:
                terms.append(token)
        return " AND ".join(terms) if terms else raw

    # ------------------------------------------------------------------
    # Vector tier (FAISS)
    # ------------------------------------------------------------------

    async def _init_vector(self) -> None:
        if not HAS_VECTOR:
            return
        try:
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            self._vector_dim = self._embedder.get_sentence_embedding_dimension()
            self._faiss_index = faiss.IndexFlatL2(self._vector_dim)
            logger.info("Vector memory initialised (dim=%d).", self._vector_dim)
        except Exception as exc:
            logger.warning("Vector init failed: %s — using keyword fallback.", exc)
            self._embedder = None
            self._faiss_index = None

    async def store_embedding(self, text: str, metadata: dict[str, Any]) -> None:
        """Compute embedding and add to FAISS index."""
        if self._embedder is None or self._faiss_index is None:
            logger.debug("Vector tier unavailable — skipping embedding.")
            return
        try:
            vec = self._embedder.encode([text], normalize_embeddings=True)
            self._faiss_index.add(np.array(vec, dtype=np.float32))
            self._vector_store.append({"text": text, "metadata": metadata})
        except Exception as exc:
            logger.warning("store_embedding failed: %s", exc)

    async def search_similar(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Semantic search over stored embeddings. Falls back to keyword."""
        if self._embedder is not None and self._faiss_index is not None and self._faiss_index.ntotal > 0:
            try:
                q_vec = self._embedder.encode([query], normalize_embeddings=True)
                distances, indices = self._faiss_index.search(np.array(q_vec, dtype=np.float32), k)
                results = []
                for dist, idx in zip(distances[0], indices[0]):
                    if 0 <= idx < len(self._vector_store):
                        results.append({
                            **self._vector_store[idx],
                            "score": float(1.0 / (1.0 + dist)),
                            "distance": float(dist),
                        })
                return results
            except Exception as exc:
                logger.warning("FAISS search failed: %s — falling back to keyword.", exc)

        return await self._keyword_search(query, k)

    async def _keyword_search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Simple keyword fallback over the vector store."""
        query_lower = query.lower()
        scored = []
        for item in self._vector_store:
            text_lower = item["text"].lower()
            score = sum(1 for word in query_lower.split() if word in text_lower)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:k]]

    # ------------------------------------------------------------------
    # Consolidation helper
    # ------------------------------------------------------------------

    async def consolidate(self, text: str, metadata: dict[str, Any]) -> None:
        """Extract basic facts from text and store them.

        Simple heuristic: looks for patterns like "X is Y", "X est Y",
        "X a Y", etc.
        """
        # Simple rule-based fact extraction
        patterns = [
            (r"(\w+(?:\s+\w+)?)\s+(?:est|sont)\s+(.+?)[.!\n]", "identity"),
            (r"(\w+(?:\s+\w+)?)\s+(?:a|ont)\s+(.+?)[.!\n]", "possession"),
            (r"(\w+(?:\s+\w+)?)\s+(?:utilise|utilisent)\s+(.+?)[.!\n]", "usage"),
            (r"(\w+(?:\s+\w+)?)\s+(?:s'appelle|s'appellent)\s+(.+?)[.!\n]", "name"),
        ]

        source = metadata.get("channel", "consolidation")
        for pattern, categorie in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                sujet = match.group(1).strip().lower()
                objet = match.group(2).strip().lower()
                if len(sujet) > 2 and len(objet) > 2:
                    try:
                        await self.store_fact(
                            sujet=sujet,
                            predicat=categorie,
                            objet=objet,
                            categorie=categorie,
                            source=source,
                            confidence=0.6,
                        )
                    except Exception:
                        pass

        # Also store as an event
        await self.store_event("message.processed", {
            "preview": text[:200],
            "source": source,
        })

