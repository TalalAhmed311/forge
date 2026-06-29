"""Filesystem tools: read_file, write_file, list_dir (Section 12).

All paths are confined to the target workspace; a path that escapes it (via `..`
or an absolute path outside the root) is refused.
"""

from __future__ import annotations

import os

from forge.tools.base import Tool, ToolContext, ToolResult

MAX_READ_BYTES = 200_000


def _resolve(workspace: str, path: str) -> str:
    """Resolve `path` against the workspace, refusing anything outside it."""

    root = os.path.realpath(workspace)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    resolved = os.path.realpath(candidate)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"path '{path}' escapes the workspace")
    return resolved


def protected_root_for(workspace: str, resolved_path: str, config) -> str:
    """Return the protected root `resolved_path` falls under, or "" if none.

    Eval isolation (Phase 7 §5.1): when self-improvement is enabled, the test
    directory(ies) and `.forge/eval/` are write-denied so the optimizer cannot
    weaken the evaluator it is being scored against. Only enforced while
    `improve.enabled` is true, so a Phase 1-6 run is unaffected (§10 checklist).
    """

    if not config.get("improve", "enabled", default=False):
        return ""
    root = os.path.realpath(workspace)
    for rel in config.get("improve", "protected_paths", default=[]) or []:
        protected = os.path.realpath(os.path.join(root, rel))
        if resolved_path == protected or resolved_path.startswith(protected + os.sep):
            return rel
    return ""


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace and return its contents."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "workspace-relative path"}},
        "required": ["path"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args["path"])
        if not os.path.isfile(path):
            return ToolResult(ok=False, content=f"no such file: {args['path']}")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(MAX_READ_BYTES + 1)
        truncated = len(data) > MAX_READ_BYTES
        data = data[:MAX_READ_BYTES]
        # Grounding mechanism #1: a read is citable evidence.
        if ctx.grounding is not None:
            rel = os.path.relpath(path, os.path.realpath(ctx.workspace))
            ctx.grounding.add(f"file exists: {rel}", source=f"read_file {rel}")
        note = "\n[...truncated]" if truncated else ""
        return ToolResult(ok=True, content=data + note, meta={"path": path})


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a workspace file with the given content."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        # Only the engineer may write (Section 12).
        if ctx.role != "engineer":
            return ToolResult(
                ok=False, content=f"role '{ctx.role}' is not permitted to write files"
            )
        path = _resolve(ctx.workspace, args["path"])
        # Eval isolation: refuse writes to protected (evaluator) paths with a
        # corrective error, so the engineer fixes the code, not the judge.
        protected = protected_root_for(ctx.workspace, path, ctx.config)
        if protected:
            return ToolResult(
                ok=False,
                content=(
                    f"refused: '{args['path']}' is under protected path "
                    f"'{protected}'. Tests and the regression suite are read-only. "
                    "Fix the implementation so the existing tests pass; do not "
                    "modify the tests."
                ),
            )
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(args["content"])
        rel = os.path.relpath(path, os.path.realpath(ctx.workspace))
        if ctx.grounding is not None:
            ctx.grounding.add(f"file exists: {rel}", source=f"write_file {rel}")
        return ToolResult(ok=True, content=f"wrote {len(args['content'])} bytes to {rel}")


class ListDirTool(Tool):
    name = "list_dir"
    description = "List entries of a workspace directory."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "defaults to '.'"}},
        "required": [],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", "."))
        if not os.path.isdir(path):
            return ToolResult(ok=False, content=f"not a directory: {args.get('path', '.')}")
        entries = sorted(os.listdir(path))
        lines = []
        for e in entries:
            full = os.path.join(path, e)
            lines.append(f"{e}/" if os.path.isdir(full) else e)
        return ToolResult(ok=True, content="\n".join(lines) or "(empty)")
