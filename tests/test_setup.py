"""The interactive setup wizard: per-role provider/model selection, API-key
handling (skip if in env, else prompt + save), and Ollama model pull."""

from __future__ import annotations

import os

from forge.config import load_config
from forge.project import ForgeProject
from forge.setup import OllamaClient, SetupIO, run_setup, upsert_env_file


class FakeOllama:
    def __init__(self, installed, available=True):
        self.installed = installed
        self.available = available
        self.pulled = []
        self.ensured = 0

    def ensure_available(self, io):
        self.ensured += 1
        return self.available

    def list_models(self):
        return list(self.installed)

    def pull(self, model, say):
        self.pulled.append(model)
        return True


def _io(answers, secrets=None):
    answers = list(answers)
    secrets = list(secrets or [])
    said = []
    return SetupIO(
        ask=lambda _p: answers.pop(0),
        secret=lambda _p: secrets.pop(0),
        say=said.append,
    ), said


# -- .env upsert ------------------------------------------------------------ #


def test_upsert_env_appends_then_replaces(tmp_path):
    p = str(tmp_path / ".env")
    upsert_env_file(p, "OPENAI_API_KEY", "sk-1")
    assert "OPENAI_API_KEY=sk-1" in open(p).read()
    # Replacing keeps a single line.
    upsert_env_file(p, "OPENAI_API_KEY", "sk-2")
    body = open(p).read()
    assert "OPENAI_API_KEY=sk-2" in body and "sk-1" not in body
    assert body.count("OPENAI_API_KEY=") == 1


# -- full wizard run -------------------------------------------------------- #


def test_wizard_writes_valid_config_and_skips_present_key(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    env = {"OPENAI_API_KEY": "already-set"}  # so no secret prompt is needed
    ollama = FakeOllama(installed=["qwen2.5-coder:7b"])

    # ROLES order: architect, engineer, frontend_engineer, router, clarifier.
    # architect: provider 2 (openai), model 1 (gpt-4o)
    # engineer:  provider 4 (ollama), model 1 (qwen2.5-coder:7b)
    # frontend:  provider 4, model 1
    # router:    provider 4, model 1
    # clarifier: provider 2 (openai), model 2 (gpt-4o-mini)
    io, said = _io(["2", "1", "4", "1", "4", "1", "4", "1", "2", "2"])

    roles = run_setup(project, io=io, ollama=ollama, env=env, pull_embedder=False)

    assert roles["architect"] == {"provider": "openai", "model": "gpt-4o"}
    assert roles["engineer"] == {
        "provider": "ollama", "model": "qwen2.5-coder:7b", "num_ctx": 32768,
    }
    assert roles["router"]["num_ctx"] == 8192  # lighter role
    assert roles["clarifier"]["model"] == "gpt-4o-mini"

    # The written config.yaml is valid and loads.
    cfg = load_config(project.config_path)
    assert cfg.role("architect")["provider"] == "openai"
    assert cfg.role("frontend_engineer")["provider"] == "ollama"
    # Key was already present → a skip message, no .env written.
    assert any("already set" in m for m in said)
    assert not os.path.exists(os.path.join(str(tmp_path), ".env"))
    assert ollama.pulled == []  # installed model chosen, nothing pulled


def test_wizard_prompts_and_saves_missing_key(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    env: dict = {}  # no keys present
    ollama = FakeOllama(installed=["qwen2.5-coder:7b"])
    # All five roles on openai gpt-4o; key prompted once (cached in env after).
    io, said = _io(["2", "1", "2", "1", "2", "1", "2", "1", "2", "1"],
                   secrets=["sk-secret"])

    roles = run_setup(project, io=io, ollama=ollama, env=env, pull_embedder=False)

    assert all(v["provider"] == "openai" for v in roles.values())
    # The key was prompted once and persisted to <root>/.env.
    env_path = os.path.join(str(tmp_path), ".env")
    assert os.path.isfile(env_path)
    assert "OPENAI_API_KEY=sk-secret" in open(env_path).read()
    assert env["OPENAI_API_KEY"] == "sk-secret"


def test_reconfigure_single_role_preserves_others(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    env = {"OPENAI_API_KEY": "x"}
    ollama = FakeOllama(installed=["qwen2.5-coder:7b"])

    # First, a full setup: everything on openai gpt-4o.
    io, _ = _io(["2", "1"] * 5)
    run_setup(project, io=io, ollama=ollama, env=env, pull_embedder=False)

    # Now reconfigure ONLY the engineer to a local Ollama model.
    io2, said = _io(["4", "1"])  # provider ollama, installed model 1
    roles = run_setup(project, io=io2, ollama=ollama, env=env, roles=("engineer",))

    assert roles == {"engineer": {
        "provider": "ollama", "model": "qwen2.5-coder:7b", "num_ctx": 32768}}
    assert any("reconfiguring: engineer" in m for m in said)

    # The other four roles are untouched in the written config.
    cfg = load_config(project.config_path)
    assert cfg.role("engineer")["provider"] == "ollama"
    assert cfg.role("architect")["provider"] == "openai"
    assert cfg.role("clarifier")["model"] == "gpt-4o"
    assert cfg.role("frontend_engineer")["provider"] == "openai"


def test_wizard_pulls_new_ollama_model(tmp_path):
    project = ForgeProject(root=str(tmp_path))
    env = {"OPENAI_API_KEY": "x"}
    ollama = FakeOllama(installed=[])  # nothing installed -> only "pull" option
    # architect openai gpt-4o; the rest ollama, each pulling the tag entered.
    # ollama role: provider 4, then choose option 1 (pull), then type tag.
    io, said = _io([
        "2", "1",                      # architect: openai / gpt-4o
        "4", "1", "qwen2.5-coder:7b",  # engineer: ollama / pull tag
        "4", "1", "qwen2.5-coder:7b",  # frontend
        "4", "1", "qwen2.5:3b",        # router
        "2", "2",                      # clarifier: openai / gpt-4o-mini
    ])

    roles = run_setup(project, io=io, ollama=ollama, env=env, pull_embedder=False)

    assert roles["engineer"]["model"] == "qwen2.5-coder:7b"
    assert roles["router"]["model"] == "qwen2.5:3b"
    assert ollama.pulled == ["qwen2.5-coder:7b", "qwen2.5-coder:7b", "qwen2.5:3b"]


def test_ollama_setup_is_ensured_and_bare_name_pulled(tmp_path):
    """Choosing Ollama triggers ensure_available, and a tag-less name (latest) is
    pulled as entered."""

    project = ForgeProject(root=str(tmp_path))
    env = {"OPENAI_API_KEY": "x"}
    ollama = FakeOllama(installed=[])
    # architect openai; engineer ollama with a bare model name (latest); rest openai.
    io, _ = _io([
        "2", "1",                 # architect openai gpt-4o
        "4", "1", "qwen2.5-coder",  # engineer: ollama, download, bare name
        "2", "1",                 # frontend openai
        "2", "1",                 # router openai
        "2", "1",                 # clarifier openai
    ])
    roles = run_setup(project, io=io, ollama=ollama, env=env, pull_embedder=False)

    assert ollama.ensured >= 1                       # availability was ensured
    assert ollama.pulled == ["qwen2.5-coder"]        # bare name => latest
    assert roles["engineer"]["model"] == "qwen2.5-coder"


def test_setup_pulls_embedding_model_for_long_term_memory(tmp_path, monkeypatch):
    # This test specifically exercises the long-term path; opt back in past the
    # autouse fixture that disables it for hermeticity.
    from forge.config import DEFAULT_CONFIG
    monkeypatch.setitem(DEFAULT_CONFIG["memory"], "long_term", True)
    project = ForgeProject(root=str(tmp_path))
    env = {"OPENAI_API_KEY": "x"}
    ollama = FakeOllama(installed=[])   # nomic not present -> should be pulled
    # All five roles on openai, then "y" to the embedding-model pull prompt.
    io, said = _io(["2", "1"] * 5 + ["y"])

    run_setup(project, io=io, ollama=ollama, env=env)   # pull_embedder defaults True

    assert "nomic-embed-text" in ollama.pulled
    assert any("nomic-embed-text" in m for m in said)
