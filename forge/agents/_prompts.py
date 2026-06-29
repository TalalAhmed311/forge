"""Load system prompts from the prompts/ directory at runtime (Section 4)."""

from __future__ import annotations

import functools
import os

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


@functools.lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load `prompts/<name>.md`. Cached; prompts don't change within a run."""

    path = os.path.join(_PROMPT_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
