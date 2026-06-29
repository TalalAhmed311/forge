"""Phase 7 — self-improvement. Mirrors the §10 sanity checklist:
  * engineer cannot write protected paths (eval isolation);
  * a lesson learned on run 1 is retrieved/applied on a later matching run;
  * a skill is promoted only after passing the regression gate, never inline;
  * skills/lessons are versioned + reversible (rollback);
  * with improve.enabled=false, behavior is exactly Phase 1-6.
"""

from __future__ import annotations

import json
import os
import textwrap

import pytest

from forge.config import load_config
from forge.improve.lessons import Lesson, LessonStore
from forge.improve.regression import RegressionGate
from forge.improve.skills import SkillCandidate, SkillLibrary
from forge.tools.base import ToolContext
from forge.tools.fs import WriteFileTool


# -- 7a: eval isolation ----------------------------------------------------- #


def _ctx(workspace, improve_enabled):
    cfg = load_config(overrides={
        "roles": {r: {"provider": "mock", "model": "m"}
                  for r in ("architect", "engineer", "router", "clarifier")},
        "improve": {"enabled": improve_enabled},
    })
    return ToolContext(workspace=str(workspace), config=cfg, role="engineer")


def test_engineer_cannot_write_protected_tests(tmp_path):
    os.makedirs(tmp_path / "tests", exist_ok=True)
    ctx = _ctx(tmp_path, improve_enabled=True)
    res = WriteFileTool().run(
        {"path": "tests/test_calc.py", "content": "def test_x(): assert True"}, ctx
    )
    assert not res.ok
    assert "protected" in res.content.lower()
    # The error is corrective: it tells the engineer to fix code, not the test.
    assert "do not" in res.content.lower()
    assert not (tmp_path / "tests" / "test_calc.py").exists()


def test_engineer_cannot_write_eval_dir(tmp_path):
    ctx = _ctx(tmp_path, improve_enabled=True)
    res = WriteFileTool().run(
        {"path": ".forge/eval/test_frozen.py", "content": "x"}, ctx
    )
    assert not res.ok and "protected" in res.content.lower()


def test_guard_off_when_improve_disabled(tmp_path):
    """§10: with improve disabled, the agent behaves exactly as Phase 1-6."""

    os.makedirs(tmp_path / "tests", exist_ok=True)
    ctx = _ctx(tmp_path, improve_enabled=False)
    res = WriteFileTool().run(
        {"path": "tests/test_calc.py", "content": "def test_x(): assert True"}, ctx
    )
    assert res.ok
    assert (tmp_path / "tests" / "test_calc.py").exists()


def test_non_test_writes_still_allowed_under_isolation(tmp_path):
    ctx = _ctx(tmp_path, improve_enabled=True)
    res = WriteFileTool().run({"path": "src/calc.py", "content": "x = 1"}, ctx)
    assert res.ok
    assert (tmp_path / "src" / "calc.py").exists()


# -- 7b: lessons ------------------------------------------------------------ #


def test_lesson_add_retrieve_and_render(tmp_path):
    store = LessonStore(str(tmp_path / "lessons.jsonl"))
    lid = store.add(Lesson(
        id="", trigger="build error mentions a missing migration",
        rule="run migrate before re-running the failing test",
    ))
    assert lid == "L0001"
    hits = store.retrieve("the test failed with a missing migration error")
    assert hits and hits[0].id == "L0001"
    assert "migrate" in hits[0].render()


def test_lesson_demotion_after_low_win_rate(tmp_path):
    store = LessonStore(str(tmp_path / "lessons.jsonl"),
                        demote_after_uses=4, demote_win_rate=0.5)
    store.add(Lesson(id="", trigger="flaky thing", rule="do the fix"))
    for _ in range(4):
        store.record_outcome("L0001", helped=False)
    assert store.active() == []          # demoted, not deleted
    assert len(store.all()) == 1


def test_lesson_learned_then_applied_on_later_run(tmp_path):
    """Run 1 records a lesson; run 2 with the same trigger retrieves it."""

    store = LessonStore(str(tmp_path / "lessons.jsonl"))
    # "Run 1" reflection produced this lesson.
    store.add(Lesson(
        id="", trigger="OperationalError on test about migrations",
        rule="apply pending migrations first",
    ))
    # "Run 2" gather() retrieves lessons matching the situation.
    applied = store.retrieve("task failed with OperationalError migrations")
    assert any("migrations" in l.rule for l in applied)


# -- 7c: skills + regression gate ------------------------------------------- #


SKILL_CODE = textwrap.dedent('''
    """Create a marker file. Contract: run(workspace, args) -> {"ok","output"}."""
    import os
    def run(workspace, args):
        path = os.path.join(workspace, args.get("name", "marker.txt"))
        with open(path, "w") as fh:
            fh.write("made by skill")
        return {"ok": True, "output": f"wrote {path}"}
''').strip()


def _seeded_gate(tmp_path):
    """A regression gate whose frozen suite is a trivially-passing test."""

    eval_dir = tmp_path / ".forge" / "eval"
    os.makedirs(eval_dir, exist_ok=True)
    (eval_dir / "test_frozen.py").write_text("def test_ok():\n    assert True\n")
    return RegressionGate(workspace=str(tmp_path), eval_dir=str(eval_dir))


def test_skill_not_promoted_without_gate_suite(tmp_path):
    """No frozen suite => gate refuses => nothing is promoted."""

    gate = RegressionGate(workspace=str(tmp_path),
                          eval_dir=str(tmp_path / ".forge" / "eval"))
    lib = SkillLibrary(skills_dir=str(tmp_path / ".forge" / "skills"), gate=gate)
    lib.propose(SkillCandidate(name="make_marker", code=SKILL_CODE))
    assert lib.staged() == ["make_marker"]
    assert lib.promote("make_marker") is False
    assert "make_marker" not in lib._index


def test_skill_promoted_only_after_gate_passes_then_callable(tmp_path):
    gate = _seeded_gate(tmp_path)
    lib = SkillLibrary(skills_dir=str(tmp_path / ".forge" / "skills"), gate=gate)
    lib.propose(SkillCandidate(
        name="make_marker", code=SKILL_CODE,
        when_to_use="create a marker file"))
    assert lib.promote("make_marker") is True
    assert "make_marker" in lib._index
    assert lib.staged() == []  # staging cleared after promotion

    # Exposed as a callable tool; running it actually performs the procedure.
    tools = lib.tool_objects()
    assert tools and tools[0].name == "make_marker"
    ctx = ToolContext(workspace=str(tmp_path), config=None, role="engineer")
    res = tools[0].run({"args": {"name": "out.txt"}}, ctx)
    assert res.ok
    assert (tmp_path / "out.txt").exists()
    assert "make_marker" in lib.catalog()


def test_skill_gate_rejects_broken_skill(tmp_path):
    gate = _seeded_gate(tmp_path)
    lib = SkillLibrary(skills_dir=str(tmp_path / ".forge" / "skills"), gate=gate)
    lib.propose(SkillCandidate(name="broken", code="this is not valid python ("))
    assert lib.promote("broken") is False  # validator (import) fails the gate


def test_skill_versioning_and_rollback(tmp_path):
    gate = _seeded_gate(tmp_path)
    lib = SkillLibrary(skills_dir=str(tmp_path / ".forge" / "skills"), gate=gate)
    lib.propose(SkillCandidate(name="make_marker", code=SKILL_CODE, when_to_use="v1"))
    assert lib.promote("make_marker")
    assert lib._index["make_marker"]["current_version"] == 1

    # Promote an improved v2.
    lib.propose(SkillCandidate(name="make_marker", code=SKILL_CODE, when_to_use="v2"))
    assert lib.promote("make_marker")
    assert lib._index["make_marker"]["current_version"] == 2
    # Prior version file is kept (never overwritten).
    assert os.path.isfile(tmp_path / ".forge" / "skills" / "make_marker__v1.py")

    # Rollback restores the prior version (one-step undo).
    assert lib.rollback("make_marker") is True
    assert lib._index["make_marker"]["current_version"] == 1


def test_index_persists_across_instances(tmp_path):
    gate = _seeded_gate(tmp_path)
    skills_dir = str(tmp_path / ".forge" / "skills")
    lib = SkillLibrary(skills_dir=skills_dir, gate=gate)
    lib.propose(SkillCandidate(name="make_marker", code=SKILL_CODE))
    assert lib.promote("make_marker")
    # A fresh library reads the promoted skill from index.json.
    reopened = SkillLibrary(skills_dir=skills_dir, gate=gate)
    assert "make_marker" in reopened._index


# -- orchestrator-level integration ---------------------------------------- #


def _improve_orchestrator(tmp_path, architect_provider, engineer_provider):
    from forge.memory.tracker import Tracker
    from forge.orchestrator import Orchestrator
    from forge.project import ForgeProject
    from forge.providers.registry import Registry, inject_provider

    project = ForgeProject(root=str(tmp_path))
    project.ensure_dirs()
    config = load_config(overrides={
        "roles": {r: {"provider": "mock", "model": "m"}
                  for r in ("architect", "engineer", "router", "clarifier")},
        "improve": {"enabled": True, "skills": {"enabled": True}},
    })
    registry = Registry(config)
    inject_provider(registry, "architect", architect_provider)
    inject_provider(registry, "engineer", engineer_provider)
    Tracker(project.tracker_path).init_empty(project="demo")
    return Orchestrator(project, config, registry)


def test_reflect_records_lesson_and_gather_injects_it(tmp_path):
    """Full loop: a run produces a lesson via reflect; a later gather injects it."""

    import json as _json

    from forge.providers.base import Completion
    from forge.providers.mock import MockProvider
    from tests.test_orchestrator import _engineer_write, _engineer_done

    plan = {
        "goal": "make module a",
        "tasks": [{"id": "T1", "title": "create a.py",
                   "test_command": 'python3 -c "import a"'}],
    }
    lesson = {"lesson": {"trigger": "creating an importable module",
                         "rule": "write `value = 1` into the module"},
              "skill": None}
    # Architect provider serves: plan, then the reflection lesson.
    architect = MockProvider(script=[Completion(text=_json.dumps(plan)),
                                     Completion(text=_json.dumps(lesson))])
    engineer = MockProvider(script=[_engineer_write, _engineer_done])
    orch = _improve_orchestrator(tmp_path, architect, engineer)

    report = orch.run("create module a as an importable file please")
    assert report.status == "done", report.message

    # The lesson was persisted by reflect().
    assert os.path.isfile(tmp_path / ".forge" / "lessons.jsonl")
    assert orch.lesson_store.active()

    # A later gather on a matching situation injects the lesson as advisory text.
    gathered = orch.context_manager.gather("create an importable module", orch._window)
    blob = gathered.render()
    assert "LESSONS FROM PAST RUNS" in blob
    assert "value = 1" in blob


def test_forge_improve_promotes_staged_skill_via_gate(tmp_path):
    """`forge improve` path: seed eval, gate, promote a staged skill -> tool."""

    from forge.providers.mock import MockProvider

    orch = _improve_orchestrator(tmp_path, MockProvider(), MockProvider())
    # Seed the frozen suite from the project's tests dir.
    os.makedirs(tmp_path / "tests", exist_ok=True)
    (tmp_path / "tests" / "test_frozen.py").write_text("def test_ok():\n    assert True\n")

    orch.skill_library.propose(SkillCandidate(
        name="make_marker", code=SKILL_CODE, when_to_use="create a marker"))

    seeded = orch.seed_eval()
    assert "test_frozen.py" in seeded
    promoted = orch.promote_pending()
    assert ("make_marker", True) in promoted

    # A new orchestrator exposes the promoted skill as an engineer tool.
    from forge.memory.tracker import Tracker
    from forge.orchestrator import Orchestrator
    from forge.project import ForgeProject
    from forge.providers.registry import Registry, inject_provider

    project = ForgeProject(root=str(tmp_path))
    config = load_config(project.config_path, overrides={
        "roles": {r: {"provider": "mock", "model": "m"}
                  for r in ("architect", "engineer", "router", "clarifier")},
        "improve": {"enabled": True},
    })
    registry = Registry(config)
    inject_provider(registry, "architect", MockProvider())
    inject_provider(registry, "engineer", MockProvider())
    orch2 = Orchestrator(project, config, registry)
    assert "make_marker" in orch2.engineer.tools.names()


# -- 7d: harness self-editing (human-gated) --------------------------------- #


def test_harness_writes_reviewable_proposal_and_changes_nothing(tmp_path):
    from forge.improve.harness import HarnessAnalyzer

    logs = tmp_path / "logs"
    os.makedirs(logs, exist_ok=True)
    # Two traces showing the same recurring failure.
    for i in range(2):
        (logs / f"run{i}.log").write_text(
            "ASSISTANT calls: run_command\n"
            "TOOL run_command -> ERR: ModuleNotFoundError: no module named widget\n"
        )
    engineer_prompt = (tmp_path / "engineer.md")
    engineer_prompt.write_text("ORIGINAL PROMPT")

    analyzer = HarnessAnalyzer(logs_dir=str(logs),
                               proposals_dir=str(tmp_path / "proposals"), gate=None)
    proposals = analyzer.propose(min_occurrences=2)
    written = analyzer.write_proposals(proposals)

    assert proposals and written                      # a reviewable proposal exists
    assert "ModuleNotFoundError" in proposals[0].rationale
    # Nothing was auto-applied: the prompt file is untouched.
    assert engineer_prompt.read_text() == "ORIGINAL PROMPT"
    assert os.path.isfile(written[0])
