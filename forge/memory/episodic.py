"""Episodic store: chunking + dual representation (Section 8.2).

The append-only stream of engineer turns / tool outputs / architect reasoning is
partitioned into overlapping segments. Each sealed chunk keeps BOTH an immutable
raw segment and a short summary, plus a vector embedding of the raw text and a
BM25 entry — the dual representation the multi-pathway router needs (Section 8.3).

Dependency-free by design (Open Decisions, Section 17):
  * Embedding for P_vec: a deterministic hashing vectorizer over token n-grams.
    Crude but zero-dependency and good enough as the "nuance the summary lost"
    failsafe; swap in an API/local model later via the `embedder` hook.
  * Summary for s_i: a heuristic head-of-segment summary by default; pass a
    `summarizer` callable (a cheap model) to upgrade.
Both choices are recorded in DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


# --------------------------------------------------------------------------- #
# P_vec: dependency-free hashing embedder.
# --------------------------------------------------------------------------- #


class HashingEmbedder:
    """Hash token unigrams+bigrams into a fixed vector; L2-normalize.

    Not semantic in the learned sense, but it captures lexical overlap as a dense
    signal that survives paraphrase better than exact keyword match, which is all
    P_vec needs to be: a failsafe alongside summaries (P_global) and BM25 (P_kw).
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = tokenize(text)
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for gram in grams:
            h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both pre-normalized


# --------------------------------------------------------------------------- #
# P_kw: BM25 over raw chunk text.
# --------------------------------------------------------------------------- #


class BM25Index:
    """Incremental BM25. Adding a doc is O(doc length); no global re-index."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs: list[list[str]] = []
        self.doc_freq: dict[str, int] = {}
        self.total_len = 0

    def add(self, tokens: list[str]) -> None:
        self.docs.append(tokens)
        self.total_len += len(tokens)
        for term in set(tokens):
            self.doc_freq[term] = self.doc_freq.get(term, 0) + 1

    def _avgdl(self) -> float:
        return self.total_len / len(self.docs) if self.docs else 0.0

    def score(self, query: str, doc_index: int) -> float:
        if not self.docs:
            return 0.0
        q_terms = tokenize(query)
        doc = self.docs[doc_index]
        if not doc:
            return 0.0
        n = len(self.docs)
        avgdl = self._avgdl() or 1.0
        tf: dict[str, int] = {}
        for t in doc:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            df = self.doc_freq.get(term, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            freq = tf[term]
            denom = freq + self.k1 * (1 - self.b + self.b * len(doc) / avgdl)
            score += idf * (freq * (self.k1 + 1)) / denom
        return score


# --------------------------------------------------------------------------- #
# Chunk + store.
# --------------------------------------------------------------------------- #


@dataclass
class Chunk:
    id: str
    raw: str
    summary: str
    tokens: list[str]
    vec: list[float]


def _default_summarizer(raw: str, max_chars: int = 200) -> str:
    """Heuristic summary: first non-trivial line, trimmed. Upgrade with a model."""

    for line in raw.splitlines():
        line = line.strip()
        if len(line) > 10:
            return line[:max_chars]
    return raw[:max_chars].replace("\n", " ").strip()


class ChunkStore:
    """Sliding-window chunking with overlap and O(1) incremental updates (8.2)."""

    def __init__(
        self,
        chunk_tokens: int = 8000,
        overlap: int = 800,
        embedder: Optional[HashingEmbedder] = None,
        summarizer: Optional[Callable[[str], str]] = None,
    ) -> None:
        if overlap >= chunk_tokens:
            raise ValueError("overlap must be < chunk_tokens")
        self.L = chunk_tokens
        self.S = chunk_tokens - overlap  # stride
        self.embedder = embedder or HashingEmbedder()
        self.summarizer = summarizer or _default_summarizer
        self.bm25 = BM25Index()
        self.chunks: list[Chunk] = []
        # Active buffer carries (token, char) so we can reconstruct raw text.
        self._active_tokens: list[str] = []
        self._active_text: list[str] = []

    def append(self, text: str) -> None:
        """Append a turn; seal chunks as the active buffer fills. O(len)."""

        toks = tokenize(text)
        # Keep raw text aligned with tokens by storing the turn text in pieces.
        self._active_text.append(text)
        self._active_tokens.extend(toks)
        while len(self._active_tokens) >= self.L:
            self._seal()

    def flush(self) -> None:
        """Seal whatever remains as a final (short) chunk."""

        if self._active_tokens:
            self._seal(force=True)

    def _seal(self, force: bool = False) -> None:
        take = min(self.L, len(self._active_tokens))
        sealed_tokens = self._active_tokens[:take]
        raw = "\n".join(self._active_text).strip()
        cid = f"c{len(self.chunks)}"
        chunk = Chunk(
            id=cid,
            raw=raw,
            summary=self.summarizer(raw),
            tokens=sealed_tokens,
            vec=self.embedder.embed(raw),
        )
        self.chunks.append(chunk)
        self.bm25.add(sealed_tokens)

        if force:
            self._active_tokens = []
            self._active_text = []
        else:
            # Carry the overlap region (L - S tokens) forward into the next chunk.
            self._active_tokens = self._active_tokens[self.S :]
            # Approximate the raw carry-forward by keeping the last text piece;
            # exactness isn't required, continuity is (overlap preserves context).
            self._active_text = self._active_text[-1:]

    def get(self, chunk_id: str) -> Optional[Chunk]:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        return None
