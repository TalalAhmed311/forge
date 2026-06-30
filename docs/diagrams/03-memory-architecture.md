# Diagram · Memory Architecture

The three memory tiers and the cross-session recall pipeline: how a turn's events
are written cheaply to short-term, distilled into long-term at task completion, and
recalled into a future session via search → RRF fusion → router briefing.

```mermaid
flowchart TB
    subgraph write["WRITE path (cheap, continuous)"]
        direction TB
        step["engineer / agent step"]
        evsink["event_sink<br/>EpisodicEvent (agent + task tagged)"]
        done["task complete"]
        card["distilled card<br/>summary + facts (cheap model)"]
        step --> evsink
        done --> card
    end

    subgraph tiers["The three tiers"]
        direction LR
        t1["<b>Tier-1 Tracker</b><br/>PROJECT_TRACKER.md<br/>verbatim · atomic · restart-safe"]
        st["<b>Short-term Episodic</b><br/>events.py<br/>InMemory | Redis stream (TTL)"]
        lt["<b>Long-term store</b><br/>longterm.py<br/>InMemory | pgvector"]
    end

    evsink --> st
    card --> lt
    card --> st

    subgraph doc["Each long-term doc = 3 pathways"]
        direction LR
        psum["summary<br/>(intent match)"]
        pkw["content / BM25<br/>(exact identifiers)"]
        pvec["embedding<br/>(semantic nuance)"]
    end
    lt --- doc

    subgraph recall["READ path — cross-session recall (once, at task start)"]
        direction TB
        q["new task title (query)"]
        search["store.search()<br/>3 ranked lists per pathway"]
        rrf["RRF fusion<br/>fusion.py · scale-free, rank-based"]
        agg["Aggregator (router role)<br/>aggregator.py · cited briefing"]
        brief["Briefing → injected into<br/>architect / engineer context"]
        q --> search --> rrf --> agg --> brief
    end

    lt --> search

    subgraph ctxasm["Context assembly per task"]
        direction TB
        cm["ContextManager.gather()<br/>tier-1 verbatim FIRST, then tier-2"]
        simple["SimpleContextManager<br/>tier-1 + recent turns"]
        epis["EpisodicContextManager<br/>chunks · router · disclosure · code index"]
        cm --> simple
        cm --> epis
    end

    t1 --> cm
    brief --> cm
    st -->|"this-session slice"| cm

    subgraph back["Backends (graceful fallback — factory.py)"]
        direction LR
        redis[("Redis")]
        pg[("Postgres / pgvector")]
        ollama[("Ollama embeddings<br/>nomic-embed-text 768d")]
    end
    st -.-> redis
    lt -.-> pg
    search -.embed query.-> ollama
    card -.embed.-> ollama

    note["If a backend is down → in-memory fallback;<br/>the on-disk tracker is the durability backstop."]:::n

    classDef n fill:#f6f6f6,stroke:#999,stroke-dasharray:3 3,color:#444;
```

## Reading it

- **Write often & cheap**: every durable step appends a tagged `EpisodicEvent` to
  short-term; only at task completion is a *distilled card* (not the raw transcript)
  promoted to long-term.
- **Read rarely & up-front**: cross-session recall runs **once** at task start. The
  three independent ranked lists are fused by **rank** (RRF — scores are
  incomparable), then the `router` model synthesizes a short **cited** briefing.
- **Tier-1 is always first and verbatim**; tier-2 (recall briefing + this-session
  slice + episodic/code retrieval) is layered under it within the token budget.
- **Degradation**: Redis/Postgres/Ollama all sit on dashed edges; losing them drops
  to in-memory equivalents, never blocking the run.
