"""Cross-session recall core: RRF fusion + the router aggregator (Stage 1 + 2)."""

from __future__ import annotations

from forge.memory.aggregator import Aggregator, Briefing, Candidate
from forge.memory.fusion import reciprocal_rank_fusion
from forge.providers.base import Completion
from forge.providers.mock import MockProvider


# -- Stage 1: RRF ----------------------------------------------------------- #


def test_rrf_rewards_agreement_across_pathways():
    summary = ["a", "b", "c"]
    bm25 = ["b", "a", "d"]
    vector = ["a", "e", "b"]
    fused = dict(reciprocal_rank_fusion([summary, bm25, vector]))
    # 'a' is top-or-near in all three → should win; 'b' second.
    order = [d for d, _ in reciprocal_rank_fusion([summary, bm25, vector])]
    assert order[0] == "a"
    assert order[1] == "b"
    assert fused["a"] > fused["b"] > fused["d"]


def test_rrf_is_scale_free_and_unions():
    # Disjoint lists still merge; a doc appearing once still ranks.
    fused = reciprocal_rank_fusion([["x"], ["y"], ["z"]])
    assert {d for d, _ in fused} == {"x", "y", "z"}


def test_rrf_weights_favor_a_pathway():
    # Weight the third pathway heavily; its top doc should jump.
    lists = [["a", "b"], ["a", "b"], ["c", "a"]]
    base = dict(reciprocal_rank_fusion(lists))
    weighted = dict(reciprocal_rank_fusion(lists, weights=[1, 1, 10]))
    assert weighted["c"] > base["c"]


def test_rrf_top_n_caps_results():
    fused = reciprocal_rank_fusion([["a", "b", "c", "d", "e"]], top_n=3)
    assert len(fused) == 3


# -- Stage 2: aggregator ---------------------------------------------------- #


def _cands():
    return [
        Candidate("c12", "s3", "POST /auth/login returns {token}", kind="decision", score=0.9),
        Candidate("c4", "s3", "User model in app/models.py", kind="spec", score=0.7),
        Candidate("c9", "s1", "unrelated note about CSS spacing", kind="trace", score=0.3),
    ]


def test_aggregator_synthesizes_cited_briefing():
    router = MockProvider(script=[Completion(
        text="Reuse the auth API: POST /auth/login → {token} [s3:c12]; "
             "User model lives in app/models.py [s3:c4].")])
    briefing = Aggregator(router).aggregate("add token refresh endpoint", _cands())
    assert "[s3:c12]" in briefing.text
    assert set(briefing.cited) == {"s3:c12", "s3:c4"}     # only the cited ones
    assert "CROSS-SESSION MEMORY" in briefing.render()


def test_aggregator_handles_no_relevant_context():
    router = MockProvider(script=[Completion(text="(no relevant prior context)")])
    briefing = Aggregator(router).aggregate("totally new thing", _cands())
    assert briefing.text == ""
    assert briefing.render() == ""


def test_aggregator_heuristic_fallback_without_model():
    briefing = Aggregator(provider=None).aggregate("anything", _cands())
    # Falls back to the top summaries with citations, no model call.
    assert "[s3:c12]" in briefing.text
    assert briefing.cited


def test_aggregator_empty_candidates():
    assert Aggregator(MockProvider()).aggregate("q", []).text == ""
