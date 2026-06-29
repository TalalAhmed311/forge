"""Embedders for the long-term vector pathway (P_vec).

`OllamaEmbedder` (default) runs **nomic-embed-text** locally via Ollama — open
source, 768-dim, no API key, nothing leaves the machine. `HashingEmbedder` (from
episodic.py, dependency-free) is the offline fallback. Both expose `.dim` and
`.embed(text) -> list[float]`; the pgvector column dimension must match whichever
is configured (768 for nomic-embed-text — see db/init.sql).
"""

from __future__ import annotations

from typing import Protocol

from forge.memory.episodic import HashingEmbedder  # offline fallback, re-exported
from forge.providers._http import post_json

__all__ = ["Embedder", "OllamaEmbedder", "HashingEmbedder"]


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    """Local embeddings via Ollama (default model: nomic-embed-text, 768-dim)."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        dim: int = 768,
        base_url: str = "http://localhost:11434",
    ) -> None:
        self.model = model
        self.dim = dim
        self.base_url = base_url.rstrip("/")

    def embed(self, text: str) -> list[float]:
        raw = post_json(
            f"{self.base_url}/api/embeddings",
            {"model": self.model, "prompt": text},
        )
        return raw["embedding"]
