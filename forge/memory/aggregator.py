"""Router aggregation — Stage 2: select + synthesize a cited briefing.

After RRF (`fusion.py`) produces the fused top-N candidate documents, the router
model's job is NOT to re-score them — it is to SELECT the ones actually relevant
to the new task and SYNTHESIZE a short, grounded briefing that cites its sources.
That briefing is what gets injected into the original (architect/engineer) model's
context; the full raw segments stay in long-term storage and are pulled on demand.

A deterministic heuristic fallback runs when no router provider is available, so
cross-session recall still works offline (it just concatenates the top summaries
with citations instead of synthesizing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from forge.providers.base import Message, Provider


@dataclass
class Candidate:
    """One fused retrieval hit handed to the aggregator."""

    doc_id: str
    session_id: str
    summary: str
    kind: str = ""
    score: float = 0.0

    def cite(self) -> str:
        return f"{self.session_id}:{self.doc_id}"


@dataclass
class Briefing:
    """The aggregated cross-session context handed to the original model."""

    text: str
    cited: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.text.strip():
            return ""
        return (
            "# CROSS-SESSION MEMORY (from past sessions — apply if relevant; "
            "fetch_raw a cited id for detail)\n" + self.text
        )


AGGREGATOR_PROMPT = (
    "You are Forge's memory router. A new task is about to be implemented on an "
    "EXISTING project. You are given notes from PAST sessions on this SAME project "
    "(each with a citation id). Write a SHORT briefing telling the implementer what "
    "already exists that this task builds on or must stay consistent with.\n\n"
    "Rules:\n"
    "- Prior work on the same project is almost always relevant context even if it "
    "implemented a DIFFERENT feature: name the modules/files, types, functions, and "
    "conventions (error handling, id format, test layout) the new task should reuse "
    "or match. Include these — do not dismiss them just because the exact feature "
    "differs.\n"
    "- Be concrete and CITE every claim with its id in square brackets, e.g. "
    "[S1:S1-T2]. Never state anything you can't cite.\n"
    "- Keep it under ~150 words. Do not invent files, symbols, or APIs.\n"
    "- ONLY if the notes are genuinely unrelated to the task, reply with the single "
    "word: NONE"
)


class Aggregator:
    def __init__(self, provider: Optional[Provider] = None) -> None:
        self.provider = provider

    def aggregate(self, query: str, candidates: list[Candidate]) -> Briefing:
        if not candidates:
            return Briefing(text="")
        if self.provider is not None:
            return self._synthesize(query, candidates)
        return self._heuristic(candidates)

    def _synthesize(self, query: str, candidates: list[Candidate]) -> Briefing:
        listing = "\n".join(
            f"[{c.cite()}] ({c.kind}) {c.summary}" for c in candidates
        )
        messages = [
            Message("system", AGGREGATOR_PROMPT),
            Message("user", f"New task:\n{query}\n\nCandidate notes:\n{listing}"),
        ]
        text = self.provider.complete(messages).text.strip()
        if self._is_empty_briefing(text):
            return Briefing(text="")
        # Record which candidates were actually cited.
        cited = [c.cite() for c in candidates if f"[{c.cite()}]" in text]
        return Briefing(text=text, cited=cited)

    @staticmethod
    def _is_empty_briefing(text: str) -> bool:
        """Robustly detect a 'nothing relevant' reply. Small models wrap the
        sentinel in brackets/parens or add trailing prose, so normalize first."""

        norm = text.strip().strip("[](){}").strip().lower()
        return (
            not norm
            or norm == "none"
            or norm.startswith("no relevant prior context")
        )

    @staticmethod
    def _heuristic(candidates: list[Candidate]) -> Briefing:
        """No router model: present the top summaries verbatim, with citations."""

        lines = [f"- [{c.cite()}] ({c.kind}) {c.summary}" for c in candidates[:5]]
        return Briefing(text="\n".join(lines), cited=[c.cite() for c in candidates[:5]])
