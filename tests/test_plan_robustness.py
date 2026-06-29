"""Robustness of architect-plan parsing against weaker models' malformed JSON.

gpt-4o-mini (and other small models) intermittently emit a single intended plan
object with STRAY top-level `}` closers and spec files hoisted to top-level keys
instead of nested under "specs". Either defect used to lose the `tasks` array
(=> "architect produced no tasks") and all specs but `overview.md`. These tests
pin the two fixes: brace repair in extract_json, and top-level-`.md` tolerance in
the architect's plan applier.
"""

from __future__ import annotations

import os

from forge._jsonutil import extract_json
from forge.agents.architect import Architect
from forge.memory.context_manager import GatheredContext
from forge.memory.tracker import Tracker
from forge.project import ForgeProject
from forge.providers.mock import MockProvider
from forge.providers.base import Completion


# A faithful reduction of the real failing output: stray `},` closers after the
# top-level spec keys, with only overview.md nested under "specs".
MALFORMED_PLAN = (
    '{\n'
    '  "goal": "Build an auth library",\n'
    '  "specs": {\n'
    '    "overview.md": "# Overview\\nPitch and {braces} inside a string"\n'
    '  },\n'
    '  "architecture.md": "# Architecture\\nStack and | tables |",\n'
    '  },\n'
    '  "code_standards.md": "# Code Standards\\nRun: pytest -q",\n'
    '  },\n'
    '  "tasks": [\n'
    '    {"id": "T1", "title": "scaffold", "test_command": "pytest -q", "surface": "backend"},\n'
    '    {"id": "T2", "title": "login", "test_command": "pytest -q", "surface": "backend"}\n'
    '  ]\n'
    '}\n'
)


def test_extract_json_recovers_tasks_from_stray_closers():
    obj = extract_json(MALFORMED_PLAN)
    assert obj is not None
    assert len(obj["tasks"]) == 2
    assert {t["id"] for t in obj["tasks"]} == {"T1", "T2"}
    # The leaked spec keys survive too (as top-level keys here).
    assert "architecture.md" in obj and "code_standards.md" in obj


def test_extract_json_leaves_valid_json_untouched():
    good = '{"goal": "g", "specs": {"overview.md": "x"}, "tasks": [{"id": "T1"}]}'
    assert extract_json(good) == {
        "goal": "g", "specs": {"overview.md": "x"}, "tasks": [{"id": "T1"}]
    }


def test_architect_writes_all_specs_even_when_hoisted_to_top_level(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    tracker = Tracker(project.tracker_path)
    tracker.init_empty(project="demo")

    architect = Architect(
        MockProvider(script=[Completion(text=MALFORMED_PLAN)]),
        specs_dir=project.specs_dir,
    )
    result = architect.plan("build it", GatheredContext(tier1="x"), tracker,
                            session_id="S1")

    assert result.ok and result.num_tasks == 2
    session_specs = project.session_specs_dir("S1")
    written = sorted(os.listdir(session_specs))
    assert written == ["architecture.md", "code_standards.md", "overview.md"]
