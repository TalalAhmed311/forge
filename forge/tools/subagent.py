"""spawn_subagent — run a nested agent on a scoped task and return its result.

The handler is injected by the caller (the REPL builds a fresh AgentLoop that
shares the provider/tools/permissions but has its own history and NO further
spawn tool, so recursion is bounded). Useful for fan-out work (e.g. "investigate
X" or "implement Y") without polluting the main conversation's context.
"""

from __future__ import annotations

from typing import Callable

from forge.tools.base import Tool, ToolContext, ToolResult


class SpawnSubagentTool(Tool):
    name = "spawn_subagent"
    description = (
        "Delegate a self-contained sub-task to a fresh agent and get back its final "
        "report. Give a complete, standalone description (the subagent does not see "
        "this conversation). Good for focused investigation or a bounded build step."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "self-contained task description"},
        },
        "required": ["task"],
    }

    def __init__(self, handler: Callable[[str], str]) -> None:
        self._handler = handler

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        task = (args.get("task") or "").strip()
        if not task:
            return ToolResult(ok=False, content="task is required")
        try:
            report = self._handler(task)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, content=f"subagent failed: {type(exc).__name__}: {exc}")
        return ToolResult(ok=True, content=report or "(subagent returned no output)")
