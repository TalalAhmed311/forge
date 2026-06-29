"""Shared fixtures: a throwaway target repo with a planted failing test."""

from __future__ import annotations

import os
import textwrap

import pytest

from forge.config import load_config
from forge.grounding import GroundingCache
from forge.memory.context_manager import SimpleContextManager
from forge.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _no_real_memory_backends(monkeypatch):
    """Keep the suite off the real Redis/Postgres.

    Orchestrator tests run a real `forge run` against a tmp project with the
    default config, whose memory defaults point at localhost Redis + pgvector. On
    a dev box with `docker compose up`, that silently writes junk rows to the real
    database. Force long-term off and point the DSNs at nowhere so tests are
    hermetic regardless of what's running locally. Tests that exercise the memory
    layer construct stores directly or set their own DSNs, so they're unaffected.
    """

    from forge.config import DEFAULT_CONFIG

    mem = DEFAULT_CONFIG["memory"]
    monkeypatch.setitem(mem, "long_term", False)
    monkeypatch.setitem(mem, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setitem(mem, "pg_dsn", "postgresql://x@127.0.0.1:1/x")


@pytest.fixture
def failing_repo(tmp_path):
    """A workspace where calc.add is buggy and tests/test_calc.py fails."""

    (tmp_path / "calc.py").write_text(
        textwrap.dedent(
            """
            def add(a, b):
                return a - b  # bug: should be +
            """
        ).strip()
        + "\n"
    )
    os.makedirs(tmp_path / "tests", exist_ok=True)
    (tmp_path / "tests" / "test_calc.py").write_text(
        textwrap.dedent(
            """
            from calc import add

            def test_add():
                assert add(1, 2) == 3
            """
        ).strip()
        + "\n"
    )
    return tmp_path


@pytest.fixture
def tool_ctx(failing_repo):
    cfg = load_config(overrides={"roles": {
        "architect": {"provider": "mock", "model": "m"},
        "engineer": {"provider": "mock", "model": "m"},
        "router": {"provider": "mock", "model": "m"},
        "clarifier": {"provider": "mock", "model": "m"},
    }})
    grounding = GroundingCache()
    cm = SimpleContextManager(tier1_provider=lambda: "(tier1)")
    return ToolContext(
        workspace=str(failing_repo),
        config=cfg,
        role="engineer",
        grounding=grounding,
        context_manager=cm,
    )
