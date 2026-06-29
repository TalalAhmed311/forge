"""Context manager interface + the simple pre-Phase-6 fallback (Section 8 intro).

The spec is explicit: do NOT build episodic memory first. "Until it exists, the
orchestrator simply loads tier-1 state + recent raw history that fits the
window." `SimpleContextManager` is exactly that. The full episodic engine
(`EpisodicContextManager`, Phase 6) implements the same interface, so the
orchestrator never changes when you upgrade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

# Rough chars-per-token used only for crude budgeting where a tokenizer is absent.
CHARS_PER_TOKEN = 4


@dataclass
class ChunkRef:
    """A retrieval hit shown to the model: a cheap summary plus an id to expand."""

    chunk_id: str
    summary: str
    pathway: str = "global"  # which routing pathway activated it (Section 8.3)


@dataclass
class GatheredContext:
    """What `gather` returns: tier-1 verbatim first, then tier-2 retrieved text."""

    tier1: str
    episodic_summaries: list[ChunkRef] = field(default_factory=list)
    expanded_raw: list[str] = field(default_factory=list)
    code_hits: list[str] = field(default_factory=list)
    # Phase 7: advisory lessons + the available-skills catalog (§3.3, §4.4).
    lessons: list[str] = field(default_factory=list)
    skills_catalog: str = ""
    # Memory injection (once, at task start): cross-session briefing (long-term)
    # + this-session slice of what other agents already did (short-term).
    cross_session: str = ""
    session_slice: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = ["# AUTHORITATIVE STATE (tier 1 — verbatim, trust this)\n" + self.tier1]
        if self.code_hits:
            parts.append("# CODE SYMBOL HITS\n" + "\n\n".join(self.code_hits))
        if self.episodic_summaries:
            lines = [f"[{c.chunk_id}] ({c.pathway}) {c.summary}" for c in self.episodic_summaries]
            parts.append(
                "# RELEVANT MEMORY (summaries — call fetch_raw_context(id) for detail)\n"
                + "\n".join(lines)
            )
        if self.expanded_raw:
            parts.append("# EXPANDED RAW CONTEXT\n" + "\n\n".join(self.expanded_raw))
        if self.session_slice:
            parts.append(
                "# THIS SESSION SO FAR (what the team already did — build on it)\n"
                + "\n".join(self.session_slice)
            )
        if self.cross_session:
            parts.append(self.cross_session)  # already a labeled CROSS-SESSION block
        if self.skills_catalog:
            parts.append(
                "# AVAILABLE SKILLS (pre-verified tools you may call)\n"
                + self.skills_catalog
            )
        if self.lessons:
            parts.append(
                "# LESSONS FROM PAST RUNS (advisory — apply if relevant, not ground "
                "truth)\n" + "\n".join(self.lessons)
            )
        return "\n\n".join(parts)


class ContextManager(ABC):
    """Owns tier-2 retrieval; always includes tier-1 verbatim first (Section 8.6)."""

    # Phase 7 hooks (set by the orchestrator when improve is enabled). Kept as
    # callables so the memory layer never imports the improve module.
    _lessons_hook = None        # Callable[[str], list[str]]
    _skills_catalog_hook = None  # Callable[[], str]

    def set_improve_hooks(self, lessons_hook=None, skills_catalog_hook=None) -> None:
        self._lessons_hook = lessons_hook
        self._skills_catalog_hook = skills_catalog_hook

    def _augment(self, gathered: "GatheredContext", query: str) -> "GatheredContext":
        """Inject lessons + skill catalog into a gathered context (§3.3, §4.4)."""

        if self._lessons_hook is not None:
            gathered.lessons = self._lessons_hook(query) or []
        if self._skills_catalog_hook is not None:
            gathered.skills_catalog = self._skills_catalog_hook() or ""
        return gathered

    @abstractmethod
    def append(self, turn: str) -> None:
        """Add an engineer turn / tool output / architect reasoning to episodic."""

    @abstractmethod
    def gather(self, query: str, window: int) -> GatheredContext:
        """Return tier-1 + retrieved tier-2 content under the `window` budget."""

    @abstractmethod
    def search(self, query: str) -> list[ChunkRef]:
        """Back the `search_context` tool (Section 8.3)."""

    @abstractmethod
    def fetch_raw(self, chunk_id: str) -> Optional[str]:
        """Back the `fetch_raw_context` tool (Section 8.4)."""


class SimpleContextManager(ContextManager):
    """Pre-Phase-6 fallback: tier-1 verbatim + the most recent raw turns that fit.

    No chunking, no embeddings, no routing — just keep the recent tail. This is a
    genuinely useful product (the spec says so) and is what runs until the
    episodic engine is enabled.
    """

    def __init__(self, tier1_provider: Callable[[], str], recent_turns: int = 12) -> None:
        self._tier1 = tier1_provider
        self._recent_turns = recent_turns
        self._turns: list[str] = []

    def append(self, turn: str) -> None:
        self._turns.append(turn)

    def gather(self, query: str, window: int) -> GatheredContext:
        tier1 = self._tier1()
        budget = max(0, window - len(tier1) // CHARS_PER_TOKEN)
        refs: list[ChunkRef] = []
        used = 0
        # Walk most-recent-first, keeping turns until the window budget is spent.
        for i in range(len(self._turns) - 1, -1, -1):
            text = self._turns[i]
            cost = len(text) // CHARS_PER_TOKEN
            if used + cost > budget:
                break
            used += cost
            refs.insert(0, ChunkRef(chunk_id=f"turn-{i}", summary=text, pathway="recent"))
            if len(refs) >= self._recent_turns:
                break
        return self._augment(GatheredContext(tier1=tier1, episodic_summaries=refs), query)

    def search(self, query: str) -> list[ChunkRef]:
        # Naive substring relevance over recent turns; the real router is Phase 6.
        q = query.lower()
        hits = [
            ChunkRef(chunk_id=f"turn-{i}", summary=t, pathway="recent")
            for i, t in enumerate(self._turns)
            if q in t.lower()
        ]
        return hits[-8:]

    def fetch_raw(self, chunk_id: str) -> Optional[str]:
        if chunk_id.startswith("turn-"):
            try:
                idx = int(chunk_id.split("-", 1)[1])
                return self._turns[idx]
            except (ValueError, IndexError):
                return None
        return None


class EpisodicContextManager(ContextManager):
    """Phase 6: chunked dual-representation memory + multi-pathway retrieval.

    Combines the episodic store (8.2), the multi-pathway router (8.3),
    summary-first disclosure with auto-expand and a live-raw cap (8.4), and the
    parallel code symbol index (8.5). `gather` always emits tier-1 verbatim first.
    """

    def __init__(
        self,
        tier1_provider: Callable[[], str],
        workspace: str,
        chunk_tokens: int = 8000,
        chunk_overlap: int = 800,
        max_activated_chunks: int = 8,
        max_live_raw: int = 3,
        summarizer: Optional[Callable[[str], str]] = None,
    ) -> None:
        # Imported here to keep the simple manager dependency-light at import time.
        from forge.memory.code_index import CodeIndex
        from forge.memory.episodic import ChunkStore
        from forge.memory.router import Router

        self._tier1 = tier1_provider
        self.workspace = workspace
        self.max_live_raw = max_live_raw
        self.store = ChunkStore(
            chunk_tokens=chunk_tokens, overlap=chunk_overlap, summarizer=summarizer
        )
        self.router = Router(self.store, max_activated_chunks=max_activated_chunks)
        self.code_index = CodeIndex.build(workspace)

    def reindex_code(self) -> None:
        """Rebuild the symbol index after the engineer has changed files."""

        from forge.memory.code_index import CodeIndex

        self.code_index = CodeIndex.build(self.workspace)

    def append(self, turn: str) -> None:
        self.store.append(turn)

    def gather(self, query: str, window: int) -> GatheredContext:
        from forge.memory.disclosure import disclose

        tier1 = self._tier1()
        # Seal the active buffer so the most recent work is retrievable too.
        self.store.flush()

        activations = self.router.route(query)
        disclosure = disclose(query, activations, self.store, self.max_live_raw)
        code_hits = self.code_index.query(query)

        budget = max(0, window - len(tier1) // CHARS_PER_TOKEN)
        summaries: list[ChunkRef] = []
        raws: list[str] = []
        used = 0

        # Raw expansions are the highest-signal; spend budget on them first.
        for cid, raw in disclosure.raws:
            cost = len(raw) // CHARS_PER_TOKEN
            if used + cost > budget:
                break
            raws.append(f"[{cid}]\n{raw}")
            used += cost
        for cid, summary, pathway in disclosure.summaries:
            cost = len(summary) // CHARS_PER_TOKEN
            if used + cost > budget:
                break
            summaries.append(ChunkRef(chunk_id=cid, summary=summary, pathway=pathway))
            used += cost

        return self._augment(
            GatheredContext(
                tier1=tier1,
                episodic_summaries=summaries,
                expanded_raw=raws,
                code_hits=code_hits,
            ),
            query,
        )

    def search(self, query: str) -> list[ChunkRef]:
        self.store.flush()
        activations = self.router.route(query)
        refs = []
        for act in activations:
            chunk = self.store.get(act.chunk_id)
            if chunk is not None:
                refs.append(
                    ChunkRef(
                        chunk_id=chunk.id,
                        summary=chunk.summary,
                        pathway="+".join(sorted(act.pathways)),
                    )
                )
        return refs

    def fetch_raw(self, chunk_id: str) -> Optional[str]:
        chunk = self.store.get(chunk_id)
        return chunk.raw if chunk is not None else None
