"""Code symbol index — the code-specific 4th pathway (Section 8.5).

The episodic router is dialogue-shaped; code is related by symbols, not narrative
similarity. This is a PARALLEL system: given a query mentioning `parse_config`,
the high-leverage signal is "where is this symbol defined / used," not embedding
similarity.

Open Decision (Section 17): ctags-style regex over identifiers rather than
tree-sitter — simpler to start, zero dependency. Recorded in DECISIONS.md. The
`lookup`/`callsites` contract is stable, so a tree-sitter backend can drop in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Python-first definition patterns; extend per language as needed.
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE)
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.MULTILINE)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

CODE_EXTENSIONS = {".py"}
SKIP_DIRS = {".git", ".forge", "__pycache__", ".venv", "venv", "node_modules"}


@dataclass
class Definition:
    symbol: str
    path: str
    line: int
    kind: str  # "def" | "class"


@dataclass
class CodeIndex:
    root: str
    defs: dict = field(default_factory=dict)        # symbol -> list[Definition]
    _files: list = field(default_factory=list)       # (path, text)

    @classmethod
    def build(cls, root: str) -> "CodeIndex":
        index = cls(root=root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if os.path.splitext(name)[1] not in CODE_EXTENSIONS:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(path, root)
                index._files.append((rel, text))
                index._index_file(rel, text)
        return index

    def _index_file(self, rel: str, text: str) -> None:
        for pattern, kind in ((_DEF_RE, "def"), (_CLASS_RE, "class")):
            for m in pattern.finditer(text):
                symbol = m.group(1)
                line = text.count("\n", 0, m.start()) + 1
                self.defs.setdefault(symbol, []).append(
                    Definition(symbol, rel, line, kind)
                )

    def lookup(self, symbol: str) -> list[Definition]:
        return self.defs.get(symbol, [])

    def callsites(self, symbol: str, max_hits: int = 20) -> list[tuple]:
        """Return (path, line, text) where `symbol` appears as an identifier."""

        hits = []
        word = re.compile(rf"\b{re.escape(symbol)}\b")
        for rel, text in self._files:
            for i, line in enumerate(text.splitlines(), start=1):
                if word.search(line):
                    hits.append((rel, i, line.strip()))
                    if len(hits) >= max_hits:
                        return hits
        return hits

    def query(self, text: str, max_symbols: int = 5) -> list[str]:
        """Render code hits for identifiers in `text` that we have definitions for.

        Returns formatted snippets: each known symbol's definition site(s) plus a
        few call sites — exactly the symbol-graph signal Section 8.5 wants.
        """

        seen = []
        rendered = []
        for ident in _IDENT_RE.findall(text):
            if ident in seen or ident not in self.defs:
                continue
            seen.append(ident)
            lines = [f"symbol `{ident}`:"]
            for d in self.defs[ident]:
                lines.append(f"  defined ({d.kind}) at {d.path}:{d.line}")
            for path, line, src in self.callsites(ident, max_hits=3):
                lines.append(f"  used at {path}:{line}: {src}")
            rendered.append("\n".join(lines))
            if len(seen) >= max_symbols:
                break
        return rendered
