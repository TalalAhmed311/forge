"""Provider interface and shared data types (Section 5.1).

Nothing else in the system may import a model SDK directly; every model call in
Forge flows through a `Provider`. Adapters normalize provider-specific tool-call
shapes into the single `Completion.tool_calls` form so callers never branch on
which backend they happen to be using.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Message:
    """One turn in a conversation.

    `role` is one of "system" | "user" | "assistant" | "tool". For a tool result
    message, set `tool_call_id` to the id of the call it answers.
    """

    role: str
    content: str
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


@dataclass
class ToolSpec:
    """A tool advertised to the model. `parameters` is a JSON schema object."""

    name: str
    description: str
    parameters: dict


@dataclass
class Completion:
    """A normalized model response.

    `tool_calls` is always the same shape regardless of provider:
        [{"id": str, "name": str, "arguments": dict}, ...]
    """

    text: str
    tool_calls: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Raised when a provider call fails irrecoverably (HTTP, auth, etc.)."""


class ToolCallParseError(ValueError):
    """Raised when a model emits a tool call that cannot be parsed/validated.

    The Ollama adapter catches this inside its parse->validate->retry loop
    (Section 5.2). Hosted adapters surface it directly.
    """


class Provider(ABC):
    """Abstract model backend.

    Subclasses MUST expose `context_window`; the context manager reads it to
    decide how much episodic context to load. Never hardcode a window elsewhere.
    """

    name: str = "provider"
    context_window: int = 8192

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        temperature: float = 0.0,
    ) -> Completion:
        """Run one completion and return a normalized `Completion`."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Shared helpers used by the concrete adapters.
# --------------------------------------------------------------------------- #


def normalize_tool_call(call_id: str, name: str, raw_arguments: Any) -> dict:
    """Coerce one provider tool call into Forge's normalized shape.

    `raw_arguments` may arrive as a JSON string (OpenAI/Ollama) or as an already
    decoded dict (Anthropic). Either way we end up with a dict, raising
    `ToolCallParseError` on malformed JSON so callers can retry.
    """

    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:  # malformed JSON from the model
            raise ToolCallParseError(
                f"tool '{name}' arguments are not valid JSON: {exc}"
            ) from exc
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    elif raw_arguments is None:
        arguments = {}
    else:
        raise ToolCallParseError(
            f"tool '{name}' arguments have unexpected type {type(raw_arguments)!r}"
        )

    if not name or not isinstance(name, str):
        raise ToolCallParseError(f"tool call is missing a valid name: {name!r}")

    return {"id": call_id or "", "name": name, "arguments": arguments}


def validate_tool_calls(calls: list[dict], tools: Optional[list[ToolSpec]]) -> None:
    """Validate that each normalized call names a known tool.

    Lightweight by design: we check the tool exists and that required top-level
    properties are present. Deep schema validation is left to the tool itself.
    """

    if not tools:
        return
    by_name = {t.name: t for t in tools}
    for call in calls:
        spec = by_name.get(call["name"])
        if spec is None:
            raise ToolCallParseError(
                f"model called unknown tool '{call['name']}'; "
                f"available: {sorted(by_name)}"
            )
        required = spec.parameters.get("required", []) if spec.parameters else []
        missing = [r for r in required if r not in call["arguments"]]
        if missing:
            raise ToolCallParseError(
                f"tool '{spec.name}' missing required args {missing}"
            )
