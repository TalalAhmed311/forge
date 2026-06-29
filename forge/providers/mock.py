"""Mock provider — deterministic, offline, no network or API key.

Two uses:
  * tests script an exact sequence of `Completion`s to drive the loops;
  * `forge` can run end-to-end offline for demos by replaying a canned script.

It is a real `Provider`, so it exercises the same orchestration code paths as a
hosted model.
"""

from __future__ import annotations

from typing import Callable, Optional, Union

from forge.providers.base import Completion, Message, Provider, ToolSpec

# A scripted step is either a ready-made Completion or a callable that builds one
# from the current message history (so tests can react to what the loop sent).
ScriptStep = Union[Completion, Callable[[list[Message]], Completion]]


class MockProvider(Provider):
    name = "mock"

    def __init__(
        self,
        script: Optional[list[ScriptStep]] = None,
        context_window: int = 8192,
        default_text: str = "done",
    ) -> None:
        self.script = list(script or [])
        self.context_window = context_window
        self.default_text = default_text
        self.calls: list[list[Message]] = []  # recorded history per call, for assertions

    def complete(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        temperature: float = 0.0,
    ) -> Completion:
        self.calls.append(list(messages))
        if self.script:
            step = self.script.pop(0)
            result = step(messages) if callable(step) else step
            return result
        # Exhausted script (or never given one): emit a plain "done" with no tool
        # call, which signals the engineer loop to verify.
        return Completion(text=self.default_text, tool_calls=[], raw={}, usage={})
