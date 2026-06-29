"""Extract a JSON object from model output (which often wraps it in prose/fences)."""

from __future__ import annotations

import json
from typing import Optional


def extract_json(text: str) -> Optional[dict]:
    """Return the first valid top-level JSON object found in `text`, or None.

    Tolerates ```json fences and surrounding prose by scanning for balanced
    braces and attempting to parse each candidate.
    """

    if not text:
        return None

    # Fast path: the whole string is JSON.
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # Repair path: weaker models often emit a single intended object with STRAY
    # top-level closing braces (e.g. `"architecture.md": "...",\n},\n"tasks":...`),
    # which prematurely terminate the root and make a plain scan drop everything
    # after the first stray `}` (notably the `tasks` array). Drop those stray
    # closers and retry before falling back to the lossy first-object scan.
    repaired = _repair_stray_closers(text)
    if repaired is not None:
        try:
            obj = json.loads(repaired)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Scan for balanced-brace candidates and try each.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        start = -1
    return None


def _repair_stray_closers(text: str) -> Optional[str]:
    """Drop stray top-level `}` tokens that prematurely close the root object.

    Walks the span from the first `{` to the last `}` (string-aware, honoring
    escapes), tracking brace depth. A `}` seen while depth == 1 that is NOT the
    final character would close the root early — in valid JSON that never happens
    before the end, so it is a model artifact and is dropped. Returns the repaired
    string, or None if there is no plausible object span.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    span = text[start : end + 1]

    out: list[str] = []
    depth = 0
    in_str = False
    esc = False
    last = len(span) - 1
    changed = False
    for i, ch in enumerate(span):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == "{":
            depth += 1
            out.append(ch)
        elif ch == "}":
            if depth == 1 and i != last:
                changed = True  # stray premature closer — drop it
                continue
            depth -= 1
            out.append(ch)
        else:
            out.append(ch)
    if not changed:
        return None
    return _normalize_commas("".join(out))


def _normalize_commas(span: str) -> str:
    """String-aware cleanup of comma artifacts left by dropping stray closers:
    collapse runs of commas (`,  ,` -> `,`) and drop trailing commas before a
    closing `}`/`]`. Only touches commas OUTSIDE string literals."""

    out: list[str] = []
    in_str = False
    esc = False
    n = len(span)
    for i, ch in enumerate(span):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch == ",":
            # Look ahead past whitespace for the next significant char.
            j = i + 1
            while j < n and span[j] in " \t\r\n":
                j += 1
            if j < n and span[j] in ",}]":
                continue  # redundant comma (before another comma or a closer)
        out.append(ch)
    return "".join(out)
