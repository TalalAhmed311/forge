"""Working-tree checkpoints for undo.

Before a tool mutates a file, the write/edit tools call `snapshot(path)`. The
first snapshot of a file within the current checkpoint records its prior content
(or that it did not exist). `undo()` restores every file to its recorded prior
state and clears the checkpoint, giving a one-step revert of the agent's last
batch of changes. `checkpoint()` starts a fresh batch (called per user turn).
"""

from __future__ import annotations

import os
from typing import Dict, Optional


class CheckpointManager:
    def __init__(self) -> None:
        # path -> prior content, or None if the file did not exist before.
        self._snapshots: Dict[str, Optional[str]] = {}

    def checkpoint(self) -> None:
        """Begin a new batch (drop snapshots from the previous turn)."""
        self._snapshots = {}

    def snapshot(self, path: str) -> None:
        path = os.path.realpath(path)
        if path in self._snapshots:
            return  # keep the EARLIEST state in this batch
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._snapshots[path] = fh.read()
            except OSError:
                self._snapshots[path] = None
        else:
            self._snapshots[path] = None  # newly created -> undo deletes it

    def has_changes(self) -> bool:
        return bool(self._snapshots)

    def changed_paths(self) -> list[str]:
        return sorted(self._snapshots)

    def undo(self) -> list[str]:
        """Restore all snapshotted files; return the paths that were reverted."""
        reverted = []
        for path, prior in self._snapshots.items():
            try:
                if prior is None:
                    if os.path.isfile(path):
                        os.remove(path)
                        reverted.append(path)
                else:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(prior)
                    reverted.append(path)
            except OSError:
                continue
        self._snapshots = {}
        return reverted
