"""Construct the memory backends with graceful fallback.

Tries the configured services (Redis for short-term, Postgres/pgvector for
long-term); if a service is unreachable or its driver isn't installed, falls back
to the in-memory equivalent. The tracker on disk is the real durability backstop,
so a missing service degrades recall — it never blocks a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from forge.memory.embeddings import HashingEmbedder, OllamaEmbedder
from forge.memory.events import EpisodicLog, InMemoryEpisodicLog, RedisEpisodicLog
from forge.memory.longterm import (
    InMemoryLongTermStore,
    LongTermStore,
    PgVectorLongTermStore,
)
from forge.memory.recall import CrossSessionRecall


@dataclass
class MemoryBundle:
    episodic: EpisodicLog
    long_term: LongTermStore
    recall: Optional[CrossSessionRecall]
    notes: list   # human-readable lines about what connected / fell back
    session_store: Optional[object] = None  # PgSessionStore, when Postgres is up


def _build_embedder(mem: dict):
    if mem.get("embedder") == "hashing":
        return HashingEmbedder()
    return OllamaEmbedder(
        model=mem.get("embedder_model", "nomic-embed-text"),
        dim=mem.get("embedder_dim", 768),
        base_url=mem.get("ollama_base_url", "http://localhost:11434"),
    )


def _embedder_works(embedder) -> bool:
    """nomic-embed-text via Ollama must be reachable AND pulled to use pgvector
    (the vector dimension must match). If not, we fall back to in-memory."""

    try:
        vec = embedder.embed("ping")
        return isinstance(vec, list) and len(vec) == embedder.dim
    except Exception:
        return False


def _connect_redis(url: str):
    try:
        import redis  # type: ignore
    except ImportError:
        return None, "redis driver not installed (pip install '.[memory]')"
    try:
        client = redis.Redis.from_url(url)
        client.ping()
        return client, None
    except Exception as exc:  # connection refused, etc.
        return None, f"redis unreachable ({exc})"


def _connect_pg(dsn: str):
    try:
        import psycopg  # type: ignore
        from pgvector.psycopg import register_vector  # type: ignore
    except ImportError:
        return None, "psycopg/pgvector not installed (pip install '.[memory]')"
    try:
        conn = psycopg.connect(dsn)
        # Autocommit: each statement is its own transaction. The connection is
        # long-lived and shared by reads (recall) and writes (cards, sessions);
        # without this a single failed SELECT would abort the transaction and
        # poison every subsequent statement ("InFailedSqlTransaction").
        conn.autocommit = True
        register_vector(conn)
        return conn, None
    except Exception as exc:
        return None, f"postgres unreachable ({exc})"


def build_memory(
    config,
    project: str,
    session_id: str,
    aggregator_provider=None,
) -> MemoryBundle:
    """Build short-term + long-term memory, falling back to in-memory as needed."""

    mem = config.memory
    notes: list = []

    # --- short-term: Redis (or in-memory) ---
    client, why = _connect_redis(mem.get("redis_url", ""))
    if client is not None:
        episodic: EpisodicLog = RedisEpisodicLog(client, project, session_id)
        notes.append("short-term: redis")
    else:
        episodic = InMemoryEpisodicLog()
        notes.append(f"short-term: in-memory ({why})")

    if not mem.get("long_term", False):
        # Long-term disabled: no cross-session recall, just an empty in-memory store.
        return MemoryBundle(episodic, InMemoryLongTermStore(HashingEmbedder()), None, notes)

    # --- long-term: pgvector (needs Postgres AND a working embedder) ---
    conn, why = _connect_pg(mem.get("pg_dsn", ""))
    embedder = _build_embedder(mem)
    if conn is not None and _embedder_works(embedder):
        store: LongTermStore = PgVectorLongTermStore(conn, embedder)
        notes.append(f"long-term: pgvector ({mem.get('embedder_model')})")
    else:
        if conn is not None:
            why = f"embedder '{mem.get('embedder_model')}' unavailable (pull it in Ollama)"
        embedder = HashingEmbedder()  # dimension-safe offline default
        store = InMemoryLongTermStore(embedder)
        notes.append(f"long-term: in-memory ({why})")

    # Session registry mirror — needs only Postgres, not the embedder, so it is
    # available even when long-term recall falls back to in-memory.
    session_store = None
    if conn is not None:
        from forge.memory.sessions import PgSessionStore

        session_store = PgSessionStore(conn, project)
        if session_store.last_error:
            notes.append(f"sessions: pg DDL failed ({session_store.last_error})")
        else:
            notes.append("sessions: pgvector")

    recall = CrossSessionRecall(store, embedder, aggregator_provider=aggregator_provider)
    return MemoryBundle(episodic, store, recall, notes, session_store=session_store)
