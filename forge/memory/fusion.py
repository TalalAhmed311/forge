"""Reciprocal Rank Fusion — combine the three retrieval pathways (Stage 1).

The long-term store returns three independent ranked lists for a query:
summary-match, full-text (BM25-ish), and vector similarity. Their raw scores are
on incomparable scales, so we fuse by RANK, not score. RRF is scale-free and the
standard for hybrid search:

    score(doc) = Σ_pathways  weight / (k + rank_in_that_pathway)

The router model then selects/synthesizes from the fused top-N (Stage 2,
`aggregator.py`); fusion itself is deterministic and model-free.
"""

from __future__ import annotations

from typing import Optional


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
    weights: Optional[list[float]] = None,
    top_n: Optional[int] = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id-lists into one ranking.

    `ranked_lists` is one best-first list of doc ids per pathway. `weights` lets
    you favor a pathway (e.g. weight code/identifier matches higher); defaults to
    equal. Returns (doc_id, fused_score) sorted best-first.
    """

    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights must match the number of ranked lists")

    scores: dict[str, float] = {}
    for lst, weight in zip(ranked_lists, weights):
        for rank, doc_id in enumerate(lst):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank + 1)

    fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return fused[:top_n] if top_n else fused
