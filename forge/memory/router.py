"""Multi-pathway routing (Section 8.3) — keep all three pathways.

Given a query, activate candidate chunks via the UNION of three orthogonal
pathways:
  * P_global — match the query against chunk SUMMARIES (broad intent; primary).
  * P_vec    — vector similarity against RAW-chunk embeddings (nuance failsafe).
  * P_kw     — BM25 keyword match against RAW text (exact identifiers/names).
Activate the union, then cap at `max_activated_chunks` (paper's saturation point).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set

from forge.memory.episodic import ChunkStore, cosine, tokenize


@dataclass
class Activation:
    chunk_id: str
    pathways: Set[str] = field(default_factory=set)
    score: float = 0.0


class Router:
    def __init__(self, store: ChunkStore, max_activated_chunks: int = 8) -> None:
        self.store = store
        self.max_activated = max_activated_chunks

    def route(self, query: str) -> list[Activation]:
        if not self.store.chunks:
            return []

        q_vec = self.store.embedder.embed(query)
        q_tokens = set(tokenize(query))
        acts: dict[str, Activation] = {}

        def bump(chunk_id: str, pathway: str, score: float) -> None:
            a = acts.setdefault(chunk_id, Activation(chunk_id))
            a.pathways.add(pathway)
            a.score = max(a.score, score)

        # P_global: query vs summaries (lexical overlap + summary embedding).
        for c in self.store.chunks:
            s_tokens = set(tokenize(c.summary))
            overlap = len(q_tokens & s_tokens) / (len(q_tokens) or 1)
            sim = cosine(q_vec, self.store.embedder.embed(c.summary))
            score = 0.6 * overlap + 0.4 * sim
            if score > 0.05:
                bump(c.id, "global", score)

        # P_vec: query vs raw-chunk embeddings.
        for c in self.store.chunks:
            sim = cosine(q_vec, c.vec)
            if sim > 0.05:
                bump(c.id, "vec", sim)

        # P_kw: BM25 over raw text.
        for idx, c in enumerate(self.store.chunks):
            score = self.store.bm25.score(query, idx)
            if score > 0.0:
                bump(c.id, "kw", score)

        ranked = sorted(
            acts.values(),
            key=lambda a: (len(a.pathways), a.score),
            reverse=True,
        )
        return ranked[: self.max_activated]
