"""Rung 1 — Lessons (Phase 7 §3).

A lesson is a short natural-language rule learned from a run, retrieved when a
similar situation recurs (the Reflexion pattern). Lessons are advisory text
injected into context; they are lower-risk than skills and bypass the code
regression gate, but are subject to use/win demotion so a lesson that doesn't
help goes inactive.

Retrieval reuses the existing BM25 index (base spec §8.3) — lessons are just
another small corpus to match against. We do NOT build a second retrieval system.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from forge.memory.episodic import BM25Index, tokenize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Lesson:
    id: str
    trigger: str
    rule: str
    scope: str = "task"
    source_trace: str = ""
    evidence: str = ""
    confidence: float = 0.8
    uses: int = 0
    wins: int = 0
    active: bool = True
    created: str = field(default_factory=_now)

    def win_rate(self) -> float:
        return self.wins / self.uses if self.uses else 1.0

    def render(self) -> str:
        return f"- (when {self.trigger}) {self.rule}"


class LessonStore:
    """Append-only JSONL lessons store with BM25 retrieval over triggers."""

    def __init__(
        self,
        path: str,
        demote_after_uses: int = 8,
        demote_win_rate: float = 0.25,
    ) -> None:
        self.path = path
        self.demote_after_uses = demote_after_uses
        self.demote_win_rate = demote_win_rate

    # -- contract (§3.2) --------------------------------------------------- #

    def add(self, lesson: Lesson) -> str:
        if not lesson.id:
            lesson.id = self._next_id()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(lesson)) + "\n")
        return lesson.id

    def retrieve(self, situation: str, k: int = 5) -> list[Lesson]:
        active = self.active()
        if not active:
            return []
        # Build a throwaway BM25 over trigger+rule text and score the situation.
        index = BM25Index()
        for lesson in active:
            index.add(tokenize(f"{lesson.trigger} {lesson.rule}"))
        scored = [
            (index.score(situation, i), lesson) for i, lesson in enumerate(active)
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [lesson for score, lesson in scored if score > 0.0][:k]

    def record_outcome(self, lesson_id: str, helped: bool) -> None:
        lessons = self._read_all()
        for lesson in lessons:
            if lesson.id == lesson_id:
                lesson.uses += 1
                if helped:
                    lesson.wins += 1
                # Auto-demote a lesson that isn't earning its place (§3.1).
                if (
                    lesson.uses >= self.demote_after_uses
                    and lesson.win_rate() < self.demote_win_rate
                ):
                    lesson.active = False
                break
        self._rewrite(lessons)

    def active(self) -> list[Lesson]:
        return [lesson for lesson in self._read_all() if lesson.active]

    def all(self) -> list[Lesson]:
        return self._read_all()

    # -- persistence ------------------------------------------------------- #

    def _read_all(self) -> list[Lesson]:
        if not os.path.isfile(self.path):
            return []
        lessons: list[Lesson] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    lessons.append(Lesson(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return lessons

    def _next_id(self) -> str:
        return f"L{len(self._read_all()) + 1:04d}"

    def _rewrite(self, lessons: list[Lesson]) -> None:
        """Atomic rewrite (use/win counters mutate; the log of *content* stays
        append-only in spirit — we never drop a lesson, only flip `active`)."""

        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".lessons-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for lesson in lessons:
                    fh.write(json.dumps(asdict(lesson)) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
