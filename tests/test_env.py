"""The .env loader: parses keys, honors quotes/export, never overrides real env."""

from __future__ import annotations

import os

from forge._env import load_dotenv


def test_loads_keys_and_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "OPENAI_API_KEY=sk-plain\n"
        'ANTHROPIC_API_KEY="sk-ant-quoted"\n'
        "export DEEPSEEK_API_KEY='sk-deep'\n"
    )
    n = load_dotenv(str(env))
    assert n == 3
    assert os.environ["OPENAI_API_KEY"] == "sk-plain"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-quoted"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-deep"


def test_real_env_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "real-key")
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=from-dotenv\n")
    load_dotenv(str(env))
    assert os.environ["OPENAI_API_KEY"] == "real-key"  # not overridden


def test_missing_file_is_noop():
    assert load_dotenv("/no/such/.env") == 0
