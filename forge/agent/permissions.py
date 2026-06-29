"""Permission gating for mutating tool calls (Claude-Code-style modes).

Modes:
  - "default"     ask the user before any write/edit/command (remember "always")
  - "acceptEdits" auto-approve file writes/edits; still ask for commands
  - "plan"        read-only: refuse all mutations (the agent plans, doesn't act)
  - "bypass"      approve everything (non-interactive / trusted)

Read-only tools (read_file, list_dir, grep, glob, search_context, todo_write)
never require approval. The approver callback is supplied by the REPL; in tests
it is a stub.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Optional

MUTATING_TOOLS = {"write_file", "edit_file", "run_command"}
EDIT_TOOLS = {"write_file", "edit_file"}
MODES = ("default", "acceptEdits", "plan", "bypass")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


# approver(tool_name, args, preview) -> "yes" | "no" | "always"
Approver = Callable[[str, dict, str], str]


class PermissionManager:
    def __init__(self, mode: str = "default", approver: Optional[Approver] = None,
                 command_allowlist: Optional[list] = None) -> None:
        self.mode = mode if mode in MODES else "default"
        self._approver = approver or (lambda *_: "no")
        self._always: set[str] = set()
        self._command_allowlist = set(command_allowlist or [])

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        return True

    @staticmethod
    def _key(name: str, args: dict) -> str:
        if name == "run_command":
            try:
                first = shlex.split(str(args.get("cmd", "")))[0]
            except (ValueError, IndexError):
                first = ""
            return f"run_command:{first}"
        return name

    def check(self, call: dict, preview: str = "") -> Decision:
        name = call.get("name", "")
        args = call.get("arguments", {}) or {}
        if name not in MUTATING_TOOLS:
            return Decision(True)
        if self.mode == "plan":
            return Decision(False, "plan mode is read-only — present your plan, then "
                                   "switch to an editing mode to apply it")
        if self.mode == "bypass":
            return Decision(True)
        if name in EDIT_TOOLS and self.mode == "acceptEdits":
            return Decision(True)
        key = self._key(name, args)
        if key in self._always:
            return Decision(True)
        if name == "run_command":
            first = key.split(":", 1)[1]
            if first and first in self._command_allowlist:
                return Decision(True)
        verdict = (self._approver(name, args, preview) or "no").lower()
        if verdict == "always":
            self._always.add(key)
            return Decision(True)
        if verdict in ("yes", "y", "allow", "true"):
            return Decision(True)
        return Decision(False, "declined by user")
