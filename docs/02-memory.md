# 2 · Memory Management

> The most intricate subsystem in Forge. This document covers every tier, every
> retrieval pathway, the embeddings, the router, fusion, disclosure, the code index,
> sessions, and how the whole thing degrades gracefully when its backends are down.

## 2.1 The mental model: three tiers

Forge maps the **E-mem** model onto a coding agent with three memory tiers:

| Tier | What it holds | Lifetime | Backend |
|---|---|---|---|
| **Procedural** | prompts, code standards, learned skills | permanent | `forge/agents/prompts/`, `.forge/skills/` |
| **Tier-1 / Authoritative** | goal, task list, confirmed facts, decisions | survives restarts | `PROJECT_TRACKER.md` on disk |
| **Episodic (short-term)** | ordered, agent/task-tagged session events | ephemeral (TTL) | Redis stream (or in-memory) |
| **Long-term** | distilled per-task cards: summary + full-text + embedding | permanent, cross-session | Postgres/pgvector (or in-memory) |

The guiding policy is **write often & cheap, read rarely & up-front**:

- **Write** to short-term after each durable step — a cheap append to Redis.
- **Promote** to long-term at task completion as a *distilled card* (what was built:
  files, outcome, decision — not the raw transcript; the raw stays in `.forge/logs/`).
- **Inject** once, at the **start of each task**: a cross-session briefing plus this
  session's slice. The engineer pulls more on demand with `search_memory` — it is
  never re-injected per step.

```
   per durable step          at task completion           at task start (once)
        │                          │                              │
        ▼                          ▼                              ▼
  episodic.append()        long_term.add_document()        recall.recall()  ── briefing
  (Redis, cheap)           (distilled card → pgvector)     episodic slice  ── "what we did"
```

---

## 2.2 Tier-1: the tracker (`forge/memory/tracker.py`)

`PROJECT_TRACKER.md` is the single source of truth that survives restarts. It is
**plain markdown** — human-readable and machine-parseable — and it is **never
summarized**; it is injected verbatim into every agent context.

### Structure

```markdown
# Project Tracker: <name>
_Last updated: <iso> by <agent>_

## Goal
<the resolved requirement>

## Architecture refs
- specs/S1/architecture.md

## Tasks
- [x] S1-T1  [BE] Implement auth   | test: `pytest tests/test_auth.py`
- [ ] S1-T2  [FE] Login screen     | test: `pytest tests/test_login.py`   ← NEXT

## Confirmed facts (grounding cache)
- file exists: forge/auth.py (seen: read_file forge/auth.py)

## Decisions / escalations
- 2026-06-30 S1-T2: escalation — needs the API contract
```

The task line format is parsed by a single regex, `_TASK_RE` (`tracker.py:20`):
checkbox state, session-namespaced id, optional `[FE]`/`[BE]` surface tag, title,
optional `| test: \`...\``, and the `← NEXT` marker.

### Key methods (the public contract, Section 7.3)

| Method | Purpose |
|---|---|
| `read()` / `read_text()` | parsed `TrackerData` / verbatim text for context injection |
| `write(data, agent)` | atomic re-render (temp file → `fsync` → `os.replace`) |
| `next_task()` | first task with `done == False` — the outer-loop cursor |
| `mark_done(id, summary)` | check a task off |
| `append_fact(fact, source)` | grounding cache → "Confirmed facts" section |
| `append_decision(text)` | dated entry in "Decisions / escalations" |
| `init_empty(project)` | a fresh tracker |

### Why it is restart-safe

`_atomic_write` (`tracker.py:201`) writes to a temp file in the same directory,
`flush()` + `os.fsync()`, then `os.replace()` — atomic on POSIX. A crash mid-write
can never corrupt the spine. The tracker is touched at exactly three points
(architect writes the plan, engineer reads via `gather`, orchestrator marks
progress), which is precisely why `forge resume` works: re-run, read the tracker,
continue at `next_task()`.

### Surface tags and routing

`Task.surface` is `"frontend"` or `"backend"` (default). The architect sets it; the
orchestrator routes `[FE]` tasks to the Senior UI/UX Engineer and everything else to
the Senior Software Engineer. Constants live at `tracker.py:28` (`SURFACE_FRONTEND`,
`SURFACE_BACKEND`) and the tag↔surface maps right below.

---

## 2.3 Short-term episodic memory (`forge/memory/events.py`)

Every agent action becomes an `EpisodicEvent` — tagged with the **agent**, the
**task**, and the **event type** — appended to an ordered log. That tagging is the
point: at a handoff an agent can pull a *precise slice* ("the failure trace for T4",
"the backend's `tool_result`s for the endpoints this screen calls") instead of the
whole transcript.

```python
@dataclass
class EpisodicEvent:
    session_id: str
    agent: str        # architect | engineer | frontend_engineer | clarifier | orchestrator | agent
    type: str         # plan|reasoning|tool_call|tool_result|test_result|escalation|decision|handoff|summary
    content: str
    task_id: Optional[str] = None
    seq: int = 0      # assigned on append, for stable ordering
    ts: float
    meta: dict
```

Two implementations behind one `EpisodicLog` ABC:

- **`InMemoryEpisodicLog`** — process-local; the tested reference and the fallback
  when Redis is unreachable.
- **`RedisEpisodicLog`** — Redis Streams, **one stream per project session**
  (`forge:<project>:session:<id>:events`), with a **TTL** (default 7 days) refreshed
  on every append so short-term memory is genuinely ephemeral but survives
  `forge resume` (a new process) while Redis is up. `_field` (`events.py:117`)
  tolerates the bytes keys/values redis-py returns.

The ABC also provides `by_task(task_id, types)` and `by_agent(agent)` filters — the
slicing that handoffs rely on.

**Durable kinds.** The orchestrator only promotes a subset to long-term:
`("summary", "decision", "tool_result", "escalation", "test_result")` —
see `Orchestrator.DURABLE_KINDS` (`orchestrator.py:614`). `reasoning` stays
short-term only.

---

## 2.4 Long-term memory (`forge/memory/longterm.py`)

The cross-session document store. Each stored chunk carries **three retrieval
pathways** so a later, related session can recall it:

- **summary** — a short LLM/heuristic summary (broad intent match)
- **content** — the raw segment, also indexed for full-text/BM25 (exact identifiers)
- **embedding** — a vector of the raw segment (semantic/nuance failsafe)

`search()` returns **one ranked id-list per pathway**:
`{"summary": [...], "fulltext": [...], "vector": [...]}`. It does *not* fuse — fusion
happens in `recall.py`. Two implementations behind `LongTermStore`:

### `InMemoryLongTermStore`

Reuses the episodic `BM25Index` + a `HashingEmbedder`. Keeps a list of `Document`s,
a parallel list of summary-vectors, and a BM25 index over `content + summary`.
`search()` ranks each pathway independently with cosine / BM25 and returns the top
ids per pathway. Eligibility filtering (`_eligible`) enforces `project` scoping and
`exclude_session`.

### `PgVectorLongTermStore`

Postgres/pgvector (schema in `db/init.sql`). Same interface, so the recall pipeline
is backend-agnostic. Notable details:

- **`add_document`** upserts on the key `(project, session_id, doc_id)` with
  `ON CONFLICT ... DO UPDATE`.
- **Three SQL rankings** in `search()`:
  - summary: `ts_rank_cd` over `to_tsvector(summary)`
  - fulltext: `ts_rank_cd` over the maintained `tsv` column
  - vector: `embedding <=> %s::vector` (cosine distance, ascending)
- **OR-semantics rewrite** (`longterm.py:193`): `plainto_tsquery` ANDs every lexeme,
  so a new task title rarely matched a prior card describing a *different* feature —
  the lexical pathways returned nothing and only the vector pathway ever fired.
  The query rewrites the normalized `&` to `|` so it matches **any** shared term,
  ranked by `ts_rank_cd`. This is the intended BM25-ish behavior.
- **`_ensure_schema`** self-heals the uniqueness key on already-running docker
  volumes (the original key omitted `project`, which let two projects' `S1-T1`
  collide). `db/init.sql` carries the correct key for fresh volumes.

### The pgvector schema (`db/init.sql`)

```sql
CREATE TABLE documents (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT, doc_id TEXT, agent TEXT, task_id TEXT,
    kind TEXT, project TEXT,
    content TEXT,                 -- raw segment
    summary TEXT,                 -- LLM summary
    embedding vector(768),        -- nomic-embed-text dim
    tsv tsvector,                 -- maintained by a trigger over summary+content
    meta JSONB, created_at TIMESTAMPTZ,
    CONSTRAINT documents_project_session_doc_key UNIQUE (project, session_id, doc_id)
);
```

Indexes: GIN on `tsv` (P_kw), `ivfflat` on `embedding` (P_vec), and a btree on
`project` (scope filter). A trigger keeps `tsv` in sync with `setweight` favoring the
summary (weight A) over the content (weight B). **The vector dimension (768) must
match the configured embedder** — change it in both `db/init.sql` and config, and
recreate the `forge-pg-data` volume if you change models.

---

## 2.5 Embeddings (`forge/memory/embeddings.py`, `episodic.py`)

Two embedders, both exposing `.dim` and `.embed(text) -> list[float]`:

- **`OllamaEmbedder`** (default) — calls a local Ollama server's `/api/embeddings`
  with **`nomic-embed-text`** (768-dim). Open source, no API key, nothing leaves the
  machine. This is the production vector pathway.
- **`HashingEmbedder`** (`episodic.py:37`) — dependency-free fallback. Hashes token
  unigrams + bigrams into a fixed 256-dim vector and L2-normalizes. Not semantic in
  the learned sense, but it captures lexical overlap as a dense signal that survives
  paraphrase better than exact keyword match. It is the offline default and what
  `InMemoryLongTermStore` uses.

`cosine(a, b)` (`episodic.py:59`) is a plain dot product because both vectors are
pre-normalized.

**Dimension safety.** `factory._embedder_works` (`factory.py:43`) pings the embedder
and verifies it returns a vector of the expected `dim`. If Ollama is up but the model
isn't pulled (or the dim mismatches pgvector's column), the factory falls back to
the `HashingEmbedder` + in-memory store rather than risk a dimension error.

---

## 2.6 BM25 and chunking (`forge/memory/episodic.py`)

This file is the dependency-free heart of retrieval.

### `BM25Index`

Incremental BM25 (`episodic.py:68`). `add(tokens)` is O(doc length) — no global
re-index. Standard parameters `k1=1.5, b=0.75`. Used by both the in-memory long-term
store and the lessons store.

### `ChunkStore` — dual representation

The episodic engine (Phase 6) partitions the append-only stream of turns into
**overlapping** segments. Each sealed `Chunk` keeps **both**:

- an immutable **raw** segment, and
- a short **summary** (`s_i`),

plus a vector embedding of the raw text and a BM25 entry — the *dual representation*
the multi-pathway router needs.

`append(text)` tokenizes and extends an active buffer; when it reaches `L`
(`chunk_tokens`, default 8000) it **seals** a chunk and carries an **overlap** region
(`L - S`, where `S = chunk_tokens - overlap`) forward so context is preserved across
boundaries. `flush()` seals whatever remains. The default summarizer is a heuristic
"first non-trivial line" (`_default_summarizer`); a model summarizer can be injected.

---

## 2.7 The multi-pathway router (`forge/memory/router.py`)

> Not to be confused with the **`router` config role** (the model that synthesizes a
> cross-session briefing in §2.10). This `Router` class activates *episodic chunks*
> within a session.

Given a query, it activates candidate chunks via the **union** of three orthogonal
pathways and caps the result at `max_activated_chunks` (the paper's saturation
point):

- **P_global** — query vs chunk **summaries** (`0.6 * lexical_overlap + 0.4 * cosine`);
  broad intent, the primary pathway.
- **P_vec** — vector similarity against **raw-chunk** embeddings; the nuance failsafe.
- **P_kw** — BM25 over **raw text**; exact identifiers and names.

Each pathway "bumps" a chunk's `Activation` (recording which pathways fired and the
max score). Results are ranked by `(number_of_pathways_that_fired, score)` — a chunk
that matched on multiple pathways outranks one that matched on a single high score.

---

## 2.8 Progressive disclosure (`forge/memory/disclosure.py`)

The model sees cheap **summaries** by default and pulls raw text via
`fetch_raw_context`. Two safeguards make that safe without per-segment small models:

1. **Auto-expand (the "buried detail" fix).** If a chunk was activated by a
   raw-text pathway (P_vec / P_kw) but its **summary does not contain the matched
   query terms**, the summary fails to explain *why* the chunk matched — so it is
   expanded to raw automatically, without waiting for the model to ask.
   `_summary_explains_match` (`disclosure.py:31`) does this term-overlap test. This
   closes the classic "the necklace hides Sweden" failure where a summary drops the
   one detail the query was about.
2. **Live-raw cap.** `max_live_raw` (default 3) bounds how many whole raw segments
   are simultaneously in context, so dumping segments can't recreate
   "lost-in-the-middle."

---

## 2.9 The code-symbol index (`forge/memory/code_index.py`)

A **parallel, code-specific pathway** (Section 8.5). The episodic router is
dialogue-shaped; code is related by **symbols**, not narrative similarity. Given a
query mentioning `parse_config`, the high-leverage signal is "where is this defined /
who calls it," not embedding similarity.

- Built by walking the workspace (`CodeIndex.build`), skipping noise dirs, indexing
  `.py` files with ctags-style regexes for `def` and `class` definitions.
- `lookup(symbol)` → definition sites; `callsites(symbol)` → where it appears as an
  identifier; `query(text)` → for each known identifier in the text, render its
  definition(s) + a few call sites.
- Backs the **`find_symbol`** tool (doc 4) and the `EpisodicContextManager`'s
  `code_hits`. The orchestrator calls `reindex_code()` after each completed task so
  the index reflects what the engineer just wrote (`orchestrator.py:494`).

The `lookup`/`callsites` contract is stable, so a tree-sitter backend could drop in
to replace the regex approach.

---

## 2.10 Cross-session recall (`recall.py`, `fusion.py`, `aggregator.py`)

This is the **two-stage pipeline** that lets a *new* session benefit from *past*
sessions. It runs over the long-term store and is backend-agnostic.

```
query
  → store.search()         3 ranked lists (summary · fulltext · vector)   [§2.4]
  → RRF fusion             deterministic top-N union                       (fusion.py)
  → Aggregator             the `router` model writes a CITED briefing      (aggregator.py)
  → Briefing               injected into the architect/engineer's context
```

### Stage 1 — Reciprocal Rank Fusion (`fusion.py`)

The three pathways return scores on **incomparable scales** (cosine vs BM25 vs
ts_rank), so Forge fuses by **rank, not score** — RRF is scale-free and the standard
for hybrid search:

```
score(doc) = Σ_pathways  weight / (k + rank_in_that_pathway)      (k = 60)
```

Default weights (`recall.py:23`) slightly favor identifier/keyword matches for code:
`{summary: 1.0, fulltext: 1.2, vector: 1.0}`. Fusion is fully deterministic and
model-free.

### Stage 2 — the aggregator / router model (`aggregator.py`)

After RRF produces the fused top-N, the **`router` role model**'s job is *not* to
re-score — it is to **select** the candidates actually relevant to the new task and
**synthesize** a short, grounded briefing that **cites every claim** (e.g.
`[S1:S1-T2]`). That briefing is what gets injected; the raw segments stay in storage
and are fetched on demand. This is the `router` role's real job in the system.

The prompt (`AGGREGATOR_PROMPT`, `aggregator.py:52`) is deliberate: prior work on the
same project is *almost always relevant context even if it implemented a different
feature* — it tells the model to name the modules/types/conventions to reuse, cite
everything, stay under ~150 words, and reply with the single word `NONE` only if the
notes are genuinely unrelated. `_is_empty_briefing` robustly detects that sentinel
even when small models wrap it in brackets or add trailing prose.

If **no router provider** is available, `_heuristic` presents the top summaries
verbatim with citations — so cross-session recall still works offline.

`Briefing.render()` wraps the text in a labeled `# CROSS-SESSION MEMORY` block that
tells the model it may `fetch_raw` a cited id for detail.

### Observability

`CrossSessionRecall.trace_sink` (set when `FORGE_TRACE=1`) receives the full pipeline
per recall: query → per-pathway hits → fused RRF scores → candidates → briefing →
cited ids. The orchestrator routes this into `.forge/trace/router.log`
(`orchestrator.py:173`).

---

## 2.11 The context manager (`forge/memory/context_manager.py`)

The `ContextManager` ABC owns **tier-2 retrieval** and always emits **tier-1
verbatim first**. `gather(query, window)` returns a `GatheredContext`, whose
`render()` lays out the prompt in a fixed, labeled order:

```
# AUTHORITATIVE STATE (tier 1 — verbatim, trust this)
# CODE SYMBOL HITS
# RELEVANT MEMORY (summaries — call fetch_raw_context(id) for detail)
# EXPANDED RAW CONTEXT
# THIS SESSION SO FAR (what the team already did — build on it)
# CROSS-SESSION MEMORY (from past sessions)
# AVAILABLE SKILLS (pre-verified tools)      ← Phase 7
# LESSONS FROM PAST RUNS (advisory)          ← Phase 7
```

Two implementations:

- **`SimpleContextManager`** (default, `engine: simple`) — tier-1 + the most recent
  raw turns that fit the window. No chunking, no embeddings, no routing. The spec is
  explicit that this is a genuinely useful product and is what runs until the
  episodic engine is enabled.
- **`EpisodicContextManager`** (`engine: episodic`, Phase 6) — combines the
  `ChunkStore` (§2.6), the `Router` (§2.7), summary-first `disclosure` (§2.8) with
  auto-expand + live-raw cap, and the `CodeIndex` (§2.9). `gather` flushes the active
  buffer, routes the query, discloses (spending budget on high-signal raw expansions
  first), adds code hits, and assembles everything under the token budget.

Budgeting uses a crude `CHARS_PER_TOKEN = 4` where no tokenizer is present; the
`window` comes from the engineer provider's `context_window`.

The Phase 7 hooks (`set_improve_hooks`) let the orchestrator inject lessons + the
skill catalog without the memory layer ever importing the improve module — clean
layering via callables.

---

## 2.12 Sessions (`forge/memory/sessions.py`, `projectmd.py`)

### The session registry

Each `forge run` is a **session** with a stable id (`S1`, `S2`, …). The registry
(`.forge/sessions.json`) is what makes a run on an existing project a
**continuation** rather than a fresh start:

- `next_id()` returns one past the highest existing `S<n>`.
- `has_prior()` + the presence of `PROJECT.md` decide `is_continuation`
  (`orchestrator.py:152`).
- Task ids are namespaced by session (`S2-T1`) so they never collide in the shared
  tracker.

`PgSessionStore` mirrors the registry into the Postgres `sessions` table so sessions
are queryable next to the documents they produced (join on `project + session_id`).
The JSON file stays the durability backstop; the mirror is best-effort but surfaces
failures via `last_error` rather than swallowing them. Schema is created idempotently
both in `db/init.sql` (fresh volumes) and via `_SESSIONS_DDL` (`CREATE IF NOT
EXISTS`, for already-running volumes).

### PROJECT.md — the "read me first" consolidation

`projectmd.generate_project_md` (`projectmd.py:64`) regenerates `.forge/PROJECT.md`
**deterministically at session close** from the filesystem + tracker + registry. It
is the cumulative current truth a new session reads first instead of wading through
every prior session's specs:

- **Purpose** (the goal), **Current structure** (`dir_tree`), **Module map** (key
  symbols per file, from the code index), **Architecture decisions**, and a one-line
  **Session log** (`S1 (done): goal → tasks`).

Because it is regenerated from sources, it is cheap and always accurate. `_tier1_text`
(`orchestrator.py:284`) loads it first, ahead of per-session specs and a live
directory tree.

---

## 2.13 The memory factory & graceful degradation (`forge/memory/factory.py`)

`build_memory(config, project, session_id, aggregator_provider)` constructs a
`MemoryBundle` (`episodic`, `long_term`, `recall`, `notes`, `session_store`), trying
the configured services and falling back when they are unreachable:

```
short-term:  Redis reachable?  → RedisEpisodicLog       else InMemoryEpisodicLog
long-term:   long_term=false?  → no recall, empty in-memory store
             Postgres up AND embedder works? → PgVectorLongTermStore
                                          else → InMemoryLongTermStore (HashingEmbedder)
sessions:    Postgres up?      → PgSessionStore (needs only PG, not the embedder)
```

- `_connect_redis` / `_connect_pg` return `(client, why)` and never raise — a missing
  driver or refused connection becomes a human-readable note in `bundle.notes`
  (printed as `• memory: short-term: redis; long-term: pgvector (...)`).
- Postgres connections are **autocommit** so one failed SELECT can't poison every
  subsequent statement (`InFailedSqlTransaction`).
- Every memory write in the orchestrator and agent is wrapped in `try/except: pass`
  with the comment *"memory is best-effort; never break the loop."* The **tracker on
  disk is the real durability backstop**, so degraded recall never blocks a run.

---

## 2.14 How the orchestrator uses memory (the full lifecycle)

Putting it together, for one autonomous run (`forge/orchestrator.py`):

1. **Setup** (`_setup_memory`): build the bundle, give both engineers the
   `search_memory` tool and an `event_sink` that appends tagged episodic events,
   pick the `clarifier` role as the cheap **card summarizer**.
2. **At task start** (`_inject_memory`, once): `recall.recall(task.title)` produces
   the cross-session briefing → `gathered.cross_session`; the last 8 *durable*
   episodic events → `gathered.session_slice` ("this session so far").
3. **During the task**: the engineer's `event_sink` appends `tool_result` /
   `test_result` / `escalation` events to short-term; the model pulls more
   cross-session context on demand via `search_memory`.
4. **On completion** (`_persist_task`): build a deterministic fact backbone
   (`_card_facts`: files touched parsed from the trace + the verifying test command +
   the raw-log path), distill a 1–2 sentence summary with the cheap card summarizer
   (`_card_summary`, out of the hot loop), append a `summary` episodic event, and
   add a long-term `Document` (summary + facts). The raw trace stays in `.forge/logs/`.
5. **On close** (`_close_session`): finish both session registries and regenerate
   `PROJECT.md`.

The interactive agent does the analogous thing per turn: `_briefing` prepends the
cross-session recall to each user message, `_episodic` writes events for successful
`write_file`/`edit_file`/`run_command`, `_persist_card` promotes a card after a
delegated task, and `_promote_session_card` distills the whole conversation into one
card on exit (`forge/agent/session.py`).

---

## 2.15 Configuration reference (memory)

From `DEFAULT_CONFIG["memory"]` (`forge/config.py:38`):

```yaml
memory:
  engine: simple            # "simple" (tier-1 + recent) | "episodic" (full Phase 6)
  chunk_tokens: 8000
  chunk_overlap: 800
  max_activated_chunks: 8
  max_live_raw: 3
  long_term: true           # cross-session recall (Redis + pgvector); ON by default
  redis_url: redis://localhost:6379/0
  pg_dsn: postgresql://forge:forge@localhost:5432/forge
  embedder: ollama          # "ollama" (nomic-embed-text, 768) | "hashing"
  embedder_model: nomic-embed-text
  embedder_dim: 768
```

Bring the backends up with `docker compose up -d` (Redis + pgvector),
`pip install -e ".[memory]"` (drivers), and `ollama pull nomic-embed-text` (local
embeddings). Set `memory.long_term: false` to disable cross-session recall entirely.

---

## 2.16 Summary table — every memory file

| File | Role |
|---|---|
| `tracker.py` | tier-1 authoritative state; atomic, restart-safe |
| `events.py` | short-term episodic log (in-memory / Redis streams) |
| `episodic.py` | `HashingEmbedder`, `BM25Index`, `ChunkStore` (dual representation) |
| `embeddings.py` | `OllamaEmbedder` (nomic-embed-text) + re-exported hashing fallback |
| `longterm.py` | cross-session store (in-memory / pgvector), 3 search pathways |
| `router.py` | multi-pathway chunk activation (global/vec/kw) within a session |
| `disclosure.py` | summary-first disclosure with auto-expand + live-raw cap |
| `code_index.py` | code-symbol pathway (definitions + call sites) |
| `fusion.py` | reciprocal rank fusion (scale-free hybrid ranking) |
| `aggregator.py` | router-model briefing synthesis with citations (+ heuristic) |
| `recall.py` | the full cross-session pipeline (search → fuse → aggregate) |
| `context_manager.py` | tier-1 + tier-2 assembly under a token budget |
| `sessions.py` | session registry (JSON + Postgres mirror) |
| `projectmd.py` | consolidated "read me first" PROJECT.md, regenerated at close |
| `factory.py` | builds the bundle with graceful per-service fallback |
