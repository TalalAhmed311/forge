"""Ollama adapter for local models (Section 5.2).

Local models are the weak link: they emit malformed tool-call JSON often enough
that a parse->validate->retry loop is mandatory. We also pin `num_ctx`
explicitly — Ollama's small default silently truncates context and is the #1
cause of erratic local-model behavior.
"""

from __future__ import annotations

from typing import Optional

from forge.providers._http import post_json
from forge.providers.base import (
    Completion,
    Message,
    Provider,
    ToolCallParseError,
    ToolSpec,
    normalize_tool_call,
    validate_tool_calls,
)


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(
        self,
        model: str,
        context_window: int = 32768,
        num_ctx: int = 32768,
        base_url: str = "http://localhost:11434",
        max_parse_retries: int = 3,
        max_retries: int = 4,
    ) -> None:
        self.model = model
        # context_window is what the rest of Forge budgets against; for Ollama it
        # is exactly num_ctx, since that is the window the model actually sees.
        self.num_ctx = num_ctx
        self.context_window = min(context_window, num_ctx)
        self.base_url = base_url.rstrip("/")
        self.max_parse_retries = max_parse_retries   # malformed tool-JSON re-prompts
        self.max_retries = max_retries               # transient API/network retries

    @staticmethod
    def _to_wire_messages(messages: list[Message]) -> list[dict]:
        wire = []
        for m in messages:
            entry: dict = {"role": m.role, "content": m.content}
            if m.tool_calls:
                # Ollama expects function.arguments as an object, not a string.
                entry["tool_calls"] = [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in m.tool_calls
                ]
            wire.append(entry)
        return wire

    @staticmethod
    def _to_wire_tools(tools: Optional[list[ToolSpec]]) -> Optional[list[dict]]:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def complete(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        temperature: float = 0.0,
    ) -> Completion:
        wire_messages = self._to_wire_messages(messages)
        wire_tools = self._to_wire_tools(tools)

        last_error: Optional[ToolCallParseError] = None
        # parse -> validate -> retry: on malformed tool JSON, re-prompt with the
        # parse error appended so the model can correct itself.
        for attempt in range(self.max_parse_retries + 1):
            payload: dict = {
                "model": self.model,
                "messages": wire_messages,
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": self.num_ctx},
            }
            if wire_tools:
                payload["tools"] = wire_tools

            raw = post_json(f"{self.base_url}/api/chat", payload,
                            max_retries=self.max_retries)
            try:
                return self._parse(raw, tools)
            except ToolCallParseError as exc:
                last_error = exc
                if attempt >= self.max_parse_retries:
                    break
                # Feed the assistant's bad output + the parse error back in.
                bad = raw.get("message", {}).get("content", "")
                wire_messages = wire_messages + [
                    {"role": "assistant", "content": bad},
                    {
                        "role": "user",
                        "content": (
                            f"Your tool call could not be parsed: {exc}. "
                            "Re-emit it as a single valid tool call with strict "
                            "JSON arguments."
                        ),
                    },
                ]

        raise ToolCallParseError(
            f"ollama '{self.model}' failed to produce a valid tool call after "
            f"{self.max_parse_retries} retries: {last_error}"
        )

    def _parse(self, raw: dict, tools: Optional[list[ToolSpec]]) -> Completion:
        msg = raw.get("message", {}) or {}
        text = msg.get("content") or ""

        calls = []
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            # Ollama may hand back an id or none; synthesize a stable one if absent.
            call_id = tc.get("id") or f"call_{idx}"
            calls.append(
                normalize_tool_call(call_id, fn.get("name", ""), fn.get("arguments"))
            )
        validate_tool_calls(calls, tools)

        return Completion(
            text=text,
            tool_calls=calls,
            raw=raw,
            usage={
                "input_tokens": raw.get("prompt_eval_count", 0),
                "output_tokens": raw.get("eval_count", 0),
            },
        )
