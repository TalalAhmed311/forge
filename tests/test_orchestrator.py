"""Phase 4 + 5 exit tests: architect plans a tracker, the two loops clear it,
and `resume` continues after a simulated kill."""

from __future__ import annotations

import json
import re

from forge.config import load_config
from forge.memory.tracker import Task, Tracker
from forge.orchestrator import Orchestrator
from forge.project import ForgeProject
from forge.providers.base import Completion
from forge.providers.mock import MockProvider
from forge.providers.registry import Registry, inject_provider

PLAN = {
    "goal": "build three importable modules",
    "specs": {"overview.md": "Three modules a, b, c, each importable."},
    "tasks": [
        {"id": "T1", "title": "create a.py", "test_command": 'python3 -c "import a"'},
        {"id": "T2", "title": "create b.py", "test_command": 'python3 -c "import b"'},
        {"id": "T3", "title": "create c.py", "test_command": 'python3 -c "import c"'},
    ],
}


def _latest_task_file(history) -> str:
    """Find which file the current task wants created.

    Scope the match to the engineer's "YOUR TASK:" line — the gathered tier-1
    context also lists every other task, so a whole-message scan would always
    match the first one.
    """

    for msg in reversed(history):
        m = re.search(r"YOUR TASK:.*?create (\w+\.py)", msg.content)
        if m:
            return m.group(1)
    return "unknown.py"


def _engineer_write(history):
    fname = _latest_task_file(history)
    return Completion(
        text=f"creating {fname}",
        tool_calls=[
            {"id": "c", "name": "write_file",
             "arguments": {"path": fname, "content": "value = 1\n"}}
        ],
    )


def _engineer_done(_history):
    return Completion(text="done", tool_calls=[])


def _mock_config():
    return load_config(overrides={"roles": {r: {"provider": "mock", "model": "m"}
                                            for r in ("architect", "engineer", "router", "clarifier")}})


def _orchestrator(tmp_path, architect_provider, engineer_provider):
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    config = _mock_config()
    registry = Registry(config)
    inject_provider(registry, "architect", architect_provider)
    inject_provider(registry, "engineer", engineer_provider)
    Tracker(project.tracker_path).init_empty(project="demo")
    return Orchestrator(project, config, registry)


def test_reporter_streams_progress(tmp_path):
    """A reporter receives live progress: planning, per-task, tool calls, result."""

    architect = MockProvider(script=[Completion(text=json.dumps(PLAN))])
    engineer = MockProvider(script=[
        _engineer_write, _engineer_done,
        _engineer_write, _engineer_done,
        _engineer_write, _engineer_done,
    ])
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    config = _mock_config()
    registry = Registry(config)
    inject_provider(registry, "architect", architect)
    inject_provider(registry, "engineer", engineer)
    Tracker(project.tracker_path).init_empty(project="demo")

    logs: list = []
    orch = Orchestrator(project, config, registry, reporter=logs.append)
    report = orch.run("build modules a, b and c as importable files")

    assert report.status == "done"
    blob = "\n".join(logs)
    assert "planning" in blob.lower()
    assert "Plan ready" in blob
    assert "S1-T1" in blob and "Senior Software Engineer" in blob
    assert "write_file" in blob          # tool call surfaced
    assert "tests passed" in blob        # verification surfaced
    assert "✓ S1-T1 done" in blob


def _run_collecting(tmp_path, verbosity):
    architect = MockProvider(script=[Completion(text=json.dumps(PLAN))])
    engineer = MockProvider(script=[_engineer_write, _engineer_done] * 3)
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    config = _mock_config()
    registry = Registry(config)
    inject_provider(registry, "architect", architect)
    inject_provider(registry, "engineer", engineer)
    Tracker(project.tracker_path).init_empty(project="demo")
    logs: list = []
    orch = Orchestrator(project, config, registry,
                        reporter=logs.append, verbosity=verbosity)
    assert orch.run("build modules a, b and c as importable files").status == "done"
    return "\n".join(logs)


def test_verbosity_gates_detail(tmp_path):
    # Level 1 (default): summary only — no `args:` dump, tool result, or task list.
    summary = _run_collecting(tmp_path / "v1", verbosity=1)
    assert "✓ S1-T1 done" in summary
    assert "args:" not in summary                # full args dump hidden
    assert "wrote" not in summary                # full tool result hidden
    assert "(test: python3" not in summary       # planned-task dump hidden

    # Level 2 (-v): full tool I/O and the planned-task list appear.
    verbose = _run_collecting(tmp_path / "v2", verbosity=2)
    assert "args:" in verbose                     # full args shown
    assert "wrote" in verbose                     # full tool result content shown
    assert "(test: python3" in verbose            # planned tasks listed


def test_empty_plan_reports_failure_not_fake_success(tmp_path):
    """An architect that returns 0 tasks must fail loudly, not say 'all done'."""

    # Architect returns goal + specs but NO tasks.
    bad_plan = {"goal": "g", "specs": {"overview.md": "x"}, "tasks": []}
    architect = MockProvider(script=[Completion(text=json.dumps(bad_plan))])
    project = ForgeProject(root=str(tmp_path)); project.ensure_dirs()
    config = _mock_config()
    registry = Registry(config)
    inject_provider(registry, "architect", architect)
    Tracker(project.tracker_path).init_empty(project="demo")
    orch = Orchestrator(project, config, registry)

    report = orch.run("build something importable and useful for tests")
    assert report.status == "failed"
    assert "no tasks" in report.message
    # The raw plan was saved for debugging.
    import os
    logs = os.path.join(project.logs_dir)
    assert any("architect-plan" in f for f in os.listdir(logs))


def test_full_run_clears_three_tasks(tmp_path):
    architect = MockProvider(script=[Completion(text=json.dumps(PLAN))])
    # 3 tasks * (write, done)
    engineer = MockProvider(script=[
        _engineer_write, _engineer_done,
        _engineer_write, _engineer_done,
        _engineer_write, _engineer_done,
    ])
    orch = _orchestrator(tmp_path, architect, engineer)

    report = orch.run("build modules a, b and c as importable files")
    assert report.status == "done", report.message
    assert report.completed == ["S1-T1", "S1-T2", "S1-T3"]
    for name in ("a.py", "b.py", "c.py"):
        assert (tmp_path / name).exists()


def test_resume_continues_after_kill(tmp_path):
    # Simulate a prior run that planned 3 tasks and finished only T1.
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    tracker = Tracker(project.tracker_path)
    tracker.init_empty(project="demo")
    tracker.add_tasks([Task(t["id"], t["title"], t["test_command"]) for t in PLAN["tasks"]])
    (tmp_path / "a.py").write_text("value = 1\n")
    tracker.mark_done("T1", "done earlier")

    # Fresh process: new orchestrator, resume reads the tracker and continues.
    config = _mock_config()
    registry = Registry(config)
    inject_provider(registry, "architect", MockProvider())
    engineer = MockProvider(script=[
        _engineer_write, _engineer_done,  # T2
        _engineer_write, _engineer_done,  # T3
    ])
    inject_provider(registry, "engineer", engineer)
    orch = Orchestrator(project, config, registry)

    report = orch.resume()
    assert report.status == "done", report.message
    assert report.completed == ["T2", "T3"]  # T1 was already done
    assert Tracker(project.tracker_path).next_task() is None


def test_run_escalation_routes_to_architect(tmp_path):
    """Engineer escalates T1; architect revises it so the loop can finish."""

    plan_one = {
        "goal": "one task",
        "tasks": [{"id": "T1", "title": "create a.py", "test_command": 'python3 -c "import a"'}],
    }
    revised = {
        "goal": "one task",
        "tasks": [{"id": "T1", "title": "create a.py", "test_command": 'python3 -c "import a"'}],
    }
    architect = MockProvider(script=[
        Completion(text=json.dumps(plan_one)),
        Completion(text=json.dumps(revised)),
    ])

    def _escalate(_h):
        return Completion(text="", tool_calls=[
            {"id": "c", "name": "escalate", "arguments": {"question": "need a decision"}}])

    engineer = MockProvider(script=[_escalate, _engineer_write, _engineer_done])
    orch = _orchestrator(tmp_path, architect, engineer)

    report = orch.run("create module a as an importable file please")
    assert report.status == "done", report.message
    assert (tmp_path / "a.py").exists()
