-- Forge long-term memory schema (pgvector).
--
-- One row per indexed document/chunk, carrying the THREE retrieval pathways so a
-- new session can recall relevant prior work:
--   * summary   — short LLM summary (global/intent match)
--   * content   — raw segment, also fed into a tsvector for full-text (BM25-ish)
--   * embedding — vector of the raw segment (semantic/nuance match)
--
-- Embedding dimension is 768 — nomic-embed-text via Ollama (open source, local).
-- It MUST match the embedder Forge is configured with; change it here AND in
-- config if you use a different model. (If you recreate this with a different dim,
-- drop the docker volume `forge-pg-data` so init.sql re-runs.)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    doc_id      TEXT        NOT NULL,          -- stable id within a session (e.g. c12, d2)
    agent       TEXT,                          -- architect | engineer | frontend_engineer | ...
    task_id     TEXT,                          -- which task this came from (T4), if any
    kind        TEXT        NOT NULL,          -- spec | decision | trace | tool_result | summary
    project     TEXT,                          -- project name, for scoping recall
    content     TEXT        NOT NULL,          -- raw segment
    summary     TEXT,                          -- LLM summary (nullable until generated)
    embedding   vector(768),                   -- nomic-embed-text dim (nullable until embedded)
    tsv         tsvector,                      -- full-text index over summary + content
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Session ids (S1, S2, …) are unique only WITHIN a project, so the key MUST
    -- include project — otherwise two projects' `S1-T1` collide and ON CONFLICT
    -- overwrites the wrong project's row.
    CONSTRAINT documents_project_session_doc_key UNIQUE (project, session_id, doc_id)
);

-- Keep the full-text vector in sync with summary + content automatically.
CREATE OR REPLACE FUNCTION documents_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.content, '')), 'B');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_tsv ON documents;
CREATE TRIGGER trg_documents_tsv
    BEFORE INSERT OR UPDATE OF summary, content ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_tsv_update();

-- Pathway indexes.
CREATE INDEX IF NOT EXISTS idx_documents_tsv
    ON documents USING GIN (tsv);                          -- P_kw: full-text / BM25-ish
CREATE INDEX IF NOT EXISTS idx_documents_embedding
    ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);  -- P_vec
CREATE INDEX IF NOT EXISTS idx_documents_summary_trgm_help
    ON documents (project);                                -- scope filter

-- Recall is scoped by project and (optionally) excludes the current session.
COMMENT ON TABLE documents IS
    'Forge long-term memory: summary + full-text + embedding per chunk, for cross-session recall.';

-- Session registry (mirrors .forge/sessions.json into Postgres).
--
-- One row per `forge run` (a session). The JSON file on disk stays the
-- durability backstop; this table makes sessions queryable alongside the
-- documents they produced (join on project + session_id). A run on an existing
-- project is a CONTINUATION, so (project, session_id) is the natural key.
CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,          -- S1, S2, … (per project)
    project     TEXT        NOT NULL,          -- project name, scopes the run
    prompt      TEXT        NOT NULL DEFAULT '',
    goal        TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'in_progress',  -- in_progress|done|failed|needs_user
    tasks       JSONB       NOT NULL DEFAULT '[]'::jsonb,     -- completed task ids
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (project, session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project);

COMMENT ON TABLE sessions IS
    'Forge session registry: one row per `forge run`, joinable to documents on (project, session_id).';
