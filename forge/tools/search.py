"""Code-navigation tools: grep and glob (Claude-Code-style).

`grep` prefers ripgrep (`rg`) when available and falls back to a pure-Python
walk so it works with no external dependency. Both are confined to the workspace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from forge.tools.base import Tool, ToolContext, ToolResult

MAX_MATCHES = 200
_SKIP_DIRS = {".git", ".forge", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents for a regular expression across the workspace and "
        "return matching 'path:line: text'. Optionally restrict by a glob include "
        "(e.g. '*.py')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regular expression"},
            "path": {"type": "string", "description": "subdir to search; default '.'"},
            "include": {"type": "string", "description": "filename glob filter, e.g. '*.py'"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from forge.tools.fs import _resolve

        pattern = args["pattern"]
        root = _resolve(ctx.workspace, args.get("path", "."))
        include = args.get("include")
        if shutil.which("rg"):
            return self._ripgrep(pattern, root, include)
        return self._python(pattern, root, include)

    def _ripgrep(self, pattern, root, include) -> ToolResult:
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "-e", pattern]
        for d in _SKIP_DIRS:
            cmd += ["--glob", f"!{d}/**"]
        if include:
            cmd += ["--glob", include]
        cmd.append(root)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, content=f"grep failed: {exc}")
        lines = (proc.stdout or "").splitlines()
        if not lines:
            return ToolResult(ok=True, content="(no matches)")
        shown = lines[:MAX_MATCHES]
        note = "" if len(lines) <= MAX_MATCHES else f"\n[...{len(lines) - MAX_MATCHES} more]"
        # Make paths workspace-relative for readability.
        rel = [os.path.relpath(l, os.path.dirname(root)) if l.startswith("/") else l
               for l in shown]
        return ToolResult(ok=True, content="\n".join(rel) + note)

    def _python(self, pattern, root, include) -> ToolResult:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult(ok=False, content=f"invalid regex: {exc}")
        import fnmatch

        out: list[str] = []
        base = os.path.realpath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if include and not fnmatch.fnmatch(name, include):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                rel = os.path.relpath(full, base)
                                out.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                                if len(out) >= MAX_MATCHES:
                                    out.append("[...truncated]")
                                    return ToolResult(ok=True, content="\n".join(out))
                except (OSError, UnicodeDecodeError):
                    continue
        return ToolResult(ok=True, content="\n".join(out) or "(no matches)")


def ctx_workspace_of(root: str) -> str:
    # The python fallback reports paths relative to the search root's parent so
    # output matches ripgrep's; here we just use the root itself as the base.
    return root


class FindSymbolTool(Tool):
    """Symbol-aware navigation: where is a function/class defined, and where used.

    Backed by the repo symbol index (forge.memory.code_index). The index is built
    on first use and cached; pass refresh=true to rebuild after large changes.
    """

    name = "find_symbol"
    description = (
        "Look up a code symbol (function or class) across the repo: its "
        "definition site(s) and where it's used. Faster and more precise than grep "
        "for 'where is X defined / who calls X'. Currently indexes Python."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "function or class name"},
            "refresh": {"type": "boolean", "description": "rebuild the index first"},
        },
        "required": ["symbol"],
    }

    def __init__(self) -> None:
        self._index = None
        self._root = None

    def _get_index(self, workspace: str, refresh: bool):
        from forge.memory.code_index import CodeIndex

        if refresh or self._index is None or self._root != workspace:
            self._index = CodeIndex.build(workspace)
            self._root = workspace
        return self._index

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        symbol = (args.get("symbol") or "").strip()
        if not symbol:
            return ToolResult(ok=False, content="symbol is required")
        index = self._get_index(os.path.realpath(ctx.workspace), bool(args.get("refresh")))
        defs = index.lookup(symbol)
        uses = index.callsites(symbol, max_hits=20)
        lines = []
        if defs:
            lines.append(f"definitions of `{symbol}`:")
            lines += [f"  {d.kind} {d.path}:{d.line}" for d in defs]
        else:
            lines.append(f"no definition of `{symbol}` found in the index")
        if uses:
            lines.append(f"used at ({len(uses)} site(s), capped):")
            lines += [f"  {p}:{ln}: {src[:160]}" for p, ln, src in uses]
        return ToolResult(ok=True, content="\n".join(lines))


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files whose path matches a glob pattern (e.g. '**/*.py', 'app/*.html'), "
        "relative to the workspace. Returns matching paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob, e.g. '**/*.py'"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        import glob as _glob

        root = os.path.realpath(ctx.workspace)
        pattern = args["pattern"]
        matches = _glob.glob(os.path.join(root, pattern), recursive=True)
        rels = []
        for m in sorted(matches):
            rel = os.path.relpath(m, root)
            if any(part in _SKIP_DIRS for part in rel.split(os.sep)):
                continue
            rels.append(rel + ("/" if os.path.isdir(m) else ""))
        if not rels:
            return ToolResult(ok=True, content="(no matches)")
        shown = rels[:MAX_MATCHES]
        note = "" if len(rels) <= MAX_MATCHES else f"\n[...{len(rels) - MAX_MATCHES} more]"
        return ToolResult(ok=True, content="\n".join(shown) + note)
