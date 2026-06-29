"""Anthropic Messages API adapter (Section 5).

Anthropic differs from the OpenAI shape in three ways the adapter hides:
  * the system prompt is a top-level field, not a message;
  * tool results are `tool_result` content blocks inside a user message;
  * tool calls come back as `tool_use` blocks with already-decoded input dicts.
"""

from __future__ import annotations

import os
from typing import Optional

from forge.providers._http import post_json
from forge.providers.base import (
    Completion,
    Message,
    Provider,
    ProviderError,
    ToolSpec,
    normalize_tool_call,
    validate_tool_calls,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        model: str,
        context_window: int = 200000,
        base_url: str = "https://api.anthropic.com/v1",
        api_key: Optional[str] = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        max_tokens: int = 8192,
        max_retries: int = 4,
    ) -> None:
        self.model = model
        self.context_window = context_window
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.max_retries = max_retries

    def _headers(self) -> dict:
        if not self.api_key:
            raise ProviderError(
                f"{self.name}: no API key (set ${self.api_key_env})"
            )
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    @staticmethod
    def _split_messages(messages: list[Message]) -> tuple[str, list[dict]]:
        """Return (system_prompt, wire_messages) in Anthropic's block format."""

        system_parts: list[str] = []
        wire: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
            elif m.role == "assistant" and m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        }
                    )
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": m.role, "content": m.content})
        return "\n\n".join(system_parts), wire

    @staticmethod
    def _to_wire_tools(tools: Optional[list[ToolSpec]]) -> Optional[list[dict]]:
        if not tools:
            return None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def complete(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        temperature: float = 0.0,
    ) -> Completion:
        system, wire_messages = self._split_messages(messages)
        payload: dict = {
            "model": self.model,
            "messages": wire_messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        if system:
            payload["system"] = system
        wire_tools = self._to_wire_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools

        raw = post_json(f"{self.base_url}/messages", payload, self._headers(),
                        max_retries=self.max_retries)
        return self._parse(raw, tools)

    def _parse(self, raw: dict, tools: Optional[list[ToolSpec]]) -> Completion:
        text_parts: list[str] = []
        calls: list[dict] = []
        for block in raw.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                calls.append(
                    normalize_tool_call(
                        block.get("id", ""),
                        block.get("name", ""),
                        block.get("input", {}),
                    )
                )
        validate_tool_calls(calls, tools)

        usage = raw.get("usage", {}) or {}
        return Completion(
            text="".join(text_parts),
            tool_calls=calls,
            raw=raw,
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
        )
