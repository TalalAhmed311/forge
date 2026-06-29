"""The regression gate (Phase 7 §5.2).

Nothing — no skill, no harness edit — is promoted without clearing a FROZEN
regression suite in `.forge/eval/`, which the agent cannot write to (eval
isolation, §5.1). The suite starts as a copy of the project's current passing
tests and only grows; it is the non-gameable signal that turns self-modification
into improvement rather than drift.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    output: str = ""


class RegressionGate:
    def __init__(
        self,
        workspace: str,
        eval_dir: str,
        test_command: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self.workspace = workspace
        self.eval_dir = eval_dir
        # Run the frozen suite by path so it is independent of the project's own
        # (mutable, agent-writable) test dir.
        self.test_command = test_command or f'python3 -m pytest "{eval_dir}" -q'
        self.timeout = timeout

    def has_suite(self) -> bool:
        if not os.path.isdir(self.eval_dir):
            return False
        for _, _, files in os.walk(self.eval_dir):
            if any(f.startswith("test_") and f.endswith(".py") for f in files):
                return True
        return False

    def seed_from(self, source_dirs: list[str]) -> list[str]:
        """Copy current test files into the frozen suite. Append-only: existing
        eval files are never overwritten (the suite only grows)."""

        os.makedirs(self.eval_dir, exist_ok=True)
        copied = []
        for rel in source_dirs:
            src = os.path.join(self.workspace, rel)
            if not os.path.isdir(src):
                continue
            for dirpath, _, files in os.walk(src):
                for name in files:
                    if not (name.startswith("test_") and name.endswith(".py")):
                        continue
                    dest = os.path.join(self.eval_dir, name)
                    if os.path.exists(dest):
                        continue  # never overwrite a frozen case
                    shutil.copy2(os.path.join(dirpath, name), dest)
                    copied.append(name)
        return copied

    def gate(
        self, change_id: str, validator: Optional[Callable[[], None]] = None
    ) -> GateResult:
        """Run the frozen suite with the proposed change active.

        `validator` is an optional pre-check (e.g. import the staged skill) that
        must not raise. Then the regression suite must pass for promotion.
        """

        if validator is not None:
            try:
                validator()
            except Exception as exc:  # the change itself is malformed
                return GateResult(False, reason=f"change '{change_id}' invalid: {exc}")

        if not self.has_suite():
            return GateResult(
                False,
                reason=(
                    "no frozen regression suite in .forge/eval/ — seed it from the "
                    "project's passing tests before promoting anything"
                ),
            )

        try:
            proc = subprocess.run(
                self.test_command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return GateResult(False, reason="regression suite timed out")

        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return GateResult(True, reason="regression suite passed", output=output)
        return GateResult(False, reason="regression suite failed", output=output)
