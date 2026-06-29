"""`forge reset` clears project state but preserves config.yaml."""

from __future__ import annotations

import os

from forge.cli import reset_project
from forge.memory.tracker import Task, Tracker
from forge.project import ForgeProject


def test_reset_keeps_config_and_clears_state(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()

    # Seed state across several .forge artifacts.
    with open(project.config_path, "w") as fh:
        fh.write("roles:\n  engineer: { provider: openai, model: gpt-4o }\n")
    tracker = Tracker(project.tracker_path)
    tracker.init_empty(project="demo")
    tracker.add_tasks([Task("T1", "do x", "c1")])
    os.makedirs(project.specs_dir, exist_ok=True)
    open(os.path.join(project.specs_dir, "overview.md"), "w").close()
    os.makedirs(project.logs_dir, exist_ok=True)
    open(os.path.join(project.logs_dir, "run.log"), "w").close()
    with open(project.lessons_path, "w") as fh:
        fh.write('{"id":"L1"}\n')

    removed = reset_project(project)

    # config.yaml survived, with its contents intact.
    assert os.path.isfile(project.config_path)
    assert "gpt-4o" in open(project.config_path).read()
    assert "config.yaml" not in removed

    # State is gone / reset.
    assert not os.path.isfile(os.path.join(project.specs_dir, "overview.md"))
    assert not os.path.isfile(os.path.join(project.logs_dir, "run.log"))
    assert not os.path.isfile(project.lessons_path)
    # Tracker is back to an empty project (no tasks).
    assert Tracker(project.tracker_path).read().tasks == []


def test_reset_on_fresh_project_is_safe(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    # No config, minimal state — reset shouldn't raise and recreates the tracker.
    reset_project(project)
    assert os.path.isfile(project.tracker_path)
