"""The interactive agent loop — a model-driven, conversational tool loop.

Unlike the Engineer (one scoped task, gated by a test command), the agent runs a
multi-turn conversation: the model decides each tool call, the human can steer
between turns, writes/commands are permission-gated, edits are checkpointed for
undo, and long histories are compacted. It reuses Forge's provider + tool layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from forge.agent.permissions import PermissionManager
from forge.providers.base import Completion, Message, Provider
from forge.tools.base import ToolContext, ToolRegistry


def _preview(call: dict) -> str:
    name = call.get("name", "")
    a = call.get("arguments", {}) or {}
    if name == "write_file":
        c = str(a.get("content", ""))
        return f"write {len(c)} bytes to {a.get('path')}\n┌─\n{c[:400]}\n└─"
    if name == "edit_file":
        return (f"edit {a.get('path')}\n- {str(a.get('old_string',''))[:200]}\n"
                f"+ {str(a.get('new_string',''))[:200]}")
    if name == "run_command":
        return f"$ {a.get('cmd','')}"
    return ""


def _tok(messages: List[Message]) -> int:
    n = 0
    for m in messages:
        n += len(m.content or "") // 4
        for tc in (m.tool_calls or []):
            n += len(str(tc)) // 4
    return n


@dataclass
class AgentLoop:
    provider: Provider
    tools: ToolRegistry
    tool_ctx: ToolContext
    permissions: PermissionManager
    system_prompt: str
    on_event: Callable[[str, dict], None] = lambda *_: None
    compactor: Optional[Provider] = None  # cheap model for summarizing old turns
    max_steps: int = 50
    keep_turns: int = 2            # user-turns kept verbatim when compacting
    history: List[Message] = field(default_factory=list)
    interrupted: bool = False

    def __post_init__(self) -> None:
        if not self.history:
            self.history = [Message("system", self.system_prompt)]

    # -- public API -------------------------------------------------------- #

    def run_turn(self, user_text: str) -> str:
        """Process one user message: drive model<->tools until the model stops."""
        self.interrupted = False
        if getattr(self.tool_ctx, "checkpoint", None) is not None:
            self.tool_ctx.checkpoint.checkpoint()  # fresh undo batch per turn
        self._maybe_compact()
        self.history.append(Message("user", user_text))

        for _ in range(self.max_steps):
            if self.interrupted:
                self.history.append(Message("user", "[interrupted by user]"))
                return "(interrupted)"
            completion = self.provider.complete(self.history, tools=self.tools.specs())
            if completion.text and completion.text.strip():
                self.on_event("assistant_text", {"text": completion.text.strip()})

            if not completion.tool_calls:
                self.history.append(Message("assistant", completion.text or ""))
                return completion.text or ""

            self.history.append(self._assistant_msg(completion))
            for call in completion.tool_calls:
                content = self._run_call(call)
                self.history.append(
                    Message("tool", content, tool_call_id=call.get("id"))
                )
        self.on_event("error", {"text": f"hit max_steps ({self.max_steps})"})
        return "(reached step limit for this turn)"

    # -- internals --------------------------------------------------------- #

    def _run_call(self, call: dict) -> str:
        name = call.get("name", "")
        args = call.get("arguments", {}) or {}
        self.on_event("tool_call", {"name": name, "args": args})
        decision = self.permissions.check(call, preview=_preview(call))
        if not decision.allowed:
            self.on_event("permission_denied", {"name": name, "reason": decision.reason})
            return f"PERMISSION DENIED: {decision.reason}"
        result = self.tools.dispatch(call, self.tool_ctx)
        self.on_event("tool_result", {"name": name, "ok": result.ok,
                                      "content": result.content})
        return result.content

    @staticmethod
    def _assistant_msg(c: Completion) -> Message:
        return Message("assistant", c.text or "", tool_calls=c.tool_calls or None)

    def _maybe_compact(self) -> None:
        budget = int(self.provider.context_window * 0.75)
        if _tok(self.history) <= budget or self.compactor is None:
            return
        # Find user-message boundaries (index 0 is the system message).
        user_idx = [i for i, m in enumerate(self.history) if m.role == "user"]
        if len(user_idx) <= self.keep_turns:
            return
        cut = user_idx[-self.keep_turns]  # keep this user turn onward verbatim
        head, tail = self.history[1:cut], self.history[cut:]
        if not head:
            return
        transcript = "\n".join(
            f"{m.role}: {m.content[:1500]}" for m in head if m.content
        )
        try:
            summary = self.compactor.complete([
                Message("system", "Summarize this coding-session transcript so work "
                        "can continue. Preserve: decisions made, file paths created/"
                        "edited, commands run and their outcomes, and any open todos. "
                        "Be terse and factual; no preamble."),
                Message("user", transcript[:12000]),
            ]).text.strip()
        except Exception:
            return
        self.history = (
            [self.history[0],
             Message("user", "[Summary of earlier turns]\n" + summary)]
            + tail
        )
        self.on_event("compaction", {"kept_turns": self.keep_turns})
