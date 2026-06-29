"""Consolidated project state — `.forge/PROJECT.md`.

The cumulative "current truth" a new session reads FIRST (instead of wading
through every prior session's specs): what the project is, its current directory
tree, a map of key modules/symbols, the architecture decisions, and a one-line
log of each session. Regenerated deterministically at session close from the
filesystem + tracker + session registry — cheap and always accurate.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from forge.memory.code_index import SKIP_DIRS, CodeIndex

_SKIP = SKIP_DIRS | {"node_modules", "dist", "build", ".pytest_cache", ".idea"}


def dir_tree(root: str, max_entries: int = 200, max_depth: int = 4) -> str:
    """A compact directory tree of the project, skipping noise dirs."""

    lines: list[str] = []
    root = os.path.abspath(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        indent = "  " * depth
        if rel != ".":
            lines.append(f"{indent}{os.path.basename(dirpath)}/")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            lines.append(f"{indent}  {name}")
            count += 1
            if count >= max_entries:
                lines.append("  …(truncated)")
                return "\n".join(lines)
    return "\n".join(lines) or "(empty)"


def module_map(root: str, max_files: int = 40) -> str:
    """Key symbols per file, from the code index — the 'where things live' map."""

    index = CodeIndex.build(root)
    by_file: dict[str, list[str]] = {}
    for symbol, defs in index.defs.items():
        for d in defs:
            by_file.setdefault(d.path, []).append(f"{d.symbol} ({d.kind})")
    if not by_file:
        return "(no indexed symbols yet)"
    lines = []
    for path in sorted(by_file)[:max_files]:
        syms = ", ".join(sorted(set(by_file[path]))[:12])
        lines.append(f"- {path}: {syms}")
    return "\n".join(lines)


def generate_project_md(project, tracker, registry) -> str:
    """Build PROJECT.md text and write it. Returns the text."""

    data = tracker.read()
    sessions = registry.all()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    session_log = [
        f"- {s.id} ({s.status}): {s.goal or s.prompt}"
        + (f"  → {', '.join(s.tasks)}" if s.tasks else "")
        for s in sessions
    ] or ["- (none yet)"]

    decisions = [f"- {d}" for d in data.decisions] or ["- (none yet)"]

    text = "\n".join([
        f"# Project: {data.project}",
        f"_Consolidated state · updated {now} · sessions: "
        f"{', '.join(s.id for s in sessions) or '(none)'}_",
        "",
        "> Read this first. It is the current, cumulative truth about the project. "
        "Per-session specs live in `specs/<session>/`; this file is the summary.",
        "",
        "## Purpose",
        data.goal or "(not set)",
        "",
        "## Current structure",
        "```",
        dir_tree(project.root),
        "```",
        "",
        "## Module map (where key symbols live)",
        module_map(project.root),
        "",
        "## Architecture decisions",
        *decisions,
        "",
        "## Session log",
        *session_log,
        "",
    ])

    project.ensure_dirs()
    with open(project.project_md_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
