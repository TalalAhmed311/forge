"""The `.forge/` per-project state directory (Section 4).

`.forge/` lives inside the target project, not in Forge's source tree — that is
how state survives restarts. This module owns its layout and creation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ForgeProject:
    root: str  # the target project directory being operated on

    @property
    def forge_dir(self) -> str:
        return os.path.join(self.root, ".forge")

    @property
    def tracker_path(self) -> str:
        return os.path.join(self.forge_dir, "PROJECT_TRACKER.md")

    @property
    def decisions_path(self) -> str:
        return os.path.join(self.forge_dir, "DECISIONS.md")

    @property
    def specs_dir(self) -> str:
        return os.path.join(self.forge_dir, "specs")

    def session_specs_dir(self, session_id: str) -> str:
        """Per-session spec folder, e.g. .forge/specs/S2/."""

        return os.path.join(self.specs_dir, session_id)

    @property
    def sessions_path(self) -> str:
        return os.path.join(self.forge_dir, "sessions.json")

    @property
    def project_md_path(self) -> str:
        """Consolidated, cumulative project state — read first by a new session."""

        return os.path.join(self.forge_dir, "PROJECT.md")

    @property
    def episodic_dir(self) -> str:
        return os.path.join(self.forge_dir, "episodic")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.forge_dir, "logs")

    # -- Phase 7: self-improvement state ----------------------------------- #

    @property
    def lessons_path(self) -> str:
        return os.path.join(self.forge_dir, "lessons.jsonl")

    @property
    def skills_dir(self) -> str:
        return os.path.join(self.forge_dir, "skills")

    @property
    def eval_dir(self) -> str:
        return os.path.join(self.forge_dir, "eval")

    @property
    def config_path(self) -> str:
        return os.path.join(self.forge_dir, "config.yaml")

    def exists(self) -> bool:
        return os.path.isdir(self.forge_dir)

    def ensure_dirs(self) -> None:
        for d in (self.forge_dir, self.specs_dir, self.episodic_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)
