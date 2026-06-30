# 5 · The Autonomous Run (`forge run`)

> The fire-and-forget front door. This document walks the orchestrator end to end:
> the clarifier → architect → engineer pipeline, the two nested loops, FE/BE routing,
> escalation, the session lifecycle, and how a run survives interruption.

Sources: `forge/orchestrator.py`, `forge/clarity.py`, `forge/agents/architect.py`,
`forge/agents/engineer.py`, `forge/cli.py`.

## 5.1 The command surface

```bash
forge run "make tests/test_auth.py pass"   # plan → build → verify → done
forge run "..." -v                          # + model text, full tool I/O, test output
forge run "..." -vv                         # + plans, specs, full task context
forge run "..." --quiet                     # only the final result
forge run "..." -i                          # stay alive for follow-up requests
forge resume                                # continue from NEXT after an interruption
forge status                                # tasks + progress
forge ask "where is config loaded?"         # answered from context, no code changes
```

`cmd_run` (`cli.py:214`) optionally launches the setup wizard (first run, no config),
builds an `Orchestrator` with a verbosity-aware reporter, ensures `.forge/` exists,
and calls `orch.run(prompt)`. `_print_report` (`cli.py:422`) turns the `RunReport`
into an exit code (0 done · 1 failed · 2 needs-user) and, on failure, prints the
recovery options (`forge resume`, `forge status`, edit the tracker).

### Verbosity levels

A single level-aware reporter (`orchestrator.py:81`) filters messages:
`0` = `--quiet` · `1` = default summary · `2` = `-v` (model text, full tool I/O, test
output) · `3` = `-vv` (plans, specs, full context). Each `self.report(msg, level=N)`
prints only if `N <= verbosity`.

## 5.2 The orchestrator's construction

`Orchestrator.__init__` (`orchestrator.py:60`) wires the whole engine:

- the **tracker** (`Tracker(project.tracker_path)`);
- the **context manager** — `SimpleContextManager` or `EpisodicContextManager`
  depending on `memory.engine` (`_build_context_manager`);
- a **grounding cache** whose `on_add` mirrors confirmed facts into the tracker;
- **two senior engineers** — `backend_engineer` (`engineer` role, prompt `engineer`)
  and `frontend_engineer` (`frontend_engineer` role, prompt `engineer_frontend`),
  both carrying the loop guardrails (`max_inner_iters`, `max_seconds`,
  `no_progress_repeats`);
- the **architect** (`architect` role);
- Phase 7 **improvement** wiring (`_setup_improvement`, off by default);
- **persistent memory** (`_setup_memory`) — the bundle, the `search_memory` tool on
  both engineers, an episodic event sink per agent, and the session identity.

`max_escalations = 2` bounds how many times one task may bounce back to the architect.

## 5.3 Stage 1 — clarity (`forge/clarity.py`)

Terse prompts ("fix the auth thing") are handled with **resolve-from-context-then-
ask**, never silent guessing:

1. **`is_unambiguous`** — a heuristic gate. A prompt is ambiguous if it is very short
   (< 4 words), matches a **vague pattern** (`"fix the … thing"`, `"make it work"`,
   `"it's broken"`), or **leans on information the agent wasn't given** (`"as we
   discussed"`, `"the spec/figma/ticket"`, `"you sent"`). The last category is
   important: even a long, fluent prompt is under-specified if it references an
   external artifact Forge can't see, so it is routed to the clarifier rather than
   waved through.
2. If unambiguous → pass through untouched as the resolved intent.
3. Otherwise → a **cheap retrieval pass** (small window) gathers context and the
   `clarifier` model tries to disambiguate, returning strict JSON.
4. If the model is confident → use its `resolved` text. If it needs input → return
   `needs_user=True` with exactly **one** targeted question. If it gives nothing
   usable → fail safe by asking, not guessing.

A `needs_user` outcome ends the run early with status `needs_user` (exit code 2); the
session is finished in both registries (`_finish_session`).

## 5.4 Stage 2 — the architect plans (`forge/agents/architect.py`)

The architect **plans, never writes code**. `plan(requirement, gathered, tracker,
session_id)` asks the model for a strict-JSON plan and applies it.

### Robust JSON parsing (`_ask`, `architect.py:98`)

Weaker models (e.g. `gpt-4o-mini`) intermittently emit malformed plan JSON — stray
braces, or unescaped quotes inside long markdown spec values. `_ask` defends:

- `extract_json` repairs the brace case;
- on unparseable JSON **or a plan with no tasks**, it re-prompts up to
  `max_retries` (2) with `_JSON_FIX_PROMPT` — explicit rules (escape quotes, no
  embedded JSON examples, `tasks` is mandatory and non-empty);
- returns the best parsed dict seen, so an explicit empty-tasks plan still surfaces
  as "no tasks" rather than a crash.

### Applying the plan (`_apply_plan`, `architect.py:128`)

1. **Write spec files** into `.forge/specs/<session>/` (so sessions don't overwrite
   each other). It tolerates weaker models that emit spec files as top-level
   `*.md`/`*.txt` keys instead of nesting them under `specs`.
2. **Parse tasks**, **namespacing ids by session** (`S2-T1`) so they never collide,
   and normalizing the `surface` tag to `frontend`/`backend` (`_normalize_surface`).
3. **One atomic tracker write**: set the goal, append spec refs, append new tasks (or
   replace an *unfinished* task of the same id — escalation may revise it).

Back in `run()`, a plan that fails to parse, or one with **zero tasks**, is a
**failure** (not a vacuous success). The raw response is saved to
`.forge/logs/<stamp>-architect-plan.log` for debugging.

## 5.5 The outer loop (`_work_outer_loop`, `orchestrator.py:463`)

The outer loop is the plan/progress loop. It repeats up to `max_outer_tasks` (100):

```
task = tracker.next_task()
if task is None:  → RunReport(status="done")     # all tasks complete

engineer = backend or frontend, by task.surface tag
gathered = context_manager.gather(task.title, window)
_inject_memory(gathered, task)                   # cross-session briefing + session slice, ONCE
result  = engineer.run_task(task, gathered, tool_ctx)
append result.trace to episodic context; score lessons; reflect (Phase 7)

if result.ok:
    mark_done; reindex code; persist a distilled card; continue
elif result.escalate:
    bump per-task escalation count; if > max_escalations → fail
    architect.handle_escalation(...)  → revise the plan; continue
else:
    → RunReport(status="failed", failed_task=task.id)   # progress is saved
```

### FE/BE routing (`_engineer_for`, `orchestrator.py:326`)

The architect tags each task's **surface**; the orchestrator routes `[FE]` →
`frontend_engineer` (Senior UI/UX Engineer), everything else → `backend_engineer`
(Senior Software Engineer, the default). Full-stack work is split into a backend task
and a frontend task at the API contract the architecture spec defines. Point both
roles at the same model to run a single backend, or give the frontend role its own.

## 5.6 The inner loop (`Engineer.run_task`, `forge/agents/engineer.py`)

The inner loop is the build/verify loop — the body of the whole thesis. Given one
scoped task, the engineer edits code and runs the task's test command until it passes
or a guardrail trips. **The model never decides it's done; a deterministic test run
is the only success exit.**

```
history = [system(+grounding facts), task_message(rendered context + the test command)]
for i in 0..max_inner_iters (15):
    [guardrail] wall-clock budget exceeded?  → escalate
    completion = provider.complete(history, tools + ESCALATE_TOOL)

    if tool calls:
        if `escalate` called → hand back to the architect with a question
        else dispatch each call, trace it, write durable events, feed results back
        continue

    # no tool call ⇒ the model thinks it's done. VERIFY, don't trust:
    verdict = run_tests(task.test_command)        ← deterministic
    if verdict.ok → TaskResult(ok=True)
    else:
        [guardrail] same failure signature N times in a row? → escalate (no progress)
        feed the REAL test output back ("Tests failed. Output: … Fix and continue.")
hit the iteration cap → escalate
```

### Three guardrails (so the loop can never flail)

1. **Iteration cap** — `max_inner_iters` (15). On reaching it, escalate with a
   message that the plan may be wrong.
2. **Wall-clock budget** — `loop.max_seconds` (240s; `0` disables). Checked at the
   top of each iteration.
3. **No-progress early-escape** — if the **same** test-failure signature (MD5 of the
   first 500 chars) repeats `no_progress_repeats` (3) times with no change, the
   engineer is stuck (e.g. an unsatisfiable test command) — escalate fast instead of
   burning the whole iteration cap.

### Grounding wired in

The engineer's system message embeds `GROUNDING_DISCIPLINE` (don't claim a
file/symbol/API exists without a citable source) plus the confirmed-facts cache
(`_system`, `engineer.py:83`). Mechanism #2 — verification feeds the *real* error
back — is the loop itself: a hallucinated call fails the test and the error returns
to the model.

### The `escalate` pseudo-tool

`ESCALATE_TOOL` (`engineer.py:24`) is advertised to the model but **intercepted by
the loop, not dispatched to the registry**. When the model calls it (needs a new
dependency / an architecture decision / the requirement is ambiguous), the loop
returns `TaskResult(escalate=True, question=...)`.

## 5.7 Escalation handling (back in the outer loop)

When `result.escalate` is set, the orchestrator:

1. increments a **per-task** escalation counter; if it exceeds `max_escalations` (2),
   the run fails with "escalated N times without progress" (a permanently-stuck task
   can't ping-pong with the architect forever);
2. otherwise calls `architect.handle_escalation(question, task, gathered, tracker)`,
   which logs the escalation as a decision and **revises the plan** (may add or
   replace tasks);
3. `continue`s the outer loop — `next_task()` re-reads the (possibly revised) tracker.

## 5.8 The session lifecycle

- **Start** (`run`): register the session in both registries; if the project already
  has sessions or a `PROJECT.md`, mark it a **continuation** and orient from
  `PROJECT.md` + current files.
- **During**: durable episodic events accumulate; completed tasks promote distilled
  long-term cards; the code index is refreshed after each task.
- **Close** (`_close_session`): finish both registries with the goal + completed task
  ids, and **regenerate `PROJECT.md`** from the filesystem + tracker + registry.
- **Early exits** (`needs_user`, planning failure) call `_finish_session` so the
  session is always marked terminal in both registries.

All bookkeeping is wrapped so it "must never break the run's result."

## 5.9 Resume & restart-safety

`forge resume` (`orchestrator.resume`) requires an existing tracker and simply
re-enters `_work_outer_loop()` — `next_task()` finds the first unfinished task. This
works because:

- the tracker is the **single source of truth**, written **atomically**, and reloaded
  every run;
- task ids are session-namespaced, so a continuation never collides with prior work;
- nothing in-memory is load-bearing — the on-disk tracker is the durability backstop.

So Ctrl-C, a crash, or a machine reboot loses at most the in-flight task's progress;
everything completed is checked off and durable.

## 5.10 `forge ask` and the interactive session

- **`ask(question)`** (`orchestrator.py:540`) — an ad-hoc query answered **from
  context, no code changes**: it gathers context and asks the architect model to
  answer strictly from it (refusing to invent files/symbols/APIs).
- **`forge run -i` / `forge session`** — after the run, `_interactive_loop`
  (`cli.py:262`) keeps the process alive. Each build request builds a **fresh
  orchestrator** so it gets its own continuation session id (S2, S3, …); `/status`,
  `/ask`, `/improve`, and `/resume` are available. (This is distinct from the
  conversational `forge agent`, which is a single model-driven session.)

## 5.11 The `RunReport`

`run`/`resume` return a `RunReport(status, message, question, completed, failed_task)`
where `status ∈ {done, needs_user, failed, no_tasks}`. The CLI maps it to an exit
code and, on failure, prints actionable next steps. The contract is intentionally
small so both the CLI and tests can assert on it directly.

## 5.12 The autonomous run at a glance

```
forge run "build X"
  └─ clarity_check ──► needs_user? → ask one question, exit(2)
       │ resolved
       └─ architect.plan ──► specs/<session>/*.md + namespaced, FE/BE-tagged tasks
            │ (no tasks ⇒ failure, raw saved for debugging)
            └─ OUTER LOOP  (tracker.next_task until empty)
                 ├─ route FE/BE → engineer
                 ├─ gather context + inject memory once
                 └─ INNER LOOP (engineer ↔ tests)
                      ├─ tool calls → dispatch → feed back
                      └─ no tools → run_tests → pass? done : feed error, iterate
                                    guardrails: iters · wall-clock · no-progress
                 ├─ ok       → mark_done, reindex, promote card
                 ├─ escalate → architect revises plan (bounded)
                 └─ fail     → stop; progress saved → `forge resume`
            └─ close: finish registries, regenerate PROJECT.md
```

For the loop mechanics in isolation and the self-improvement harness, continue to
[doc 6](06-harness-and-loops.md).
