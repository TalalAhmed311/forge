"""Interactive setup wizard — pick a provider + model for each role.

Run by `forge setup`, and auto-launched by `forge run` when a project has no
`.forge/config.yaml` yet. For every role (architect, clarifier, engineer,
frontend_engineer, router) it:
  1. asks which provider — Claude (Anthropic), OpenAI, DeepSeek, or Ollama;
  2. shows that provider's models to choose from (or accept a custom id);
  3. for hosted providers, ensures the API key exists — prompting and saving it to
     `.env` only if it isn't already in the environment;
  4. for Ollama, makes sure Ollama is installed/running (setting it up if not),
     lists installed models, and pulls the chosen one if needed.

All IO and the Ollama calls are injected (`SetupIO`, `OllamaClient`) so the wizard
is testable without a real terminal or a running Ollama.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

import yaml

from forge.config import ROLES

# Provider menu, in the order shown.
PROVIDERS = ["anthropic", "openai", "deepseek", "ollama"]
PROVIDER_LABELS = {
    "anthropic": "Claude (Anthropic)",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "ollama": "Ollama (local, no API key)",
}

# Curated model catalogs for the hosted providers. "Custom" is always offered, so
# this list just needs to cover the common picks, not every model.
MODEL_CATALOG = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-fable-5",
    ],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}

API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Lighter roles get a smaller default context window.
_LIGHT_ROLES = {"router", "clarifier"}


# --------------------------------------------------------------------------- #
# Injectable IO and Ollama access.
# --------------------------------------------------------------------------- #


@dataclass
class SetupIO:
    """Terminal IO, injectable for tests."""

    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


class OllamaClient:
    """Minimal Ollama access: ensure availability, list installed, pull models."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self.base_url = base_url.rstrip("/")

    def is_installed(self) -> bool:
        return shutil.which("ollama") is not None

    def is_running(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3).read()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def ensure_available(self, io: "SetupIO") -> bool:
        """Make sure Ollama is installed AND running, setting it up if not.

        Installation touches the system, so it asks once (default yes). Starting
        the local server is low-risk and done automatically.
        """

        if not self.is_installed():
            io.say("  Ollama isn't installed on this machine.")
            if not self._install(io):
                return False
        if not self.is_running():
            io.say("  Starting the Ollama server …")
            self._start_server()
            for _ in range(15):  # wait up to ~7.5s for it to come up
                if self.is_running():
                    break
                time.sleep(0.5)
        if self.is_running():
            return True
        io.say("  Could not reach the Ollama server. Start it with `ollama serve`.")
        return False

    def _install(self, io: "SetupIO") -> bool:
        system = platform.system()
        if system == "Darwin":
            if shutil.which("brew"):
                ans = io.ask("  Install Ollama now via Homebrew? [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    subprocess.run(["brew", "install", "ollama"], check=False)
            else:
                io.say("  Homebrew not found. Install Ollama from "
                       "https://ollama.com/download, then re-run `forge setup`.")
        elif system == "Linux":
            ans = io.ask("  Install Ollama now (official install script)? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                subprocess.run("curl -fsSL https://ollama.com/install.sh | sh",
                               shell=True, check=False)
        else:
            io.say("  Install Ollama from https://ollama.com/download, then re-run setup.")
        if self.is_installed():
            io.say("  ✓ Ollama installed.")
            return True
        io.say("  Ollama still not found — skipping local setup for now.")
        return False

    def _start_server(self) -> None:
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            pass

    def pull(self, model: str, say: Callable[[str], None]) -> bool:
        # No explicit tag => Ollama pulls the latest version.
        label = model if ":" in model else f"{model} (latest)"
        say(f"  Pulling {label} … (this can take a while)")
        try:
            proc = subprocess.run(["ollama", "pull", model], check=False)
        except FileNotFoundError:
            say("  `ollama` CLI not found — install Ollama or pull the model manually.")
            return False
        return proc.returncode == 0


# --------------------------------------------------------------------------- #
# Menu helpers.
# --------------------------------------------------------------------------- #


def _choose(io: SetupIO, title: str, options: list[str]) -> int:
    """Show a numbered menu and return the chosen 0-based index."""

    io.say(title)
    for i, opt in enumerate(options, 1):
        io.say(f"  {i}. {opt}")
    while True:
        raw = io.ask("Enter number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        io.say(f"Please enter a number between 1 and {len(options)}.")


def _choose_model(io: SetupIO, provider: str) -> str:
    catalog = MODEL_CATALOG.get(provider, [])
    options = catalog + ["Enter a custom model id"]
    idx = _choose(io, f"  Select a {PROVIDER_LABELS[provider]} model:", options)
    if idx < len(catalog):
        return catalog[idx]
    while True:
        custom = io.ask("  Custom model id: ").strip()
        if custom:
            return custom


def _choose_ollama_model(io: SetupIO, ollama: OllamaClient) -> str:
    # Make sure Ollama is installed and running first (auto-set up if not).
    ollama.ensure_available(io)

    installed = ollama.list_models()
    options = list(installed) + ["Download a new model"]
    if installed:
        io.say("  Installed Ollama models:")
    idx = _choose(io, "  Select an Ollama model:", options)
    if idx < len(installed):
        return installed[idx]

    # Ask for the model NAME; the latest version is pulled automatically.
    while True:
        name = io.ask(
            "  Model name to download — latest version (e.g. qwen2.5-coder): "
        ).strip()
        if name:
            break
    ollama.pull(name, io.say)  # proceed even if pull fails; user may pull later
    return name


# --------------------------------------------------------------------------- #
# .env upsert (only for keys not already in the environment).
# --------------------------------------------------------------------------- #


def upsert_env_file(env_path: str, key: str, value: str) -> None:
    """Set `key=value` in `.env`, replacing an existing line or appending."""

    lines: list[str] = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            out.append(f"{key}={value}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(out)


# --------------------------------------------------------------------------- #
# The wizard.
# --------------------------------------------------------------------------- #


def run_setup(
    project,
    io: Optional[SetupIO] = None,
    ollama: Optional[OllamaClient] = None,
    env: Optional[dict] = None,
    roles: tuple = ROLES,
    pull_embedder: bool = True,
) -> dict:
    """Interactively build per-role config; write config.yaml + any new keys.

    After the roles, offers to pull the local embedding model used by long-term
    memory (`nomic-embed-text` via Ollama). Returns the roles config written.
    """

    io = io or SetupIO()
    ollama = ollama or OllamaClient()
    env = env if env is not None else os.environ

    project.ensure_dirs()
    if set(roles) == set(ROLES):
        io.say("\nForge setup — choose a provider and model for each role.\n")
    else:
        io.say(
            f"\nForge setup — reconfiguring: {', '.join(roles)} "
            "(other roles left unchanged).\n"
        )

    roles_cfg: dict = {}
    for role in roles:
        io.say(f"== Role: {role} ==")
        p_idx = _choose(
            io,
            f"  Provider for '{role}':",
            [PROVIDER_LABELS[p] for p in PROVIDERS],
        )
        provider = PROVIDERS[p_idx]

        if provider == "ollama":
            model = _choose_ollama_model(io, ollama)
            num_ctx = 8192 if role in _LIGHT_ROLES else 32768
            roles_cfg[role] = {"provider": "ollama", "model": model, "num_ctx": num_ctx}
        else:
            model = _choose_model(io, provider)
            _ensure_api_key(io, provider, env, project.config_path)
            roles_cfg[role] = {"provider": provider, "model": model}
        io.say(f"  → {role}: {provider} / {model}\n")

    _write_config(project.config_path, roles_cfg)
    io.say(f"Saved configuration to {project.config_path}\n")

    if pull_embedder and set(roles) == set(ROLES):
        _ensure_embedding_model(io, ollama)
    return roles_cfg


def _ensure_embedding_model(io: SetupIO, ollama: OllamaClient) -> None:
    """Long-term memory needs a local embedding model — pull it now if missing."""

    from forge.config import DEFAULT_CONFIG

    mem = DEFAULT_CONFIG["memory"]
    if not mem.get("long_term") or mem.get("embedder") != "ollama":
        return
    model = mem.get("embedder_model", "nomic-embed-text")
    io.say(f"Long-term memory uses the local embedding model '{model}'.")
    ans = io.ask(f"  Pull '{model}' now via Ollama? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes"):
        io.say(f"  Skipped. Pull it later with: ollama pull {model}")
        return
    if not ollama.ensure_available(io):
        return
    installed = ollama.list_models()
    if any(m == model or m.split(":")[0] == model for m in installed):
        io.say(f"  ✓ {model} already present.")
    else:
        ollama.pull(model, io.say)


def _ensure_api_key(io: SetupIO, provider: str, env: dict, config_path: str) -> None:
    """Prompt for and persist an API key only if it isn't already set."""

    env_var = API_KEY_ENV[provider]
    if env.get(env_var):
        io.say(f"  ✓ {env_var} already set — skipping.")
        return
    io.say(f"  {PROVIDER_LABELS[provider]} needs an API key (${env_var}).")
    while True:
        key = io.secret(f"  Paste {env_var} (input hidden): ").strip()
        if key:
            break
        io.say("  A key is required for this provider.")
    env[env_var] = key  # usable immediately this session
    # config_path is <root>/.forge/config.yaml -> .env sits at <root>/.env
    env_path = os.path.join(_project_root_from_config(config_path), ".env")
    upsert_env_file(env_path, env_var, key)
    io.say(f"  ✓ saved {env_var} to {env_path}")


def _project_root_from_config(config_path: str) -> str:
    # <root>/.forge/config.yaml -> <root>
    return os.path.dirname(os.path.dirname(os.path.abspath(config_path)))


def _write_config(config_path: str, roles_cfg: dict) -> None:
    """Merge the chosen roles into config.yaml, preserving other roles + sections.

    Only the roles just configured are updated; any role not in `roles_cfg` keeps
    its existing entry, so `forge setup --role engineer` doesn't touch the rest.
    """

    data: dict = {}
    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    existing_roles = data.get("roles") or {}
    existing_roles.update(roles_cfg)
    data["roles"] = existing_roles
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
