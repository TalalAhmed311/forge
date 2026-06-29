"""Phase 1 exit test: all adapters normalize the same logical response identically.

Each provider returns a tool call in its own wire shape; after normalization the
`Completion.tool_calls` must be byte-for-byte the same across all four.
"""

from __future__ import annotations

import json

import pytest

from forge.providers import anthropic as anthropic_mod
from forge.providers import ollama as ollama_mod
from forge.providers import openai as openai_mod
from forge.providers.anthropic import AnthropicProvider
from forge.providers.base import Message, ToolCallParseError, ToolSpec
from forge.providers.deepseek import DeepSeekProvider
from forge.providers.ollama import OllamaProvider
from forge.providers.openai import OpenAIProvider

TOOL = ToolSpec(
    name="read_file",
    description="Read a file",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)

EXPECTED_CALL = {"id": "call_1", "name": "read_file", "arguments": {"path": "a.py"}}

# Canned raw responses, one per provider wire format.
OPENAI_RAW = {
    "choices": [
        {
            "message": {
                "content": "reading",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}

ANTHROPIC_RAW = {
    "content": [
        {"type": "text", "text": "reading"},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "a.py"},
        },
    ],
    "usage": {"input_tokens": 10, "output_tokens": 3},
}

OLLAMA_RAW = {
    "message": {
        "content": "reading",
        "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
        ],
    },
    "prompt_eval_count": 10,
    "eval_count": 3,
}


def _patch(monkeypatch, module, raw):
    monkeypatch.setattr(module, "post_json", lambda *a, **k: raw)


def test_openai_normalizes(monkeypatch):
    _patch(monkeypatch, openai_mod, OPENAI_RAW)
    p = OpenAIProvider(model="gpt-4o", api_key="x")
    c = p.complete([Message("user", "hi")], tools=[TOOL])
    assert c.tool_calls == [EXPECTED_CALL]
    assert c.text == "reading"
    assert c.usage == {"input_tokens": 10, "output_tokens": 3}


def test_deepseek_uses_openai_shape(monkeypatch):
    _patch(monkeypatch, openai_mod, OPENAI_RAW)
    p = DeepSeekProvider(api_key="x")
    c = p.complete([Message("user", "hi")], tools=[TOOL])
    assert c.tool_calls == [EXPECTED_CALL]


def test_anthropic_normalizes(monkeypatch):
    _patch(monkeypatch, anthropic_mod, ANTHROPIC_RAW)
    p = AnthropicProvider(model="claude-opus-4-8", api_key="x")
    c = p.complete([Message("user", "hi")], tools=[TOOL])
    assert c.tool_calls == [EXPECTED_CALL]
    assert c.text == "reading"
    assert c.usage == {"input_tokens": 10, "output_tokens": 3}


def test_ollama_normalizes(monkeypatch):
    _patch(monkeypatch, ollama_mod, OLLAMA_RAW)
    p = OllamaProvider(model="qwen-coder")
    c = p.complete([Message("user", "hi")], tools=[TOOL])
    # Ollama synthesizes an id from index; everything else must match.
    assert c.tool_calls[0]["name"] == "read_file"
    assert c.tool_calls[0]["arguments"] == {"path": "a.py"}


def test_all_adapters_agree(monkeypatch):
    """The core Phase 1 contract: identical normalized tool_calls + text."""

    results = []
    for module, raw, provider in [
        (openai_mod, OPENAI_RAW, OpenAIProvider(model="gpt-4o", api_key="x")),
        (anthropic_mod, ANTHROPIC_RAW, AnthropicProvider(model="c", api_key="x")),
        (ollama_mod, OLLAMA_RAW, OllamaProvider(model="qwen-coder")),
    ]:
        monkeypatch.setattr(module, "post_json", lambda *a, _r=raw, **k: _r)
        c = provider.complete([Message("user", "hi")], tools=[TOOL])
        # Normalize the synthesized ollama id so the comparison is apples-to-apples.
        norm = [{**tc, "id": "X"} for tc in c.tool_calls]
        results.append((c.text, norm))

    first = results[0]
    for r in results[1:]:
        assert r == first


def test_ollama_retries_on_bad_json(monkeypatch):
    """Malformed tool JSON triggers re-prompt; a good follow-up succeeds."""

    bad = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": "{not json"}}
            ],
        }
    }
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return bad if calls["n"] == 1 else OLLAMA_RAW

    monkeypatch.setattr(ollama_mod, "post_json", fake_post)
    p = OllamaProvider(model="qwen-coder", max_parse_retries=2)
    c = p.complete([Message("user", "hi")], tools=[TOOL])
    assert c.tool_calls[0]["arguments"] == {"path": "a.py"}
    assert calls["n"] == 2  # one failure, one success


def test_ollama_gives_up_after_retries(monkeypatch):
    bad = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": "{bad"}}
            ],
        }
    }
    monkeypatch.setattr(ollama_mod, "post_json", lambda *a, **k: bad)
    p = OllamaProvider(model="qwen-coder", max_parse_retries=1)
    with pytest.raises(ToolCallParseError):
        p.complete([Message("user", "hi")], tools=[TOOL])


def test_openai_serializes_echoed_tool_calls_to_wire_shape():
    """An assistant turn with normalized tool_calls must serialize to OpenAI's
    wire shape (function.arguments as a JSON string) for multi-turn tool use."""

    history = [
        Message("user", "hi"),
        Message(
            "assistant",
            "",
            tool_calls=[{"id": "call_1", "name": "read_file", "arguments": {"path": "a.py"}}],
        ),
        Message("tool", "contents", tool_call_id="call_1"),
    ]
    wire = OpenAIProvider._to_wire_messages(history)
    tc = wire[1]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "a.py"}
    assert wire[2]["tool_call_id"] == "call_1"


def test_ollama_serializes_echoed_tool_calls_with_object_args():
    history = [
        Message(
            "assistant",
            "",
            tool_calls=[{"id": "c", "name": "read_file", "arguments": {"path": "a.py"}}],
        )
    ]
    wire = OllamaProvider._to_wire_messages(history)
    assert wire[0]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}


def test_anthropic_round_trips_tool_use_blocks():
    history = [
        Message("system", "sys"),
        Message(
            "assistant",
            "thinking",
            tool_calls=[{"id": "c", "name": "read_file", "arguments": {"path": "a.py"}}],
        ),
        Message("tool", "contents", tool_call_id="c"),
    ]
    system, wire = AnthropicProvider._split_messages(history)
    assert system == "sys"
    assistant_blocks = wire[0]["content"]
    assert any(b["type"] == "tool_use" and b["input"] == {"path": "a.py"} for b in assistant_blocks)
    assert wire[1]["content"][0]["type"] == "tool_result"


def test_unknown_tool_rejected(monkeypatch):
    raw = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "nope", "arguments": "{}"}}
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr(openai_mod, "post_json", lambda *a, **k: raw)
    p = OpenAIProvider(model="gpt-4o", api_key="x")
    with pytest.raises(ToolCallParseError):
        p.complete([Message("user", "hi")], tools=[TOOL])
