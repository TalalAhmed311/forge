"""Phase 2 exit test: the engineer drives a planted failing test to green.

The model is a scripted MockProvider: first it writes the fix, then it claims
done. The loop must VERIFY (not trust) and only return ok once the real test
passes.
"""

from __future__ import annotations

from forge.agents.engineer import Engineer
from forge.memory.context_manager import GatheredContext
from forge.memory.tracker import Task
from forge.providers.base import Completion, Message
from forge.providers.mock import MockProvider
from forge.tools.factory import engineer_tools

FIXED_SOURCE = "def add(a, b):\n    return a + b\n"

TEST_CMD = "python3 -m pytest tests/test_calc.py -q"


def _write_fix(_history):
    return Completion(
        text="fixing the bug",
        tool_calls=[
            {
                "id": "c1",
                "name": "write_file",
                "arguments": {"path": "calc.py", "content": FIXED_SOURCE},
            }
        ],
    )


def _claim_done(_history):
    return Completion(text="done, add now returns a+b", tool_calls=[])


def test_engineer_makes_failing_test_pass(tool_ctx):
    provider = MockProvider(script=[_write_fix, _claim_done])
    engineer = Engineer(provider, engineer_tools(), max_inner_iters=5)
    task = Task(id="T1", title="make the failing test pass", test_command=TEST_CMD)

    result = engineer.run_task(task, GatheredContext(tier1="(tier1)"), tool_ctx)

    assert result.ok, f"expected pass; trace:\n" + "\n".join(result.trace)
    assert result.iterations == 2
    # The fix was actually written to disk.
    assert "a + b" in (tool_ctx.workspace and open(
        f"{tool_ctx.workspace}/calc.py").read())


def test_engineer_does_not_trust_premature_done(tool_ctx):
    """If the model claims done before fixing, verification fails and it loops."""

    provider = MockProvider(script=[_claim_done, _write_fix, _claim_done])
    engineer = Engineer(provider, engineer_tools(), max_inner_iters=5)
    task = Task(id="T1", title="fix add", test_command=TEST_CMD)

    result = engineer.run_task(task, GatheredContext(tier1="(tier1)"), tool_ctx)
    assert result.ok
    assert result.iterations == 3  # claim(fail) -> write -> claim(pass)


def test_engineer_hits_iteration_cap_and_escalates(tool_ctx):
    """A model that never fixes anything must cap out and escalate, not spin."""

    provider = MockProvider(script=[])  # always returns plain "done"
    engineer = Engineer(provider, engineer_tools(), max_inner_iters=3)
    task = Task(id="T1", title="fix add", test_command=TEST_CMD)

    result = engineer.run_task(task, GatheredContext(tier1="(tier1)"), tool_ctx)
    assert not result.ok
    assert result.escalate
    assert result.iterations == 3


def test_engineer_escalates_on_request(tool_ctx):
    def _escalate(_h):
        return Completion(
            text="",
            tool_calls=[
                {"id": "c1", "name": "escalate",
                 "arguments": {"question": "Which JSON lib should I use?"}}
            ],
        )

    provider = MockProvider(script=[_escalate])
    engineer = Engineer(provider, engineer_tools(), max_inner_iters=3)
    task = Task(id="T1", title="add config parser", test_command=TEST_CMD)

    result = engineer.run_task(task, GatheredContext(tier1="(tier1)"), tool_ctx)
    assert result.escalate
    assert "JSON" in result.question


def test_write_file_blocked_for_non_engineer(tool_ctx):
    tool_ctx.role = "architect"
    from forge.tools.fs import WriteFileTool

    res = WriteFileTool().run({"path": "x.py", "content": "x"}, tool_ctx)
    assert not res.ok
    assert "not permitted" in res.content
