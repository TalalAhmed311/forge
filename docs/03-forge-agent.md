# 3 · The Interactive Agent (`forge agent`)

> The conversational, Claude-Code-style front door. This document covers the REPL,
> the model-driven loop, permission modes, undo checkpoints, context compaction,
> slash commands, subagents, and — most importantly — how the agent *drives* the
> shared engine instead of reimplementing it.

Sources: `forge/agent/session.py`, `forge/agent/loop.py`,
`forge/agent/permissions.py`, `forge/agent/checkpoint.py`,
`forge/tools/capabilities.py`, `forge/tools/subagent.py`, `forge/cli.py:cmd_agent`.

## 3.1 What it is (and is not)

`forge agent` is a multi-turn conversation in your terminal: the model reads, edits,
runs commands, and verifies, while **you steer between turns**. It is the right tool
for *understanding a repo and fixing it*.

It is **not a separate brain.** It is the interactive layer *on top of* Forge's
existing subsystems and shares the same `.forge/` state:

| You ask… | The agent engages | `.forge/` it writes |
|---|---|---|
| a question | navigation tools + memory recall (router) | session log |
| a small fix | edits + `run_command` + test verification + improve | session, memory cards |
| a build | clarifier → architect (`plan`) → tasks → engineer (`delegate`) | specs + `PROJECT_TRACKER.md` |

When it needs to plan, it calls the **`Architect`**; when it needs autonomous
execution, it calls the test-gated **`Engineer`**; and it reads/writes the same
memory, tracker, and sessions as `forge run`. **One brain, two front doors.**

## 3.2 Launch & the REPL

```bash
forge agent                          # interactive
forge agent "fix the failing test"   # with an initial request
forge agent --mode plan              # read-only: explore/understand, no edits
forge agent --mode acceptEdits       # auto-apply edits (you can still /undo)
forge agent --resume                 # continue the previous conversation
forge --improve agent                # enable reflection after delegated tasks
```

`cmd_agent` (`cli.py:385`) builds the project, config, and registry, then constructs
an `AgentSession` and calls `repl(initial=...)`.

`AgentSession.repl` (`session.py:349`) is the loop:

1. Read a line (or use the initial prompt).
2. `/`-prefixed → a **slash command** (§3.6).
3. Otherwise: `expand_mentions` inlines any `@path` files.
4. **First substantive turn only**: run the clarifier (`_clarify`) — resolve from
   context or ask exactly one question (no silent guessing).
5. `_briefing` prepends the cross-session memory recall to the message (§3.7).
6. `loop.run_turn(message)` drives the model↔tools loop until the model stops.
7. `_save()` persists the conversation. `Ctrl-C` interrupts the *turn* (not the
   process); `Ctrl-D`/`/quit` exits via `_close()`.

## 3.3 The model-driven loop (`forge/agent/loop.py`)

`AgentLoop` is the conversational tool loop. Unlike the `Engineer` (one scoped task,
gated by a test command), it runs a free-form conversation: the model decides each
tool call, the human steers between turns, mutations are permission-gated, edits are
checkpointed for undo, and long histories are compacted.

`run_turn(user_text)` (`loop.py:62`):

```
checkpoint a fresh undo batch (one per turn)
maybe compact history (if over budget)
append the user message
loop up to max_steps (50):
    if interrupted → record and stop
    completion = provider.complete(history, tools=registry.specs())
    emit assistant_text (if any)
    if no tool calls → append assistant message, RETURN  (turn done)
    append the assistant message (carrying tool_calls)
    for each tool call:
        _run_call(call):
            emit tool_call event
            permissions.check(call, preview)      ← gate mutations
            if denied → return "PERMISSION DENIED: <reason>"
            tools.dispatch(call, tool_ctx)
            emit tool_result event
        append a tool-result message (tool_call_id linked)
```

The loop is **model-driven**: it keeps going as long as the model emits tool calls,
up to `max_steps`. There is no test gate here — the human is the gate. (Contrast the
engineer's loop in doc 6, which *is* test-gated.)

### Context compaction (`_maybe_compact`, `loop.py:110`)

When the history exceeds **75% of the provider's context window**, a cheap
**compactor** model (the `clarifier` role) summarizes the older turns:

- Find user-message boundaries; keep the last `keep_turns` (2) user turns verbatim.
- Summarize everything before that into one message, instructed to preserve
  *decisions, file paths created/edited, commands run and their outcomes, and open
  todos* — terse and factual.
- Rebuild history as `[system, "[Summary of earlier turns] …", <kept tail>]`.

This keeps long sessions inside the window without losing the load-bearing facts.
Token counting is the crude `len // 4` heuristic (`_tok`).

## 3.4 Permission modes (`forge/agent/permissions.py`)

Mutating tools (`write_file`, `edit_file`, `run_command`) are gated; read-only tools
never prompt. Four modes:

| Mode | Edits (`write`/`edit`) | Commands (`run_command`) |
|---|---|---|
| `default` | ask the user (can answer "always") | ask the user |
| `acceptEdits` | auto-approve | ask the user |
| `plan` | **refused** (read-only) | **refused** |
| `bypass` | auto-approve | auto-approve |

`PermissionManager.check` (`permissions.py:59`) returns a `Decision(allowed, reason)`.
Notable behaviors:

- **"always" memory.** Answering `always` adds a key to `_always` so that
  tool/command is auto-approved for the rest of the session. The key for
  `run_command` is `run_command:<first_token>` (`_key`), so approving `pytest` once
  doesn't blanket-approve `rm`.
- **Command allowlist.** A `tools.command_allowlist` from config auto-approves
  commands whose first token is on the list, even in `default` mode.
- **`plan` mode** returns a corrective reason telling the model to present its plan
  then switch to an editing mode — this is how the read-only "explore first" workflow
  is enforced.

The approver callback is `AgentSession._approve` (`session.py:335`): it prints the
preview and reads `y/N/a` from the TTY (returns `"no"` when stdin isn't a TTY, so a
non-interactive agent never blocks).

## 3.5 Undo checkpoints (`forge/agent/checkpoint.py`)

`/undo` reverts the files changed by the **last turn**. Mechanism:

- `checkpoint()` starts a fresh batch at the top of each `run_turn`.
- The write/edit tools call `ctx.checkpoint.snapshot(path)` **before** mutating
  (`fs.py:104`, `fs.py:191`). The *first* snapshot of a file in a batch records its
  prior content — or `None` if the file did not exist.
- `undo()` restores every snapshotted file: `None` → delete the newly-created file;
  otherwise rewrite the prior content. Returns the reverted paths.

So `/undo` is a precise one-step revert of the agent's last batch of changes, and it
works whether or not the project is a git repo. (`/diff` prefers `git diff --stat`
when available, else falls back to the checkpoint's changed paths.)

## 3.6 Slash commands (`session.py:_slash`)

```
/help              show help
/mode <m>          switch permission mode (default|acceptEdits|plan|bypass)
/plan <text>       architect writes specs + tracker tasks for a build
/delegate <text>   hand the next/most-relevant task to the test-gated engineer
/status            show the tracker (tasks + progress)
/sync              run pending tasks' tests + check off the ones that pass
/memory <query>    search cross-session memory
/diff              show working-tree changes
/undo              revert the files changed by the last turn
/todos             show the task checklist
/tools             list available tools
/init              generate a FORGE.md project overview
/commit [msg]      git add -A && git commit
/clear             reset the conversation (keeps files)
/quit              exit (records the session + promotes a memory card)
```

Two of these are the bridge into the shared engine; see §3.8. The rest are
session-management conveniences.

**`/sync` deserves a callout** (`_reconcile_tracker`, `session.py:457`): free-form
building via `edit_file`/`run_command` doesn't touch the tracker on its own. `/sync`
runs each pending task's test command and checks off the ones that now pass —
reconciling the tracker to reality. It also runs automatically on `_close()`.

## 3.7 Per-turn memory & clarification

- **Clarify the first turn.** `_clarify` (`session.py:389`) runs `clarity_check` on
  the first substantive message with a `SimpleContextManager` over tier-1 text. If
  the clarifier needs input, the question is printed and the agent waits for the next
  message; otherwise the resolved intent replaces the message.
- **Inject cross-session memory every turn.** `_briefing` (`session.py:291`) calls
  `recall.recall(message, project, exclude_session)` and prepends the rendered
  briefing (`# CROSS-SESSION MEMORY …`) above the user's message, separated by `---`.
- **Write episodic events.** `_on_event` (`session.py:317`) writes a `tool_result`
  episodic event for successful `write_file`/`edit_file`/`run_command` calls.

## 3.8 Driving the shared engine: capability tools

The agent gets two **capability tools** (`forge/tools/capabilities.py`) whose actual
work is done by handlers the session injects — so the heavy subsystems are *driven,
not duplicated*.

### `plan` → the Architect

`PlanTool` calls `AgentSession._run_plan` (`session.py:210`), which constructs an
`Architect` on the `architect` role and calls `architect.plan(requirement, gathered,
tracker, session_id)`. Result: spec files in `.forge/specs/<session>/` and
session-namespaced tracker tasks. Returns a summary of the planned tasks. Use it for
a substantial feature/app — not a one-line fix.

### `delegate_task` → the test-gated Engineer

`DelegateTaskTool` calls `_run_delegate` (`session.py:227`), which builds an
`Engineer` and runs `engineer.run_task(task, gathered, tool_ctx)` — the *same*
autonomous inner loop `forge run` uses. The agent keeps the conversation while a
sub-task is driven to green (or escalation). On success it marks the task done and
promotes a memory card; if improve is on, it reflects. Returns
`PASSED`/`ESCALATED`/`FAILED` + iteration count + summary.

This is the key architectural point: the interactive agent and the autonomous run
**share the Engineer and Architect implementations**.

## 3.9 Subagents (`forge/tools/subagent.py`)

`spawn_subagent` runs a **nested** `AgentLoop` on a self-contained task and returns
its final report. The nested loop shares the provider/tools/permissions but has its
**own history** and — crucially — **no further spawn tool**, so recursion is bounded
(`_run_subagent`, `session.py:306`). Useful for fan-out ("investigate X", "implement
Y") without polluting the main conversation's context.

## 3.10 The toolset (`agent_tools`, `forge/tools/factory.py`)

The agent's registry is the Claude-Code-style set: navigation + surgical edit +
write + shell + memory.

```
read_file · list_dir · glob · grep · find_symbol      (navigate)
edit_file · write_file                                (mutate, gated)
run_command                                           (run/verify, gated)
todo_write                                            (injected: TodoStore)
spawn_subagent                                        (injected handler)
plan · delegate_task                                  (injected: architect/engineer)
search_memory                                         (added when long-term is on)
```

`edit_file` (surgical `old_string → new_string`) is preferred over `write_file` for
existing files — it is the precise alternative that avoids whole-file drift. See
doc 4 for the tool details.

## 3.11 Persistence & resume

- `_save()` (`session.py:497`) writes `.forge/agent/session.json` atomically after
  every turn: `session_id`, the full message history (role, content, tool_calls,
  tool_call_id), and todos.
- `--resume` calls `_load()` (`session.py:514`): it restores history (dropping the
  old system message, keeping the current one) and todos, and marks `_first_turn`
  False so it doesn't re-clarify.
- `_close()` (`session.py:533`) runs once on exit: `/sync` the tracker, finish both
  session registries as `done`, promote a whole-session memory card
  (`_promote_session_card` distills the conversation into a 2–3 sentence card), and
  regenerate `PROJECT.md`.

## 3.12 The interactive flow at a glance

```
forge agent "add a /health endpoint"
   │
   ├─ build session S<n>, wire memory + tracker + sessions
   ├─ clarify (first turn): resolve from context, or ask one question
   ├─ inject cross-session briefing
   ▼
AgentLoop.run_turn
   │   model: read_file/grep/find_symbol → understand the code
   │   model: edit_file/write_file (permission-gated, snapshotted)
   │   model: run_command pytest → sees exit code + stdout/stderr
   │   (model may call `plan` to spin up the architect, or
   │    `delegate_task` to hand a sub-task to the test-gated engineer)
   │   model stops calling tools → turn ends
   ▼
/sync (or on exit) reconciles the tracker; card promoted on close
```

For the loops' internals and how the engineer's gated loop differs, see
[doc 6](06-harness-and-loops.md). For the tools themselves, see
[doc 4](04-tools-and-shell.md).
