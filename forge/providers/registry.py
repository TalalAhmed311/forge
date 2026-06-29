"""Role -> Provider resolution and model routing (Section 5.3).

`config.yaml` assigns a provider+model to each role (architect, engineer, router,
clarifier). The registry turns a role into a concrete `Provider`, so the user can
run, e.g., Claude as architect and a local Qwen-coder as engineer.
"""

from __future__ import annotations

from typing import Callable, Optional

from forge.config import Config
from forge.providers.anthropic import AnthropicProvider
from forge.providers.base import Provider
from forge.providers.deepseek import DeepSeekProvider
from forge.providers.mock import MockProvider
from forge.providers.ollama import OllamaProvider
from forge.providers.openai import OpenAIProvider

# Factories take the role's config dict and build a Provider. Keeping them keyed
# by name makes adding a backend a one-line change.
_FACTORIES: dict[str, Callable[[dict], Provider]] = {}


def register_provider(name: str, factory: Callable[[dict], Provider]) -> None:
    _FACTORIES[name] = factory


def _build_openai(spec: dict) -> Provider:
    kwargs = {"model": spec["model"]}
    for k in ("context_window", "base_url", "api_key", "api_key_env", "max_retries"):
        if k in spec:
            kwargs[k] = spec[k]
    return OpenAIProvider(**kwargs)


def _build_deepseek(spec: dict) -> Provider:
    kwargs = {}
    for k in ("model", "context_window", "base_url", "api_key", "api_key_env", "max_retries"):
        if k in spec:
            kwargs[k] = spec[k]
    return DeepSeekProvider(**kwargs)


def _build_anthropic(spec: dict) -> Provider:
    kwargs = {"model": spec["model"]}
    for k in ("context_window", "base_url", "api_key", "api_key_env", "max_tokens", "max_retries"):
        if k in spec:
            kwargs[k] = spec[k]
    return AnthropicProvider(**kwargs)


def _build_ollama(spec: dict) -> Provider:
    kwargs = {"model": spec["model"]}
    for k in ("context_window", "num_ctx", "base_url", "max_parse_retries", "max_retries"):
        if k in spec:
            kwargs[k] = spec[k]
    return OllamaProvider(**kwargs)


def _build_mock(spec: dict) -> Provider:
    kwargs = {}
    for k in ("context_window", "default_text"):
        if k in spec:
            kwargs[k] = spec[k]
    return MockProvider(**kwargs)


register_provider("openai", _build_openai)
register_provider("deepseek", _build_deepseek)
register_provider("anthropic", _build_anthropic)
register_provider("ollama", _build_ollama)
register_provider("mock", _build_mock)


class Registry:
    """Resolves roles to providers and caches one instance per (provider, model)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._cache: dict[tuple, Provider] = {}

    def for_role(self, role: str) -> Provider:
        spec = self.config.role(role)
        return self._build(spec)

    def _build(self, spec: dict) -> Provider:
        provider_name = spec["provider"]
        if provider_name not in _FACTORIES:
            raise ValueError(
                f"unknown provider '{provider_name}'; "
                f"available: {sorted(_FACTORIES)}"
            )
        key = (provider_name, spec.get("model"), spec.get("num_ctx"))
        if key not in self._cache:
            self._cache[key] = _FACTORIES[provider_name](spec)
        return self._cache[key]

    def describe(self) -> dict[str, dict]:
        """For `forge config`: resolved provider/model/window per role."""

        out = {}
        for role in self.config.data["roles"]:
            spec = self.config.role(role)
            try:
                provider = self.for_role(role)
                window = provider.context_window
            except Exception as exc:  # report, don't crash `forge config`
                window = f"<error: {exc}>"
            out[role] = {
                "provider": spec["provider"],
                "model": spec["model"],
                "context_window": window,
            }
        return out


def inject_provider(registry: Registry, role: str, provider: Provider) -> None:
    """Force a role to use a specific provider instance (used by tests)."""

    spec = registry.config.role(role)
    key = (spec["provider"], spec.get("model"), spec.get("num_ctx"))
    registry._cache[key] = provider
