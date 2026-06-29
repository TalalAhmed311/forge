"""Capability tools that let the interactive agent drive Forge's heavyweight
subsystems — the architect (planning → specs + tracker) and the test-gated
engineer (autonomous task execution). The actual work is done by handlers the
AgentSession injects, so these tools stay thin and the subsystems aren't
duplicated.
"""

from __future__ import annotations

from typing import Callable, Optional

from forge.tools.base import Tool, ToolContext, ToolResult


class PlanTool(Tool):
    name = "plan"
    description = (
        "Plan a substantial piece of work: the architect writes specs "
        "(overview/architecture/code_standards) and an ordered, test-bearing task "
        "list into the project tracker (.forge/). Use for a new feature or app — "
        "NOT for a one-line fix or a question. After planning, work the tasks "
        "yourself or hand them to `delegate_task`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "requirement": {"type": "string",
                            "description": "what to build, in a sentence or two"},
        },
        "required": ["requirement"],
    }

    def __init__(self, handler: Callable[[str], str]) -> None:
        self._handler = handler

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        req = (args.get("requirement") or "").strip()
        if not req:
            return ToolResult(ok=False, content="requirement is required")
        try:
            return ToolResult(ok=True, content=self._handler(req))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, content=f"planning failed: {exc}")


class DelegateTaskTool(Tool):
    name = "delegate_task"
    description = (
        "Hand a well-scoped task to the autonomous engineer, which edits code and "
        "re-runs the given test command until it passes (or hits its cap). Provide "
        "a clear title and an EXACT test_command. Use it to drive a sub-task to "
        "green without doing each step yourself; you keep the conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "what the task should achieve"},
            "test_command": {"type": "string",
                             "description": "exact command that passes only when done"},
            "task_id": {"type": "string",
                        "description": "optional id of an existing tracker task"},
        },
        "required": ["title", "test_command"],
    }

    def __init__(self, handler: Callable[[str, str, Optional[str]], str]) -> None:
        self._handler = handler

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        title = (args.get("title") or "").strip()
        test_command = (args.get("test_command") or "").strip()
        if not title or not test_command:
            return ToolResult(ok=False, content="title and test_command are required")
        try:
            return ToolResult(ok=True,
                              content=self._handler(title, test_command, args.get("task_id")))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, content=f"delegation failed: {exc}")
