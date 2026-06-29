"""Minimal, zero-dependency `.env` loader.

Forge reads provider keys straight from `os.environ`, so a `.env` file would be
inert without this. We deliberately avoid python-dotenv to keep the dependency
footprint at just PyYAML.

Semantics: parse `KEY=VALUE` lines (optionally `export KEY=VALUE`), skip blanks
and `#` comments, strip surrounding quotes. A real environment variable always
wins — values already in `os.environ` are never overwritten.
"""

from __future__ import annotations

import os


def load_dotenv(path: str) -> int:
    """Load `path` into os.environ without overriding existing vars.

    Returns the number of variables set. Missing/unreadable files are a no-op.
    """

    if not os.path.isfile(path):
        return 0
    set_count = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        set_count += 1
    return set_count


def load_project_env(project_dir: str) -> int:
    """Load `.env` from the target project dir and the CWD (project dir wins)."""

    seen = set()
    total = 0
    for candidate in (os.path.join(project_dir, ".env"), os.path.join(os.getcwd(), ".env")):
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        total += load_dotenv(candidate)
    return total
