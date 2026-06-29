"""PROJECT_TRACKER.md — tier-1 authoritative state (Section 7.2 / 7.3).

The single source of truth that survives restarts. Plain markdown, human-readable
and machine-parseable. Writes are atomic (temp + rename) so a crash mid-write
never corrupts the spine. It is loaded verbatim into every agent context and is
never summarized.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# `- [ ] T3  Implement auth   | test: `pytest tests/test_auth.py`   ← NEXT`
# Optional `[FE]`/`[BE]` surface tag sits right after the task id.
_TASK_RE = re.compile(
    r"^- \[(?P<done>[ xX])\]\s+(?P<id>\S+)\s+(?:\[(?P<surface>FE|BE)\]\s+)?"
    r"(?P<title>.*?)\s*"
    r"(?:\|\s*test:\s*`(?P<test>[^`]*)`)?\s*(?P<next>←\s*NEXT)?\s*$"
)

# Which engineer owns a task. "frontend" -> Senior UI/UX Engineer; everything
# else -> Senior Software Engineer (backend/generalist), the default route.
SURFACE_FRONTEND = "frontend"
SURFACE_BACKEND = "backend"
_SURFACE_TO_TAG = {SURFACE_FRONTEND: "FE", SURFACE_BACKEND: "BE"}
_TAG_TO_SURFACE = {"FE": SURFACE_FRONTEND, "BE": SURFACE_BACKEND}


@dataclass
class Task:
    id: str
    title: str
    test_command: str
    done: bool = False
    summary: str = ""
    surface: str = SURFACE_BACKEND  # routing target; backend is the default

    def to_line(self, is_next: bool = False) -> str:
        box = "x" if self.done else " "
        tag = _SURFACE_TO_TAG.get(self.surface, "BE")
        line = f"- [{box}] {self.id}  [{tag}] {self.title}"
        if self.test_command:
            line += f"  | test: `{self.test_command}`"
        if is_next and not self.done:
            line += "   ← NEXT"
        return line


@dataclass
class TrackerData:
    project: str = "untitled"
    goal: str = ""
    arch_refs: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)        # rendered fact lines
    decisions: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    """Read/write wrapper around one PROJECT_TRACKER.md file."""

    def __init__(self, path: str) -> None:
        self.path = path

    # -- public contract (Section 7.3) ------------------------------------- #

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def read(self) -> TrackerData:
        if not self.exists():
            return TrackerData()
        with open(self.path, "r", encoding="utf-8") as fh:
            return self._parse(fh.read())

    def read_text(self) -> str:
        """Verbatim file text — what gets injected into agent contexts."""

        if not self.exists():
            return ""
        with open(self.path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write(self, data: TrackerData, agent: str = "forge") -> None:
        self._atomic_write(self._render(data, agent))

    def next_task(self) -> Optional[Task]:
        for task in self.read().tasks:
            if not task.done:
                return task
        return None

    def mark_done(self, task_id: str, summary: str = "") -> None:
        data = self.read()
        for task in data.tasks:
            if task.id == task_id:
                task.done = True
                task.summary = summary
                break
        self.write(data)

    def append_fact(self, fact: str, source: str) -> None:
        data = self.read()
        line = f"{fact} (seen: {source})"
        if line not in data.facts:
            data.facts.append(line)
            self.write(data)

    def append_decision(self, text: str) -> None:
        data = self.read()
        data.decisions.append(f"{datetime.now(timezone.utc).date().isoformat()} {text}")
        self.write(data)

    def add_tasks(self, tasks: list[Task]) -> None:
        data = self.read()
        existing = {t.id for t in data.tasks}
        for t in tasks:
            if t.id not in existing:
                data.tasks.append(t)
        self.write(data)

    def init_empty(self, project: str, goal: str = "") -> None:
        self.write(TrackerData(project=project, goal=goal))

    # -- rendering / parsing ----------------------------------------------- #

    @staticmethod
    def _render(data: TrackerData, agent: str) -> str:
        next_id = next((t.id for t in data.tasks if not t.done), None)
        lines = [
            f"# Project Tracker: {data.project}",
            f"_Last updated: {_now()} by {agent}_",
            "",
            "## Goal",
            data.goal or "(not set)",
            "",
            "## Architecture refs",
        ]
        lines += [f"- {ref}" for ref in data.arch_refs] or ["(none)"]
        lines += ["", "## Tasks"]
        if data.tasks:
            lines += [t.to_line(is_next=(t.id == next_id)) for t in data.tasks]
        else:
            lines.append("(no tasks yet)")
        lines += ["", "## Confirmed facts (grounding cache)"]
        lines += [f"- {f}" for f in data.facts] or ["(none yet)"]
        lines += ["", "## Decisions / escalations"]
        lines += [f"- {d}" for d in data.decisions] or ["(none yet)"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _parse(text: str) -> TrackerData:
        data = TrackerData()
        section = None
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("# Project Tracker:"):
                data.project = line.split(":", 1)[1].strip()
                continue
            if line.startswith("## "):
                section = line[3:].strip().lower()
                continue

            if section == "goal":
                if line and line != "(not set)":
                    data.goal = (data.goal + " " + line).strip() if data.goal else line
            elif section and section.startswith("architecture"):
                if line.startswith("- ") and line[2:].strip() != "(none)":
                    data.arch_refs.append(line[2:].strip())
            elif section == "tasks":
                m = _TASK_RE.match(line)
                if m:
                    data.tasks.append(
                        Task(
                            id=m.group("id"),
                            title=m.group("title").strip(),
                            test_command=(m.group("test") or "").strip(),
                            done=m.group("done").lower() == "x",
                            surface=_TAG_TO_SURFACE.get(
                                m.group("surface") or "", SURFACE_BACKEND
                            ),
                        )
                    )
            elif section and section.startswith("confirmed facts"):
                if line.startswith("- ") and line[2:].strip() != "(none yet)":
                    data.facts.append(line[2:].strip())
            elif section and section.startswith("decisions"):
                if line.startswith("- ") and line[2:].strip() != "(none yet)":
                    data.decisions.append(line[2:].strip())
        return data

    def _atomic_write(self, content: str) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tracker-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)  # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
