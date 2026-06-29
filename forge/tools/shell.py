"""Shell tool: run_command — the verification primitive (Section 12).

Confined to the workspace directory, with a hard timeout. If
`tools.command_allowlist_only` is set, only commands whose first token is on the
allowlist may run (defense against destructive/network-mutating commands).
"""

from __future__ import annotations

import shlex
import subprocess

from forge.tools.base import Tool, ToolContext, ToolResult

MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2:]
    return f"{head}\n[...truncated {len(text) - MAX_OUTPUT_CHARS} chars...]\n{tail}"


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside the workspace and return exit code, stdout, "
        "and stderr. Use this to run tests and builds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "the command line to run"},
            "timeout": {"type": "integer", "description": "seconds; optional"},
        },
        "required": ["cmd"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        cmd = args["cmd"]
        cfg = ctx.config
        timeout = int(args.get("timeout") or cfg.get("tools", "command_timeout_s", default=120))

        if cfg.get("tools", "command_allowlist_only", default=False):
            allow = set(cfg.get("tools", "command_allowlist", default=[]) or [])
            try:
                first = shlex.split(cmd)[0]
            except (ValueError, IndexError):
                first = ""
            if first not in allow:
                return ToolResult(
                    ok=False,
                    content=f"command '{first}' not on allowlist {sorted(allow)}",
                )

        return self._exec(cmd, ctx.workspace, timeout)

    @staticmethod
    def _exec(cmd: str, workspace: str, timeout: int) -> ToolResult:
        # Put the workspace on PYTHONPATH so a greenfield project can import its
        # OWN packages under the bare `pytest`/`python` the test commands use.
        # Without this, `pytest tests/test_x.py` can't import the project package
        # (no install, cwd not on sys.path for the console script) and every task
        # fails verification with an ImportError until the iteration cap — even
        # though the code is correct.
        import os as _os
        env = dict(_os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = workspace + (_os.pathsep + existing if existing else "")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                content=f"command timed out after {timeout}s: {cmd}",
                meta={"exit_code": -1, "timed_out": True},
            )
        body = (
            f"$ {cmd}\n"
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{_truncate(proc.stdout)}\n"
            f"--- stderr ---\n{_truncate(proc.stderr)}"
        )
        return ToolResult(
            ok=proc.returncode == 0,
            content=body,
            meta={
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
        )


def run_tests(test_command: str, workspace: str, timeout: int = 120) -> ToolResult:
    """Deterministic verification used by the inner loop (Section 6.1).

    Separate from the model-facing tool so the loop's exit check never depends on
    the model choosing to call a tool.
    """

    return RunCommandTool._exec(test_command, workspace, timeout)
