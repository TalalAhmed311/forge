"""Hallucination grounding (Section 11) — structural, not weight-based.

Three mechanisms, wired in from Phase 2:
  1. Evidence-grounded claims: the agent prompts forbid asserting a symbol/file/
     API exists without a citable source. This module supplies the grounding
     cache that the prompt and tracker share.
  2. Verification as detector: handled by the inner loop (Section 6) — a
     hallucinated call fails a test and the error feeds back.
  3. Confidence gate (optional): `consistency_gate` re-samples a critical step.

`GroundingCache` is the "Confirmed facts" store: things the agent has actually
observed (a file it read, a symbol it saw), each with its source. It is fed
forward so the same hallucination cannot recur within a session, and persisted to
the tracker's "Confirmed facts" section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Fact:
    text: str
    source: str


@dataclass
class GroundingCache:
    """In-session confirmed facts, optionally mirrored to the tracker."""

    facts: list[Fact] = field(default_factory=list)
    # Optional sink: orchestrator wires this to tracker.append_fact so confirmed
    # facts survive restarts.
    on_add: Optional[Callable[[str, str], None]] = None
    _seen: set = field(default_factory=set)

    def add(self, text: str, source: str) -> None:
        key = (text, source)
        if key in self._seen:
            return
        self._seen.add(key)
        self.facts.append(Fact(text, source))
        if self.on_add is not None:
            self.on_add(text, source)

    def render(self) -> str:
        """Render the cache for injection into a model prompt."""

        if not self.facts:
            return "(no confirmed facts yet)"
        return "\n".join(f"- {f.text} (seen: {f.source})" for f in self.facts)


GROUNDING_DISCIPLINE = (
    "GROUNDING RULES (mandatory):\n"
    "- Do not claim a file, symbol, function, or API exists unless you have seen "
    "it in retrieved context or a tool result you can cite.\n"
    "- When you rely on a fact, cite its source (file:line, a tool result, or a "
    "confirmed fact).\n"
    "- If something you need is not in context, say so and use a tool to fetch "
    "it. Never invent an import, signature, or path.\n"
)


def consistency_gate(
    sampler: Callable[[], str],
    samples: int = 3,
    normalize: Callable[[str], str] = str.strip,
) -> tuple[bool, list[str]]:
    """Mechanism #3: sample a critical step `samples` times; agree => confident.

    Returns (agreed, outputs). On disagreement the caller should force a
    re-grounding pass (fetch more raw context) before proceeding.
    """

    outputs = [sampler() for _ in range(max(1, samples))]
    agreed = len({normalize(o) for o in outputs}) == 1
    return agreed, outputs
