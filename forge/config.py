"""Configuration loading and validation (Section 14).

Forge runs with no config file at all: `DEFAULT_CONFIG` below mirrors the spec's
defaults. A project's `.forge/config.yaml` is deep-merged over these, and
per-role CLI overrides (`--provider`/`--model`) are merged last.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Optional

import yaml

DEFAULT_CONFIG: dict = {
    "roles": {
        "architect": {"provider": "anthropic", "model": "claude-opus-4-8"},
        # `engineer` is the Senior Software Engineer (backend + generalist); it is
        # the default route for any task not tagged frontend.
        "engineer": {"provider": "ollama", "model": "qwen-coder", "num_ctx": 32768},
        # `frontend_engineer` is the Senior UI/UX Engineer (frontend route).
        # Defaults to the same backend as `engineer` unless overridden.
        "frontend_engineer": {"provider": "ollama", "model": "qwen-coder", "num_ctx": 32768},
        "router": {"provider": "ollama", "model": "qwen", "num_ctx": 8192},
        "clarifier": {"provider": "openai", "model": "gpt-4o-mini"},
    },
    "loop": {
        "max_inner_iters": 15,
        "max_outer_tasks": 100,
    },
    "memory": {
        # "simple" = tier-1 + recent raw (default; useful product after Phase 5).
        # "episodic" = full Phase 6 engine (chunking + routing + disclosure).
        "engine": "simple",
        "chunk_tokens": 8000,
        "chunk_overlap": 800,
        "max_activated_chunks": 8,
        "max_live_raw": 3,
        # Persistent backends (run with `docker compose up -d`). ON by default;
        # when a service is unreachable the system degrades to in-memory (the
        # tracker on disk is the durability backstop), so it never blocks a run.
        "long_term": True,           # cross-session recall (Redis + pgvector)
        "redis_url": "redis://localhost:6379/0",
        "pg_dsn": "postgresql://forge:forge@localhost:5432/forge",
        # Open-source local embeddings via Ollama (no API key, nothing leaves the box).
        "embedder": "ollama",        # "ollama" (nomic-embed-text, 768) | "hashing"
        "embedder_model": "nomic-embed-text",
        "embedder_dim": 768,
    },
    "tools": {
        "command_timeout_s": 120,
        "command_allowlist_only": False,
        "command_allowlist": [],
    },
    # Phase 7 — self-improvement. Absent/disabled => behaves exactly as Phase 1-6.
    "improve": {
        "enabled": False,
        "lessons": {
            "enabled": True,
            "inject_top_k": 5,
            "demote_after_uses": 8,
            "demote_win_rate": 0.25,
        },
        "skills": {
            "enabled": True,
            "auto_promote": False,
        },
        # Eval isolation (5.1): write-denied roots, relative to the workspace.
        "protected_paths": ["tests/", "test/", ".forge/eval/"],
        "harness_self_edit": {"enabled": False},
    },
}

ROLES = ("architect", "engineer", "frontend_engineer", "router", "clarifier")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`."""

    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    data: dict

    def role(self, name: str) -> dict:
        if name not in self.data["roles"]:
            raise KeyError(f"unknown role '{name}'")
        return self.data["roles"][name]

    @property
    def loop(self) -> dict:
        return self.data["loop"]

    @property
    def memory(self) -> dict:
        return self.data["memory"]

    @property
    def tools(self) -> dict:
        return self.data["tools"]

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _validate(data: dict) -> None:
    for role in ROLES:
        spec = data["roles"].get(role)
        if not spec or "provider" not in spec or "model" not in spec:
            raise ValueError(f"config role '{role}' must set provider and model")
    if data["loop"]["max_inner_iters"] < 1:
        raise ValueError("loop.max_inner_iters must be >= 1")


def load_config(
    config_path: Optional[str] = None,
    overrides: Optional[dict] = None,
) -> Config:
    """Load config: defaults <- file <- overrides. Validate and return."""

    data = copy.deepcopy(DEFAULT_CONFIG)

    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            file_data = yaml.safe_load(fh) or {}
        if not isinstance(file_data, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        data = _deep_merge(data, file_data)

    if overrides:
        data = _deep_merge(data, overrides)

    _validate(data)
    return Config(data)
