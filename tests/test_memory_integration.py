"""Memory backends fall back gracefully, and the orchestrator persists + recalls
completed tasks across sessions (in-memory path, no live services needed)."""

from __future__ import annotations

import json

from forge.config import load_config
from forge.memory.factory import build_memory
from forge.memory.longterm import InMemoryLongTermStore
from forge.memory.events import InMemoryEpisodicLog
from forge.memory.tracker import Tracker
from forge.orchestrator import Orchestrator
from forge.project import ForgeProject
from forge.providers.base import Completion
from forge.providers.mock import MockProvider
from forge.providers.registry import Registry, inject_provider
from tests.test_orchestrator import _engineer_write, _engineer_done


def _cfg(long_term=True):
    return load_config(overrides={
        "roles": {r: {"provider": "mock", "model": "m"}
                  for r in ("architect", "engineer", "frontend_engineer", "router", "clarifier")},
        # hashing embedder so no network; in-memory stores since no services.
        "memory": {"long_term": long_term, "embedder": "hashing",
                   "redis_url": "redis://127.0.0.1:1/0",  # unreachable on purpose
                   "pg_dsn": "postgresql://x@127.0.0.1:1/x"},
    })


def test_factory_falls_back_to_in_memory(tmp_path):
    bundle = build_memory(_cfg(long_term=True), "proj", "s1")
    assert isinstance(bundle.episodic, InMemoryEpisodicLog)
    assert isinstance(bundle.long_term, InMemoryLongTermStore)
    assert bundle.recall is not None
    # Notes explain the fallback rather than crashing.
    assert any("in-memory" in n for n in bundle.notes)


def test_long_term_disabled_means_no_recall():
    bundle = build_memory(_cfg(long_term=False), "proj", "s1")
    assert bundle.recall is None


def _orchestrator(tmp_path, cfg, engineer_provider):
    project = ForgeProject(root=str(tmp_path)); project.ensure_dirs()
    registry = Registry(cfg)
    inject_provider(registry, "architect", MockProvider(script=[Completion(text=json.dumps(PLAN))]))
    inject_provider(registry, "engineer", engineer_provider)
    Tracker(project.tracker_path).init_empty(project="demo")
    return Orchestrator(project, cfg, registry)


PLAN = {
    "goal": "one importable module",
    "tasks": [{"id": "T1", "title": "create authmod.py with login",
               "test_command": 'python3 -c "import authmod"', "surface": "backend"}],
}


def test_completed_task_is_persisted_to_long_term(tmp_path):
    cfg = _cfg(long_term=True)
    # Engineer creates authmod.py then declares done.
    def _write(history):
        return Completion(text="creating", tool_calls=[{"id": "c", "name": "write_file",
            "arguments": {"path": "authmod.py", "content": "def login():\n    return True\n"}}])
    eng = MockProvider(script=[_write, _engineer_done])
    orch = _orchestrator(tmp_path, cfg, eng)

    report = orch.run("create authmod with a login function as an importable module")
    assert report.status == "done", report.message

    # The completed task landed in long-term memory…
    doc = orch.long_term_store.get("", "S1-T1")
    assert doc is not None
    # …as a DISTILLED CARD, not the raw transcript.
    assert "Task S1-T1" in doc.content and "Files: authmod.py" in doc.content
    assert "iteration" not in doc.content and "ASSISTANT" not in doc.content
    # …and is retrievable for a future related task (appears in a pathway).
    lists = orch.long_term_store.search("login authmod function", project=orch.project_name)
    assert any("S1-T1" in lists[p] for p in lists)

    # Continuous short-term writes happened DURING the run (not just at the end).
    types = {e.type for e in orch.episodic_log.all()}
    assert "tool_result" in types     # the write_file event
    assert "test_result" in types     # the passing test
    assert "summary" in types         # the completion summary

    # And the cross-session search tool is wired onto the engineers.
    assert "search_memory" in orch.engineer.tools.names()


def test_card_summary_uses_cheap_model(tmp_path):
    """The completion card carries a model-written summary (clarifier role),
    on top of the deterministic facts."""

    # Distinct model names per role so the provider cache doesn't collide.
    cfg = load_config(overrides={
        "roles": {
            "architect": {"provider": "mock", "model": "arch"},
            "engineer": {"provider": "mock", "model": "eng"},
            "frontend_engineer": {"provider": "mock", "model": "fe"},
            "router": {"provider": "mock", "model": "rt"},
            "clarifier": {"provider": "mock", "model": "clar"},
        },
        "memory": {"long_term": True, "embedder": "hashing",
                   "redis_url": "redis://127.0.0.1:1/0",
                   "pg_dsn": "postgresql://x@127.0.0.1:1/x"},
    })
    project = ForgeProject(root=str(tmp_path)); project.ensure_dirs()
    registry = Registry(cfg)
    inject_provider(registry, "architect", MockProvider(script=[Completion(text=json.dumps(PLAN))]))

    def _write(history):
        return Completion(text="creating", tool_calls=[{"id": "c", "name": "write_file",
            "arguments": {"path": "authmod.py", "content": "def login():\n    return True\n"}}])
    inject_provider(registry, "engineer", MockProvider(script=[_write, _engineer_done]))
    # The clarifier is the card summarizer; the prompt is unambiguous so clarity
    # doesn't consume this — the distillation step does.
    inject_provider(registry, "clarifier", MockProvider(script=[
        Completion(text="Added authmod.login() in authmod.py so the module is importable.")]))
    Tracker(project.tracker_path).init_empty(project="demo")
    orch = Orchestrator(project, cfg, registry)

    assert orch.run("create authmod with a login function as an importable module").status == "done"
    doc = orch.long_term_store.get("", "S1-T1")
    assert "authmod.login()" in doc.summary            # model summary is the indexed one-liner
    assert "authmod.login()" in doc.content            # …and heads the card
    assert "Files: authmod.py" in doc.content          # deterministic facts still there


def test_card_falls_back_without_summarizer(tmp_path):
    # No summarizer set → deterministic fallback (engineer's own summary / title).
    from forge.memory.tracker import Task

    class _R:
        pass
    orch = _orchestrator(tmp_path, _cfg(long_term=True), MockProvider())
    orch._card_summarizer = None
    r = _R(); r.summary = "did the thing"; r.trace = ["TASK T1"]; r.log_path = ""
    assert orch._card_summary(Task("T1", "t", "c"), r, "facts") == "did the thing"


def test_memory_injected_once_at_task_start(tmp_path):
    """_inject_memory fills the cross-session + this-session blocks of context."""

    from forge.memory.context_manager import GatheredContext
    from forge.memory.events import EpisodicEvent
    from forge.memory.tracker import Task

    orch = _orchestrator(tmp_path, _cfg(long_term=True), MockProvider())
    # Simulate that the backend already produced an endpoint this session.
    orch.episodic_log.append(EpisodicEvent(
        orch.session_id, "engineer", "tool_result",
        "wrote app/auth.py with POST /auth/login", task_id="T1"))

    gathered = GatheredContext(tier1="(tracker)")
    orch._inject_memory(gathered, Task("T2", "build the login screen", "c", surface="frontend"))

    blob = gathered.render()
    assert "THIS SESSION SO FAR" in blob
    assert "POST /auth/login" in blob            # the prior work is injected
