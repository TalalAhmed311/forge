"""Tool ABC, dispatch registry, and the shared execution context (Section 12).

Every tool exposes a JSON schema (surfaced to the model as a `ToolSpec`) and
returns a structured `ToolResult`. The orchestrator dispatches the normalized
`tool_calls` produced by the provider layer through a `ToolRegistry`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from forge.providers.base import ToolSpec

if TYPE_CHECKING:  # avoid import cycles; these are only type hints
    from forge.grounding import GroundingCache
    from forge.memory.context_manager import ContextManager


@dataclass
class ToolContext:
    """Everything a tool needs to act, injected at dispatch time.

    `workspace` is the target repo root; filesystem and shell tools are confined
    to it. `role` lets a tool refuse a caller (e.g. only the engineer writes).
    """

    workspace: str
    config: object  # forge.config.Config (untyped here to avoid a cycle)
    role: str = "engineer"
    grounding: Optional["GroundingCache"] = None
    context_manager: Optional["ContextManager"] = None
    recall: object = None  # forge.memory.recall.CrossSessionRecall (optional)
    project_name: str = ""
    session_id: str = ""
    checkpoint: object = None  # forge.agent.checkpoint.CheckpointManager (optional)


@dataclass
class ToolResult:
    ok: bool
    content: str
    meta: dict = field(default_factory=dict)


class Tool(ABC):
    name: str = "tool"
    description: str = ""
    parameters: dict = {}

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)

    @abstractmethod
    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    """Holds the tool set for a loop and dispatches normalized calls to them."""

    def __init__(self, tools: Optional[list[Tool]] = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def dispatch(self, call: dict, ctx: ToolContext) -> ToolResult:
        """Run one normalized tool call: {"id","name","arguments"}."""

        tool = self._tools.get(call["name"])
        if tool is None:
            return ToolResult(
                ok=False,
                content=f"unknown tool '{call['name']}'",
            )
        try:
            return tool.run(call.get("arguments", {}) or {}, ctx)
        except Exception as exc:  # a tool crash must not kill the loop
            return ToolResult(ok=False, content=f"{type(exc).__name__}: {exc}")
