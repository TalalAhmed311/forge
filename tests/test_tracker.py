"""Phase 4: tracker round-trips, atomic writes, and the public contract."""

from __future__ import annotations

from forge.memory.tracker import Task, Tracker


def test_init_and_read(tmp_path):
    t = Tracker(str(tmp_path / "PROJECT_TRACKER.md"))
    t.init_empty(project="demo", goal="build a thing")
    data = t.read()
    assert data.project == "demo"
    assert data.goal == "build a thing"
    assert data.tasks == []


def test_add_tasks_and_next(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.add_tasks([
        Task("T1", "skeleton", "pytest tests/test_a.py"),
        Task("T2", "config", "pytest tests/test_b.py"),
    ])
    nxt = t.next_task()
    assert nxt.id == "T1"
    assert nxt.test_command == "pytest tests/test_a.py"


def test_mark_done_advances_next(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.add_tasks([Task("T1", "a", "c1"), Task("T2", "b", "c2")])
    t.mark_done("T1", summary="done a")
    assert t.next_task().id == "T2"
    # Persisted across a fresh read (survives restart).
    assert Tracker(t.path).next_task().id == "T2"


def test_roundtrip_preserves_tasks(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.add_tasks([Task("T1", "implement auth middleware", "pytest tests/test_auth.py")])
    reparsed = Tracker(t.path).read()
    assert reparsed.tasks[0].title == "implement auth middleware"
    assert reparsed.tasks[0].test_command == "pytest tests/test_auth.py"


def test_facts_and_decisions(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.append_fact("config.load() returns Config", "forge/config.py:14")
    t.append_decision("T3: chose JWT over sessions")
    data = t.read()
    assert any("config.load()" in f for f in data.facts)
    assert any("JWT" in d for d in data.decisions)


def test_atomic_write_no_partial_file(tmp_path):
    """No temp file is left behind after a write."""

    import os

    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".tracker-")]
    assert leftovers == []
