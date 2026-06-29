"""todo_write — the agent's visible task list (Claude-Code-style plan tracking).

The model maintains a checklist across a turn so multi-step work is legible to
the user. State lives in a `TodoStore` the agent owns and renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from forge.tools.base import Tool, ToolContext, ToolResult

_STATUSES = ("pending", "in_progress", "completed")


@dataclass
class TodoStore:
    items: List[dict] = field(default_factory=list)

    def set(self, items: List[dict]) -> None:
        cleaned = []
        for it in items:
            if not isinstance(it, dict) or not it.get("content"):
                continue
            status = it.get("status", "pending")
            cleaned.append({
                "content": str(it["content"]),
                "status": status if status in _STATUSES else "pending",
            })
        self.items = cleaned

    def render(self) -> str:
        if not self.items:
            return "(no todos)"
        glyph = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        return "\n".join(f"  {glyph[i['status']]} {i['content']}" for i in self.items)


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Record or update your task checklist for the current request. Pass the "
        "FULL list each time. Use it for any multi-step task so progress is visible. "
        "Each item: {content: str, status: 'pending'|'in_progress'|'completed'}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string",
                                   "enum": list(_STATUSES)},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def __init__(self, store: TodoStore) -> None:
        self.store = store

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.store.set(args.get("todos", []) or [])
        done = sum(1 for i in self.store.items if i["status"] == "completed")
        return ToolResult(
            ok=True,
            content=f"todos updated ({done}/{len(self.store.items)} done)\n"
                    + self.store.render(),
            meta={"todos": self.store.items},
        )
