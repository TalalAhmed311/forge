"""Short-term episodic log + long-term store + cross-session recall pipeline."""

from __future__ import annotations

from forge.memory.events import EpisodicEvent, InMemoryEpisodicLog
from forge.memory.longterm import Document, InMemoryLongTermStore
from forge.memory.recall import CrossSessionRecall
from forge.memory.episodic import HashingEmbedder
from forge.providers.base import Completion
from forge.providers.mock import MockProvider


# -- short-term episodic log ------------------------------------------------ #


def test_episodic_log_is_ordered_and_taggable():
    log = InMemoryEpisodicLog()
    log.append(EpisodicEvent("s1", "architect", "plan", "planned 3 tasks"))
    log.append(EpisodicEvent("s1", "engineer", "tool_result", "wrote api.py", task_id="T1"))
    log.append(EpisodicEvent("s1", "engineer", "test_result", "FAIL: import", task_id="T4"))
    log.append(EpisodicEvent("s1", "frontend_engineer", "tool_result", "built screen", task_id="T4"))

    # Ordered with monotonic seq.
    assert [e.seq for e in log.all()] == [0, 1, 2, 3]
    # Filter the precise slice for a handoff: what happened on T4.
    t4 = log.by_task("T4")
    assert {e.agent for e in t4} == {"engineer", "frontend_engineer"}
    assert log.by_task("T4", types=("test_result",))[0].content.startswith("FAIL")
    # Per-agent view.
    assert len(log.by_agent("engineer")) == 2
    # Tail.
    assert log.tail(2)[-1].agent == "frontend_engineer"


def test_event_json_roundtrip():
    e = EpisodicEvent("s1", "engineer", "decision", "chose JWT", task_id="T3", meta={"x": 1})
    assert EpisodicEvent.from_json(e.to_json()) == e


# -- long-term store -------------------------------------------------------- #


def _seed_store():
    store = InMemoryLongTermStore(embedder=HashingEmbedder())
    store.add_document(Document("s3", "c12", "POST /auth/login returns a jwt token",
                                summary="auth login endpoint returns jwt token",
                                kind="decision", project="chatapp"))
    store.add_document(Document("s3", "c4", "class User in app/models.py with id, name",
                                summary="User model fields", kind="spec", project="chatapp"))
    store.add_document(Document("s1", "c9", "we picked a calm neutral color palette",
                                summary="UI color palette", kind="trace", project="chatapp"))
    return store


def test_get_is_scoped_by_project():
    """doc_id is unique only within a project; get(project=...) must not resolve
    a same-named doc from another project (regression: recall leaked across projects)."""

    store = InMemoryLongTermStore(embedder=HashingEmbedder())
    store.add_document(Document("S1", "S1-T1", "todo: add()/complete()",
                               summary="todo list methods", project="demo"))
    store.add_document(Document("S1", "S1-T1", "auth: register()/login()",
                               summary="auth functions", project="authproj"))
    assert store.get("", "S1-T1", project="demo").summary == "todo list methods"
    assert store.get("", "S1-T1", project="authproj").summary == "auth functions"


def test_longterm_search_returns_three_pathways():
    store = _seed_store()
    lists = store.search("jwt token auth login endpoint")
    assert set(lists) == {"summary", "fulltext", "vector"}
    # The auth doc should rank in at least one pathway.
    assert any("c12" in lists[p] for p in lists)


def test_longterm_exclude_session_and_project_filter():
    store = _seed_store()
    lists = store.search("auth login", exclude_session="s3")
    assert all("c12" not in lists[p] and "c4" not in lists[p] for p in lists)
    lists2 = store.search("auth login", project="nope")
    assert all(lists2[p] == [] for p in lists2)


# -- the full recall pipeline ----------------------------------------------- #


def test_recall_fuses_and_aggregates_with_router():
    store = _seed_store()
    router = MockProvider(script=[Completion(
        text="Prior session built auth: POST /auth/login returns a jwt token [s3:c12]; "
             "User model in app/models.py [s3:c4].")])
    recall = CrossSessionRecall(store, HashingEmbedder(), aggregator_provider=router)

    briefing = recall.recall("add a token refresh endpoint to the auth API",
                             project="chatapp")
    assert "[s3:c12]" in briefing.text
    assert "CROSS-SESSION MEMORY" in briefing.render()
    assert "s3:c12" in briefing.cited


def test_ollama_embedder_calls_local_api(monkeypatch):
    from forge.memory import embeddings as emb_mod
    from forge.memory.embeddings import OllamaEmbedder

    seen = {}

    def fake_post(url, payload, *a, **k):
        seen["url"] = url
        seen["payload"] = payload
        return {"embedding": [0.0] * 768}

    monkeypatch.setattr(emb_mod, "post_json", fake_post)
    e = OllamaEmbedder()
    vec = e.embed("hello")
    assert len(vec) == 768 and e.dim == 768
    assert seen["url"].endswith("/api/embeddings")
    assert seen["payload"] == {"model": "nomic-embed-text", "prompt": "hello"}


def test_recall_heuristic_without_router():
    store = _seed_store()
    recall = CrossSessionRecall(store, HashingEmbedder(), aggregator_provider=None)
    briefing = recall.recall("auth login jwt", project="chatapp")
    # No model: still returns cited summaries from the fused candidates.
    assert briefing.cited
    assert briefing.text
