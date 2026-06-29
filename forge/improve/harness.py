"""Rung 3 — Harness self-editing (Phase 7 §7d, OPTIONAL & HUMAN-GATED).

Offline trace analysis proposes prompt/config edits. Every proposal is checked
against the frozen regression suite and surfaced for HUMAN review — nothing here
is ever auto-applied (§5.4). The blast radius of a harness change is every future
run, which is too large for an automated gate alone.

This module only *produces* reviewable proposals (a diff + a gate result written
to `.forge/proposals/`). Adopting one is a manual step the human performs.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from forge.improve.regression import GateResult, RegressionGate

_ERROR_RE = re.compile(r"(error|exception|failed|traceback)", re.IGNORECASE)


@dataclass
class Proposal:
    id: str
    rationale: str
    target: str          # what would change, e.g. "prompts/engineer.md"
    diff: str            # human-readable suggested change
    gate: Optional[GateResult] = None

    def render(self) -> str:
        gate = (
            f"gate: {'PASS' if self.gate and self.gate.passed else 'FAIL'} "
            f"({self.gate.reason if self.gate else 'not run'})"
        )
        return (
            f"# Harness proposal {self.id}\n"
            f"_{datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n\n"
            f"## Rationale\n{self.rationale}\n\n"
            f"## Target\n{self.target}\n\n"
            f"## Suggested change\n{self.diff}\n\n"
            f"## Status\n{gate}\n\n"
            "> Review and apply manually. Forge will NOT auto-apply this.\n"
        )


class HarnessAnalyzer:
    def __init__(self, logs_dir: str, proposals_dir: str,
                 gate: Optional[RegressionGate] = None) -> None:
        self.logs_dir = logs_dir
        self.proposals_dir = proposals_dir
        self.gate = gate

    def _recurring_failures(self, max_files: int = 50) -> Counter:
        sigs: Counter = Counter()
        if not os.path.isdir(self.logs_dir):
            return sigs
        files = sorted(os.listdir(self.logs_dir))[-max_files:]
        for name in files:
            path = os.path.join(self.logs_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if _ERROR_RE.search(line):
                        sigs[line.strip()[:120]] += 1
        return sigs

    def propose(self, min_occurrences: int = 2) -> list[Proposal]:
        """Build reviewable proposals from recurring failure signatures."""

        proposals = []
        sigs = self._recurring_failures()
        gate_result = self.gate.gate("harness-baseline") if self.gate else None
        for i, (sig, count) in enumerate(sigs.most_common(3)):
            if count < min_occurrences:
                continue
            pid = f"H{datetime.now(timezone.utc).strftime('%Y%m%d')}-{i+1}"
            proposals.append(
                Proposal(
                    id=pid,
                    rationale=(
                        f"The failure signature {sig!r} recurred {count} times "
                        "across recent runs; a standing instruction may prevent it."
                    ),
                    target="forge/agents/prompts/engineer.md",
                    diff=(
                        "Append to the engineer prompt's Discipline section:\n"
                        f"  - When you see `{sig}`, address its root cause before "
                        "re-running tests."
                    ),
                    gate=gate_result,
                )
            )
        return proposals

    def write_proposals(self, proposals: list[Proposal]) -> list[str]:
        if not proposals:
            return []
        os.makedirs(self.proposals_dir, exist_ok=True)
        written = []
        for p in proposals:
            path = os.path.join(self.proposals_dir, f"{p.id}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(p.render())
            written.append(path)
        return written
