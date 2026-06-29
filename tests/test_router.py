"""Phase 6 exit tests (Section 8.6):
  * cross-session fact recall — a fact from "session 1" is retrievable in
    "session 5" without the whole history in context;
  * buried-detail recovery — auto-expand surfaces a detail the summary dropped;
  * the three routing pathways and the code symbol index work.
"""

from __future__ import annotations

from forge.memory.code_index import CodeIndex
from forge.memory.context_manager import EpisodicContextManager
from forge.memory.disclosure import disclose
from forge.memory.episodic import BM25Index, ChunkStore, HashingEmbedder, tokenize
from forge.memory.router import Router


# -- chunking & dual representation (8.2) ---------------------------------- #


def test_chunking_overlap_and_dual_representation():
    store = ChunkStore(chunk_tokens=10, overlap=4)
    for i in range(6):
        store.append(f"line {i} alpha beta gamma delta")
    store.flush()
    assert len(store.chunks) >= 2
    for c in store.chunks:
        assert c.raw and c.summary           # dual representation
        assert len(c.vec) == store.embedder.dim
        assert c.tokens                       # raw tokens kept for BM25/expand


def test_bm25_ranks_keyword_match():
    idx = BM25Index()
    idx.add(tokenize("the auth middleware validates jwt tokens"))
    idx.add(tokenize("the database connection pool settings"))
    assert idx.score("jwt", 0) > idx.score("jwt", 1)


# -- multi-pathway routing (8.3) ------------------------------------------- #


def test_router_keyword_pathway_finds_exact_identifier():
    store = ChunkStore(chunk_tokens=50, overlap=5)
    store.append("We refactored the parse_config helper to read YAML.")
    store.append("Unrelated chatter about coffee and the weather today.")
    store.flush()
    acts = Router(store).route("where is parse_config defined")
    assert acts
    top = acts[0]
    assert "kw" in top.pathways or "global" in top.pathways


# -- progressive disclosure + auto-expand (8.4) ---------------------------- #


def test_auto_expand_recovers_buried_detail():
    """The 'necklace hides Sweden' case: a detail dropped by the summary must be
    auto-expanded when a raw pathway matches it."""

    store = ChunkStore(chunk_tokens=100, overlap=10)
    # The summary heuristic keeps the FIRST meaningful line; the key fact is buried
    # several lines down, so summary-only retrieval would miss it.
    store.append(
        "Discussed the UI redesign at length and color choices.\n"
        "Many details about spacing and fonts followed here.\n"
        "Crucially, the API key is stored in the SWEDEN_VAULT env var."
    )
    store.flush()
    chunk = store.chunks[0]
    assert "SWEDEN_VAULT" not in chunk.summary  # genuinely buried

    acts = Router(store).route("SWEDEN_VAULT")
    disc = disclose("SWEDEN_VAULT", acts, store, max_live_raw=3)
    # The buried detail is recovered as raw, not left as an unhelpful summary.
    assert any("SWEDEN_VAULT" in raw for _, raw in disc.raws)


def test_live_raw_cap_is_enforced():
    store = ChunkStore(chunk_tokens=20, overlap=2)
    for i in range(5):
        store.append(f"chunk {i} contains the buried token ZZTOKEN{i} deep inside text here")
    store.flush()
    acts = Router(store, max_activated_chunks=8).route("ZZTOKEN0 ZZTOKEN1 ZZTOKEN2 ZZTOKEN3")
    disc = disclose("ZZTOKEN0 ZZTOKEN1 ZZTOKEN2 ZZTOKEN3", acts, store, max_live_raw=2)
    assert len(disc.raws) <= 2


# -- code symbol index (8.5) ----------------------------------------------- #


def test_code_index_finds_definition_and_callsites(tmp_path):
    (tmp_path / "m.py").write_text(
        "def parse_config(path):\n    return {}\n\n"
        "def main():\n    cfg = parse_config('x')\n    return cfg\n"
    )
    idx = CodeIndex.build(str(tmp_path))
    defs = idx.lookup("parse_config")
    assert defs and defs[0].kind == "def"
    hits = idx.query("how does parse_config work")
    assert hits and "parse_config" in hits[0]
    assert any("used at" in line for line in hits[0].splitlines())


# -- cross-session recall via the full manager ----------------------------- #


def test_cross_session_fact_recall(tmp_path):
    """A fact appended early is retrievable many turns later without dumping all
    history — gather returns it via routing, not by keeping everything in window."""

    cm = EpisodicContextManager(
        tier1_provider=lambda: "(tracker)",
        workspace=str(tmp_path),
        chunk_tokens=30,
        chunk_overlap=4,
    )
    # "Session 1": the decisive fact.
    cm.append("Decision: we chose the PostgreSQL driver psycopg3 for the DB layer.")
    # "Sessions 2-5": lots of unrelated turns that would blow a naive window.
    for i in range(40):
        cm.append(f"Turn {i}: routine progress note about formatting and tests.")

    gathered = cm.gather("which postgresql driver did we choose", window=4000)
    blob = gathered.render().lower()
    assert "psycopg3" in blob
