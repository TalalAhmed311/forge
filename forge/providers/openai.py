"""OpenAI chat-completions adapter (Section 5).

Used directly for OpenAI and subclassed by the DeepSeek adapter, which speaks the
same wire format on a different base URL.
"""

from __future__ import annotations

import json
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


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(
        self,
        model: str,
        context_window: int = 128000,
        base_url: str = "https://api.openai.com/v1",
        api_key: Optional[str] = None,
        api_key_env: str = "OPENAI_API_KEY",
        max_retries: int = 4,
    ) -> None:
        self.model = model
        self.context_window = context_window
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.max_retries = max_retries

    def _headers(self) -> dict:
        if not self.api_key:
            raise ProviderError(
                f"{self.name}: no API key (set ${self.api_key_env})"
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _to_wire_messages(messages: list[Message]) -> list[dict]:
        wire = []
        for m in messages:
            entry: dict = {"role": m.role, "content": m.content}
            if m.tool_calls:
                # Convert Forge's normalized shape back to OpenAI's wire format so
                # echoed assistant turns are accepted in multi-turn tool use.
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
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
        payload: dict = {
            "model": self.model,
            "messages": self._to_wire_messages(messages),
            "temperature": temperature,
        }
        wire_tools = self._to_wire_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools

        raw = post_json(f"{self.base_url}/chat/completions", payload,
                        self._headers(), max_retries=self.max_retries)
        return self._parse(raw, tools)

    def _parse(self, raw: dict, tools: Optional[list[ToolSpec]]) -> Completion:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""

        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(
                normalize_tool_call(
                    tc.get("id", ""), fn.get("name", ""), fn.get("arguments", "")
                )
            )
        validate_tool_calls(calls, tools)

        usage = raw.get("usage", {}) or {}
        return Completion(
            text=text,
            tool_calls=calls,
            raw=raw,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )
