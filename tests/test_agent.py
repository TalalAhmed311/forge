"""Tests for the interactive agent: tools, permissions, checkpoints, loop."""

from __future__ import annotations

import os

import pytest

from forge.agent.checkpoint import CheckpointManager
from forge.agent.loop import AgentLoop
from forge.agent.permissions import PermissionManager
from forge.agent.session import expand_mentions, load_project_doc
from forge.config import load_config
from forge.providers.base import Completion, Message
from forge.providers.mock import MockProvider
from forge.tools.base import ToolContext
from forge.tools.factory import agent_tools
from forge.tools.fs import EditFileTool
from forge.tools.search import GlobTool, GrepTool
from forge.tools.todo import TodoStore, TodoWriteTool


def _ctx(tmp_path, checkpoint=None):
    return ToolContext(workspace=str(tmp_path), config=load_config(), role="agent",
                       checkpoint=checkpoint)


# -- edit_file --------------------------------------------------------------- #

def test_edit_file_replaces_unique(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n")
    res = EditFileTool().run(
        {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3"}, _ctx(tmp_path))
    assert res.ok
    assert f.read_text() == "x = 1\ny = 3\n"


def test_edit_file_rejects_non_unique(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v = 1\nv = 1\n")
    res = EditFileTool().run(
        {"path": "a.py", "old_string": "v = 1", "new_string": "v = 2"}, _ctx(tmp_path))
    assert not res.ok and "not unique" in res.content
    res2 = EditFileTool().run(
        {"path": "a.py", "old_string": "v = 1", "new_string": "v = 2",
         "replace_all": True}, _ctx(tmp_path))
    assert res2.ok and f.read_text() == "v = 2\nv = 2\n"


def test_edit_file_missing_and_not_found(tmp_path):
    assert not EditFileTool().run(
        {"path": "nope.py", "old_string": "a", "new_string": "b"}, _ctx(tmp_path)).ok
    (tmp_path / "b.py").write_text("hello\n")
    r = EditFileTool().run(
        {"path": "b.py", "old_string": "zzz", "new_string": "b"}, _ctx(tmp_path))
    assert not r.ok and "not found" in r.content


def test_edit_file_role_gate(tmp_path):
    (tmp_path / "c.py").write_text("a\n")
    ctx = ToolContext(workspace=str(tmp_path), config=load_config(), role="architect")
    assert not EditFileTool().run(
        {"path": "c.py", "old_string": "a", "new_string": "b"}, ctx).ok


# -- grep / glob ------------------------------------------------------------- #

def test_grep_and_glob(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "readme.md").write_text("nothing here\n")
    g = GrepTool().run({"pattern": "def hello", "include": "*.py"}, _ctx(tmp_path))
    assert g.ok and "m.py" in g.content
    gl = GlobTool().run({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert "pkg/m.py" in gl.content


# -- todo -------------------------------------------------------------------- #

def test_todo_store_and_tool(tmp_path):
    store = TodoStore()
    res = TodoWriteTool(store).run({"todos": [
        {"content": "build", "status": "in_progress"},
        {"content": "test", "status": "pending"},
        {"bad": "ignored"},
    ]}, _ctx(tmp_path))
    assert res.ok and len(store.items) == 2
    assert "[~] build" in store.render() and "[ ] test" in store.render()


# -- permissions ------------------------------------------------------------- #

def _call(name, **args):
    return {"id": "1", "name": name, "arguments": args}


def test_permission_modes():
    # read-only tool always allowed regardless of mode
    assert PermissionManager("plan").check(_call("read_file", path="x")).allowed
    # plan mode blocks writes
    assert not PermissionManager("plan").check(_call("write_file", path="x", content="y")).allowed
    # bypass allows everything
    assert PermissionManager("bypass").check(_call("run_command", cmd="rm -rf /")).allowed
    # acceptEdits auto-allows edits but still gates commands
    pm = PermissionManager("acceptEdits", approver=lambda *_: "no")
    assert pm.check(_call("edit_file", path="x", old_string="a", new_string="b")).allowed
    assert not pm.check(_call("run_command", cmd="ls")).allowed


def test_permission_default_asks_and_remembers_always():
    calls = {"n": 0}

    def approver(name, args, preview):
        calls["n"] += 1
        return "always"

    pm = PermissionManager("default", approver=approver)
    assert pm.check(_call("run_command", cmd="pytest -q")).allowed
    assert pm.check(_call("run_command", cmd="pytest tests/")).allowed  # same prefix
    assert calls["n"] == 1  # second 'pytest' auto-allowed by remembered 'always'


def test_permission_allowlist():
    pm = PermissionManager("default", approver=lambda *_: "no",
                           command_allowlist=["ls"])
    assert pm.check(_call("run_command", cmd="ls -la")).allowed
    assert not pm.check(_call("run_command", cmd="rm x")).allowed


# -- checkpoint -------------------------------------------------------------- #

def test_checkpoint_undo_restores_and_deletes(tmp_path):
    cp = CheckpointManager()
    cp.checkpoint()
    existing = tmp_path / "keep.txt"
    existing.write_text("original")
    cp.snapshot(str(existing))
    existing.write_text("modified")
    created = tmp_path / "new.txt"
    cp.snapshot(str(created))   # snapshot BEFORE creation -> records "did not exist"
    created.write_text("created")
    assert cp.has_changes()
    cp.undo()
    assert existing.read_text() == "original"   # reverted
    assert not created.exists()                 # creation undone


# -- agent loop -------------------------------------------------------------- #

def _loop(tmp_path, provider, mode="bypass", **kw):
    cp = CheckpointManager()
    tools = agent_tools()
    ctx = ToolContext(workspace=str(tmp_path), config=load_config(), role="agent",
                      checkpoint=cp)
    return AgentLoop(provider=provider, tools=tools, tool_ctx=ctx,
                     permissions=PermissionManager(mode), system_prompt="sys", **kw)


def test_agent_loop_executes_tools_then_stops(tmp_path):
    provider = MockProvider(script=[
        Completion(text="creating", tool_calls=[
            _call("write_file", path="hello.txt", content="hi there")]),
        Completion(text="done — wrote hello.txt", tool_calls=[]),
    ])
    loop = _loop(tmp_path, provider, mode="bypass")
    out = loop.run_turn("create hello.txt")
    assert out == "done — wrote hello.txt"
    assert (tmp_path / "hello.txt").read_text() == "hi there"
    assert any(m.role == "tool" for m in loop.history)


def test_agent_loop_respects_permission_denial(tmp_path):
    provider = MockProvider(script=[
        Completion(text="", tool_calls=[
            _call("write_file", path="secret.txt", content="x")]),
        Completion(text="ok, skipped", tool_calls=[]),
    ])
    loop = _loop(tmp_path, provider, mode="default")  # default + no approver => deny
    events = []
    loop.on_event = lambda k, d: events.append(k)
    out = loop.run_turn("write secret")
    assert out == "ok, skipped"
    assert not (tmp_path / "secret.txt").exists()
    assert "permission_denied" in events


def test_agent_loop_compaction(tmp_path):
    provider = MockProvider(script=[Completion(text="hi", tool_calls=[])])
    provider.context_window = 80  # tiny budget to force compaction
    compactor = MockProvider(script=[Completion(text="EARLIER: did stuff")])
    loop = _loop(tmp_path, provider, mode="bypass", compactor=compactor)
    # Several prior turns of bulky content.
    for i in range(4):
        loop.history.append(Message("user", "u" * 200))
        loop.history.append(Message("assistant", "a" * 200))
    loop._maybe_compact()
    assert loop.history[1].content.startswith("[Summary of earlier turns]")
    assert "EARLIER" in loop.history[1].content


# -- session helpers --------------------------------------------------------- #

def test_expand_mentions(tmp_path):
    (tmp_path / "note.txt").write_text("secret content")
    out = expand_mentions("look at @note.txt please", str(tmp_path))
    assert "secret content" in out
    # non-existent mention left alone
    assert expand_mentions("see @ghost.txt", str(tmp_path)) == "see @ghost.txt"


def test_load_project_doc(tmp_path):
    assert load_project_doc(str(tmp_path)) == ""
    (tmp_path / "FORGE.md").write_text("Run with uvicorn.")
    doc = load_project_doc(str(tmp_path))
    assert "Project instructions (FORGE.md)" in doc and "uvicorn" in doc


# -- session (slash commands, undo) ------------------------------------------ #

def _session(tmp_path, mode="bypass"):
    from forge.agent.session import AgentSession
    from forge.config import ROLES
    from forge.providers.registry import Registry

    config = load_config(overrides={"roles": {
        r: {"provider": "mock", "model": "m"} for r in ROLES}})
    out = []
    sess = AgentSession(str(tmp_path), config, Registry(config), mode=mode,
                        sink=out.append)
    return sess, out


def test_session_slash_mode_and_tools(tmp_path):
    sess, out = _session(tmp_path)
    assert sess._slash("/mode plan") is False
    assert sess.permissions.mode == "plan"
    assert sess._slash("/mode nonsense") is False  # rejected, mode unchanged
    assert sess.permissions.mode == "plan"
    sess._slash("/tools")
    assert any("edit_file" in line for line in out)
    assert sess._slash("/quit") is True


def test_session_slash_undo(tmp_path):
    sess, out = _session(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("before")
    sess.checkpoint.checkpoint()
    sess.checkpoint.snapshot(str(f))
    f.write_text("after")
    sess._slash("/undo")
    assert f.read_text() == "before"
    assert any("reverted" in line for line in out)


# -- find_symbol (repo index) ------------------------------------------------ #

def test_find_symbol(tmp_path):
    from forge.tools.search import FindSymbolTool
    (tmp_path / "mod.py").write_text(
        "def parse_config(path):\n    return {}\n\n"
        "def main():\n    return parse_config('x')\n"
    )
    res = FindSymbolTool().run({"symbol": "parse_config"}, _ctx(tmp_path))
    assert res.ok
    assert "def mod.py:1" in res.content          # definition site
    assert "mod.py:5" in res.content              # call site in main()


# -- session persistence / resume -------------------------------------------- #

# -- guardrails: the engineer can't flail forever --------------------------- #

def test_engineer_no_progress_escapes_early(tmp_path):
    from forge.agents.engineer import Engineer
    from forge.memory.context_manager import GatheredContext
    from forge.memory.tracker import Task
    from forge.tools.factory import engineer_tools

    # Mock model never calls tools -> the loop verifies each turn. `false` always
    # fails identically, so the no-progress guard must bail well before the cap.
    eng = Engineer(MockProvider(script=[]), engineer_tools(), max_inner_iters=10,
                   no_progress_repeats=3, max_seconds=0)
    task = Task(id="T1", title="x", test_command="false")
    ctx = ToolContext(workspace=str(tmp_path), config=load_config(), role="engineer")
    res = eng.run_task(task, GatheredContext(tier1="x"), ctx)
    assert not res.ok and res.escalate
    assert res.reason == "no progress"
    assert res.iterations < 10  # escaped early, did not burn the whole cap


# -- integration: agent drives Forge subsystems ----------------------------- #

def test_session_registers_session_and_tracker(tmp_path):
    sess, _ = _session(tmp_path)
    assert sess.session_id.startswith("S")
    assert os.path.isfile(tmp_path / ".forge" / "sessions.json")
    assert sess.tracker.exists()
    # the session is recorded as started
    assert any(s.id == sess.session_id for s in sess.session_registry.all())


def test_plan_tool_invokes_architect_and_writes_specs(tmp_path):
    import json as _json
    from forge.providers.registry import inject_provider

    sess, _ = _session(tmp_path)
    plan = {
        "goal": "build a widget",
        "specs": {"overview.md": "o", "architecture.md": "a", "code_standards.md": "c"},
        "tasks": [{"id": "T1", "title": "scaffold widget",
                   "test_command": "pytest -q", "surface": "backend"}],
    }
    inject_provider(sess.registry, "architect",
                    MockProvider(script=[Completion(text=_json.dumps(plan))]))
    out = sess._run_plan("build a widget")
    assert "planned 1 task" in out
    sid = sess.session_id
    assert os.path.isfile(tmp_path / ".forge" / "specs" / sid / "overview.md")
    assert os.path.isfile(tmp_path / ".forge" / "specs" / sid / "architecture.md")
    assert any(t.title == "scaffold widget" for t in sess.tracker.read().tasks)


def test_delegate_tool_runs_test_gated_engineer(tmp_path):
    # engineer provider (mock) returns no tool calls -> loop verifies via the
    # test command. `true` exits 0, so the task passes on the first iteration.
    sess, _ = _session(tmp_path)
    out = sess._run_delegate("make it green", "true", None)
    assert "PASSED" in out


def test_reconcile_tracker_checks_off_passing_tasks(tmp_path):
    from forge.memory.tracker import Task
    sess, _ = _session(tmp_path)
    data = sess.tracker.read()
    data.tasks = [Task(id="S1-T1", title="ok", test_command="true"),
                  Task(id="S1-T2", title="nope", test_command="false")]
    sess.tracker.write(data)
    marked = sess._reconcile_tracker()
    assert marked == ["S1-T1"]
    done = {t.id: t.done for t in sess.tracker.read().tasks}
    assert done["S1-T1"] is True and done["S1-T2"] is False


def test_clarify_first_turn_asks_on_vague(tmp_path):
    import json as _json
    from forge.providers.registry import inject_provider

    sess, out = _session(tmp_path)
    inject_provider(sess.registry, "clarifier", MockProvider(script=[
        Completion(text=_json.dumps({"confident": False, "question": "which thing?"}))]))
    result = sess._clarify("fix the thing")
    assert result is None  # needs user -> turn deferred
    assert any("which thing?" in line for line in out)


def test_session_persist_and_resume(tmp_path):
    from forge.providers.base import Message

    sess, _ = _session(tmp_path)
    sess.loop.history.append(Message("user", "remember: the API key is in .env"))
    sess.loop.history.append(Message("assistant", "noted"))
    sess.todos.set([{"content": "ship it", "status": "in_progress"}])
    sess._save()
    assert os.path.isfile(sess.state_path)

    # A brand-new session with resume=True picks up the prior conversation.
    from forge.agent.session import AgentSession
    from forge.config import ROLES
    from forge.providers.registry import Registry
    config = load_config(overrides={"roles": {
        r: {"provider": "mock", "model": "m"} for r in ROLES}})
    out = []
    resumed = AgentSession(str(tmp_path), config, Registry(config),
                           mode="bypass", sink=out.append, resume=True)
    contents = [m.content for m in resumed.loop.history]
    assert any("the API key is in .env" in c for c in contents)
    assert resumed.loop.history[0].role == "system"   # fresh system prompt kept
    assert resumed.todos.items and resumed.todos.items[0]["content"] == "ship it"
    assert any("resumed session" in line for line in out)
