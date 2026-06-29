"""Cross-session recall pipeline — the full Stage 1 + Stage 2 flow.

    query
      → store.search()         3 ranked lists (summary · fulltext · vector)
      → RRF fusion             deterministic top-N union          (fusion.py)
      → Aggregator             router writes a cited briefing      (aggregator.py)
      → Briefing               injected into the original model's context

Backend-agnostic: works the same over the in-memory or the pgvector store.
"""

from __future__ import annotations

from typing import Callable, Optional

from forge.memory.aggregator import Aggregator, Briefing, Candidate
from forge.memory.fusion import reciprocal_rank_fusion
from forge.memory.longterm import LongTermStore
from forge.providers.base import Provider

# Default pathway weights for RRF. Identifier/keyword matches (fulltext) are
# weighted a touch higher for code recall; summary carries intent.
DEFAULT_WEIGHTS = {"summary": 1.0, "fulltext": 1.2, "vector": 1.0}


class CrossSessionRecall:
    def __init__(
        self,
        store: LongTermStore,
        embedder,
        aggregator_provider: Optional[Provider] = None,
        max_candidates: int = 8,
        weights: Optional[dict] = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.aggregator = Aggregator(aggregator_provider)
        self.max_candidates = max_candidates
        self.weights = weights or DEFAULT_WEIGHTS
        # Optional observability hook: set to a callable to receive a dict of the
        # full pipeline (query → per-pathway hits → RRF → briefing) per recall.
        self.trace_sink: Optional[Callable[[dict], None]] = None
        self._aggregator_provider = aggregator_provider

    def recall(
        self,
        query: str,
        project: Optional[str] = None,
        exclude_session: Optional[str] = None,
    ) -> Briefing:
        qv = self.embedder.embed(query)
        lists = self.store.search(
            query, qv, project=project, exclude_session=exclude_session
        )
        fused = reciprocal_rank_fusion(
            [lists["summary"], lists["fulltext"], lists["vector"]],
            weights=[self.weights["summary"], self.weights["fulltext"], self.weights["vector"]],
            top_n=self.max_candidates,
        )
        candidates = []
        for doc_id, score in fused:
            doc = self._lookup(doc_id, project)
            if doc is not None:
                candidates.append(
                    Candidate(doc_id=doc.doc_id, session_id=doc.session_id,
                              summary=doc.summary or doc.content[:160],
                              kind=doc.kind, score=score)
                )
        briefing = self.aggregator.aggregate(query, candidates)
        if self.trace_sink is not None:
            try:
                self.trace_sink({
                    "query": query,
                    "project": project,
                    "exclude_session": exclude_session,
                    "retrieved": {k: list(v) for k, v in lists.items()},
                    "fused": [{"doc_id": d, "score": round(s, 4)} for d, s in fused],
                    "candidates": [{"cite": c.cite(), "kind": c.kind,
                                    "summary": c.summary} for c in candidates],
                    "aggregated_by": (
                        getattr(self._aggregator_provider, "model", None)
                        or (type(self._aggregator_provider).__name__
                            if self._aggregator_provider else "heuristic")
                    ),
                    "briefing": briefing.text,
                    "cited": briefing.cited,
                })
            except Exception:
                pass  # tracing must never break recall
        return briefing

    def _lookup(self, doc_id: str, project: Optional[str]):
        # Resolve a fused doc_id back to a Document. Session is unknown here, but
        # doc_id is unique only WITHIN a project, so scope by project — otherwise
        # another project's same-named doc (e.g. its own S1-T2) can be returned.
        return self.store.get(session_id="", doc_id=doc_id, project=project)
