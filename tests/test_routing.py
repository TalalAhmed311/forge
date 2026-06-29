"""FE/BE routing: the architect tags task surfaces and the orchestrator sends each
task to the right senior engineer (backend vs frontend)."""

from __future__ import annotations

import json

from forge.config import load_config
from forge.memory.tracker import SURFACE_BACKEND, SURFACE_FRONTEND, Task, Tracker
from forge.orchestrator import Orchestrator
from forge.project import ForgeProject
from forge.providers.base import Completion
from forge.providers.mock import MockProvider
from forge.providers.registry import Registry, inject_provider


# -- tracker round-trips the surface tag ------------------------------------ #


def test_tracker_persists_surface_tag(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.add_tasks([
        Task("T1", "auth API", "c1", surface=SURFACE_BACKEND),
        Task("T2", "login screen", "c2", surface=SURFACE_FRONTEND),
    ])
    reparsed = {task.id: task.surface for task in Tracker(t.path).read().tasks}
    assert reparsed == {"T1": SURFACE_BACKEND, "T2": SURFACE_FRONTEND}
    # The tag is visible in the rendered line.
    assert "[FE]" in Tracker(t.path).read_text()
    assert "[BE]" in Tracker(t.path).read_text()


def test_untagged_task_defaults_to_backend(tmp_path):
    t = Tracker(str(tmp_path / "T.md"))
    t.init_empty(project="demo")
    t.add_tasks([Task("T1", "do a thing", "c1")])
    assert Tracker(t.path).next_task().surface == SURFACE_BACKEND


# -- architect tags tasks from its JSON plan -------------------------------- #


def test_architect_reads_surface_from_plan(tmp_path):
    from forge.agents.architect import Architect

    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    tracker = Tracker(project.tracker_path)
    tracker.init_empty(project="demo")
    plan = {
        "goal": "g",
        "tasks": [
            {"id": "T1", "title": "API", "test_command": "c1", "surface": "backend"},
            {"id": "T2", "title": "UI", "test_command": "c2", "surface": "frontend"},
            {"id": "T3", "title": "lib", "test_command": "c3"},  # unset -> backend
        ],
    }
    architect = Architect(MockProvider(script=[Completion(text=json.dumps(plan))]),
                          specs_dir=project.specs_dir)
    from forge.memory.context_manager import GatheredContext
    architect.plan("build it", GatheredContext(tier1="x"), tracker)

    surfaces = {t.id: t.surface for t in tracker.read().tasks}
    # ids are namespaced to the session (default S1) so they never collide.
    assert surfaces == {"S1-T1": SURFACE_BACKEND, "S1-T2": SURFACE_FRONTEND,
                        "S1-T3": SURFACE_BACKEND}


# -- the orchestrator routes to the correct engineer ------------------------ #


def _surface_marker(history) -> str:
    """The engineer prompt differs by persona; detect which one ran."""

    sys = history[0].content
    if "Senior UI/UX Engineer" in sys:
        return "frontend"
    if "Senior Software Engineer" in sys:
        return "backend"
    return "?"


def test_orchestrator_routes_fe_and_be_to_distinct_engineers(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    # Distinct model names so the two engineer roles resolve to separate provider
    # instances (the registry dedups identical provider+model).
    config = load_config(overrides={"roles": {
        "architect": {"provider": "mock", "model": "arch"},
        "engineer": {"provider": "mock", "model": "be"},
        "frontend_engineer": {"provider": "mock", "model": "fe"},
        "router": {"provider": "mock", "model": "r"},
        "clarifier": {"provider": "mock", "model": "c"},
    }})
    registry = Registry(config)

    plan = {
        "goal": "two tasks",
        "tasks": [
            {"id": "T1", "title": "create be.py", "test_command": 'python3 -c "import be"',
             "surface": "backend"},
            {"id": "T2", "title": "create fe.py", "test_command": 'python3 -c "import fe"',
             "surface": "frontend"},
        ],
    }
    inject_provider(registry, "architect", MockProvider(script=[Completion(text=json.dumps(plan))]))

    # Each engineer's mock records which persona it ran under and writes its file.
    seen = {"backend": [], "frontend": []}

    def make_engineer(kind):
        def _write(history):
            assert _surface_marker(history) == kind, f"{kind} engineer got wrong persona"
            fname = "be.py" if kind == "backend" else "fe.py"
            return Completion(text=f"{kind} writing", tool_calls=[
                {"id": "c", "name": "write_file",
                 "arguments": {"path": fname, "content": "x = 1\n"}}])

        def _done(history):
            seen[kind].append(history[1].content)
            return Completion(text="done", tool_calls=[])

        return MockProvider(script=[_write, _done])

    inject_provider(registry, "engineer", make_engineer("backend"))
    inject_provider(registry, "frontend_engineer", make_engineer("frontend"))
    Tracker(project.tracker_path).init_empty(project="demo")

    orch = Orchestrator(project, config, registry)
    report = orch.run("build a backend module and a frontend module")

    assert report.status == "done", report.message
    assert (tmp_path / "be.py").exists() and (tmp_path / "fe.py").exists()
    # Both engineers ran exactly once, each on its own task.
    assert len(seen["backend"]) == 1 and len(seen["frontend"]) == 1
    assert "create be.py" in seen["backend"][0]
    assert "create fe.py" in seen["frontend"][0]
