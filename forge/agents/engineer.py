"""The Engineer agent — body of the inner build/verify loop (Section 6).

Given one scoped task, it edits code and runs the project's tests until they pass
or the iteration cap is hit. The model NEVER decides it is done; a deterministic
test run is the only exit (Section 6.2). Grounding mechanisms #1 (evidence
discipline in the prompt) and #2 (verification feeds the real error back) are
wired in here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from forge.agents._prompts import load_prompt
from forge.grounding import GROUNDING_DISCIPLINE
from forge.memory.context_manager import GatheredContext
from forge.memory.tracker import Task
from forge.providers.base import Completion, Message, Provider, ToolSpec
from forge.tools.base import ToolContext, ToolRegistry
from forge.tools.shell import run_tests

# The escalate "tool" is intercepted by the loop, not dispatched to the registry.
ESCALATE_TOOL = ToolSpec(
    name="escalate",
    description=(
        "Stop and hand the task back to the architect when it cannot be completed "
        "within the specs (needs a new dependency or an architecture decision, or "
        "the requirement is ambiguous). Provide a single clear question."
    ),
    parameters={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
)


@dataclass
class TaskResult:
    ok: bool
    summary: str = ""
    reason: str = ""
    escalate: bool = False
    question: str = ""
    iterations: int = 0
    trace: list[str] = field(default_factory=list)
    log_path: str = ""  # set by the orchestrator after the trace is persisted


class Engineer:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        max_inner_iters: int = 15,
        command_timeout_s: int = 120,
        prompt_name: str = "engineer",
        reporter=None,
        event_sink=None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.max_inner_iters = max_inner_iters
        self.command_timeout_s = command_timeout_s
        # Cheap, continuous writes to short-term memory: (type, content, task_id,
        # meta). No-op by default; the orchestrator wires it to the episodic log.
        self.event_sink = event_sink or (lambda *a, **k: None)
        # Level-aware progress sink (msg, level=1); no-op by default so library
        # use stays quiet. Higher levels carry verbose detail (model text, full
        # tool I/O, test output) and are filtered by the orchestrator's verbosity.
        self.report = reporter or (lambda *_a, **_k: None)
        # Which persona this engineer runs as: the Senior Software Engineer
        # ("engineer") or the Senior UI/UX Engineer ("engineer_frontend").
        self.system_prompt = load_prompt(prompt_name)

    def _system(self, tool_ctx: ToolContext) -> Message:
        facts = (
            tool_ctx.grounding.render() if tool_ctx.grounding is not None else "(none)"
        )
        content = (
            f"{self.system_prompt}\n\n{GROUNDING_DISCIPLINE}\n"
            f"## Confirmed facts so far\n{facts}\n"
        )
        return Message("system", content)

    @staticmethod
    def _task_message(task: Task, gathered: GatheredContext) -> Message:
        body = (
            f"{gathered.render()}\n\n"
            f"# YOUR TASK: {task.id} — {task.title}\n"
            f"Verification (this exact command must pass): `{task.test_command}`\n\n"
            "Implement the task. When you believe it is complete, stop calling "
            "tools and state what you changed; the command above will be run to "
            "verify."
        )
        return Message("user", body)

    def run_task(
        self,
        task: Task,
        gathered: GatheredContext,
        tool_ctx: ToolContext,
    ) -> TaskResult:
        history: list[Message] = [self._system(tool_ctx), self._task_message(task, gathered)]
        trace: list[str] = [f"TASK {task.id}: {task.title}"]
        tool_specs = self.tools.specs() + [ESCALATE_TOOL]

        # -vv: dump the full task prompt/context sent to the engineer once.
        self.report(f"    ┌─ task context ─\n{history[1].content}\n    └─", level=3)

        for i in range(self.max_inner_iters):
            self.report(f"    · thinking (iteration {i + 1}/{self.max_inner_iters})…")
            completion = self.provider.complete(history, tools=tool_specs)
            trace.append(self._trace_completion(completion))
            # -v: the model's own message this turn.
            if completion.text.strip():
                self.report(f"    │ {completion.text.strip()}", level=2)

            if completion.tool_calls:
                # Escalation short-circuits the loop (Section 9.3).
                esc = next(
                    (c for c in completion.tool_calls if c["name"] == "escalate"), None
                )
                if esc is not None:
                    question = esc["arguments"].get("question", "(no question given)")
                    trace.append(f"ESCALATE: {question}")
                    self.report(f"    ↑ escalating: {question}")
                    self.event_sink("escalation", question, task.id, {})
                    return TaskResult(
                        ok=False, escalate=True, question=question,
                        iterations=i + 1, trace=trace,
                    )

                history.append(self._assistant_msg(completion))
                for call in completion.tool_calls:
                    result = self.tools.dispatch(call, tool_ctx)
                    trace.append(
                        f"TOOL {call['name']}({_short(call.get('arguments'))}) -> "
                        f"{'ok' if result.ok else 'ERR'}: {_short(result.content)}"
                    )
                    mark = "ok" if result.ok else "ERR"
                    self.report(
                        f"    · {call['name']}({_short(call.get('arguments'), 60)}) "
                        f"→ {mark}"
                    )
                    # Cheap write to short-term memory (durable tool results only).
                    if call["name"] == "write_file" and result.ok:
                        self.event_sink("tool_result", result.content, task.id,
                                        {"tool": "write_file"})
                    # -v: full args + full tool result content.
                    self.report(f"      args: {call.get('arguments')}", level=2)
                    self.report(_indent(result.content, "      │ "), level=2)
                    history.append(
                        Message("tool", result.content, tool_call_id=call["id"])
                    )
                continue

            # No tool call => the model thinks it's done. VERIFY, don't trust.
            self.report(f"    · verifying: {task.test_command or '(no test command)'}")
            verdict = self._verify(task, tool_ctx)
            trace.append(verdict.content[:400])
            if verdict.ok:
                self.report("    ✓ tests passed")
                self.event_sink("test_result", "PASS: " + task.test_command, task.id, {})
                return TaskResult(
                    ok=True, summary=completion.text or "task complete",
                    iterations=i + 1, trace=trace,
                )
            self.report("    ✗ tests failed — feeding the error back")
            # -v: the actual test output that failed.
            self.report(_indent(verdict.content, "      │ "), level=2)
            # Failed: feed the ACTUAL error back and loop (Section 6.2).
            history.append(self._assistant_msg(completion))
            history.append(
                Message(
                    "user",
                    f"Tests failed. Output:\n{verdict.content}\nFix and continue.",
                )
            )

        # Hard iteration cap reached — surface and escalate, never loop forever.
        return TaskResult(
            ok=False,
            reason="iteration cap reached",
            escalate=True,
            question=(
                f"Task {task.id} hit the {self.max_inner_iters}-iteration cap "
                f"without passing `{task.test_command}`. The plan may be wrong."
            ),
            iterations=self.max_inner_iters,
            trace=trace,
        )

    def _verify(self, task: Task, tool_ctx: ToolContext):
        if not task.test_command:
            # No verifiable exit defined: accept but flag it (specs should set one).
            from forge.tools.base import ToolResult

            return ToolResult(ok=True, content="(no test command; accepted unverified)")
        return run_tests(task.test_command, tool_ctx.workspace, self.command_timeout_s)

    @staticmethod
    def _assistant_msg(completion: Completion) -> Message:
        return Message(
            "assistant", completion.text or "", tool_calls=completion.tool_calls or None
        )

    @staticmethod
    def _trace_completion(c: Completion) -> str:
        if c.tool_calls:
            names = ", ".join(tc["name"] for tc in c.tool_calls)
            return f"ASSISTANT calls: {names}"
        return f"ASSISTANT: {_short(c.text)}"


def _short(value, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in str(text).splitlines())
