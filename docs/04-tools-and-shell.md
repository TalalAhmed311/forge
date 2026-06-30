# 4 · Tools & the Shell Command Service

> How the model *acts* on the world. This document covers the tool abstraction, how
> provider tool-calls are normalized, every tool in the system, and `run_command` —
> the verification primitive — in detail (confinement, timeouts, allowlists, eval
> isolation).

Sources: `forge/tools/*.py`, `forge/providers/base.py`, `forge/providers/ollama.py`.

## 4.1 The tool abstraction (`forge/tools/base.py`)

Every tool is a small object exposing a **JSON schema** (surfaced to the model as a
`ToolSpec`) and returning a structured **`ToolResult`**.

```python
class Tool(ABC):
    name: str
    description: str
    parameters: dict                 # JSON schema
    def spec(self) -> ToolSpec: ...
    def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...

@dataclass
class ToolResult:
    ok: bool
    content: str
    meta: dict = {}
```

### `ToolContext` — everything a tool needs, injected at dispatch

```python
@dataclass
class ToolContext:
    workspace: str            # target repo root; fs/shell tools are confined to it
    config: object            # forge.config.Config
    role: str = "engineer"    # lets a tool refuse a caller (only engineer/agent write)
    grounding: GroundingCache | None
    context_manager: ContextManager | None   # backs search_context/fetch_raw_context
    recall: CrossSessionRecall | None         # backs search_memory
    project_name: str
    session_id: str
    checkpoint: CheckpointManager | None       # backs /undo snapshots
```

The context is the dependency-injection seam: the same tool object behaves correctly
whether it is dispatched by the orchestrator, the interactive agent, or a subagent,
because everything situational arrives through `ctx`.

### `ToolRegistry` — holds a tool set and dispatches calls

`dispatch(call, ctx)` looks up `call["name"]`, runs it with `call["arguments"]`, and
— critically — **catches any exception** so *a tool crash never kills the loop*
(`base.py:88`); it returns `ToolResult(ok=False, content="<ExcType>: <msg>")`. An
unknown tool name returns a structured error too, never a raise.

## 4.2 From model output to a normalized call (`forge/providers/base.py`)

Different providers return tool calls in different shapes. The provider layer
normalizes them all to **one** shape so callers never branch on backend:

```python
{"id": str, "name": str, "arguments": dict}
```

- `normalize_tool_call(call_id, name, raw_arguments)` coerces `raw_arguments` — which
  may be a JSON **string** (OpenAI/Ollama) or an already-decoded **dict**
  (Anthropic) — into a dict, raising `ToolCallParseError` on malformed JSON.
- `validate_tool_calls(calls, tools)` checks each call names a **known** tool and
  that required top-level args are present, raising `ToolCallParseError` otherwise.
  (Deep schema validation is left to the tool itself.)

### The Ollama parse-retry loop (`forge/providers/ollama.py`)

Local models emit malformed tool JSON often enough that a **parse → validate →
retry** loop is mandatory. `OllamaProvider.complete` (`ollama.py:76`):

```
for attempt in 0..max_parse_retries (default 3):
    POST /api/chat  (with num_ctx pinned explicitly)
    try: return _parse(raw, tools)        # normalize + validate
    except ToolCallParseError as exc:
        feed the bad output + the exact parse error back as new messages
        ("Your tool call could not be parsed: <exc>. Re-emit it as a single
          valid tool call with strict JSON arguments.")
raise ToolCallParseError after exhausting retries
```

Two separate retry budgets: `max_parse_retries` (malformed tool JSON, re-prompts the
model) and `max_retries` (transient HTTP/network, handled in `_http.py` with
exponential backoff + `Retry-After`). `num_ctx` is pinned explicitly because
Ollama's small default silently truncates context — the #1 cause of erratic
local-model behavior.

> No module imports a vendor SDK. All hosted adapters POST JSON through
> `forge/providers/_http.py`, which retries 429/5xx with jittered exponential
> backoff and does **not** retry 401/403/404 (those are bugs/bad keys).

## 4.3 The tool catalog

Tools are grouped into role-specific registries by `forge/tools/factory.py`:

| Registry | Tools | Used by |
|---|---|---|
| `engineer_tools()` | read_file, write_file, list_dir, run_command, search_context, fetch_raw_context | the autonomous engineer |
| `agent_tools()` | read_file, list_dir, glob, grep, find_symbol, edit_file, write_file, run_command | the interactive agent |
| `architect_tools()` | read_file, list_dir, run_command, search_context, fetch_raw_context | the architect (inspects, doesn't write) |

The engineer additionally gets `search_memory` and any promoted **skills** when
long-term memory / improve are enabled (added in `orchestrator._setup_memory` /
`_setup_improvement`). The agent gets `todo_write`, `spawn_subagent`, `plan`,
`delegate_task`, and `search_memory`.

### Filesystem tools (`forge/tools/fs.py`)

All paths are **confined to the workspace**. `_resolve` (`fs.py:16`) joins the path
against the realpath of the workspace and refuses anything that escapes via `..` or
an absolute path outside the root (`raise ValueError("path escapes the workspace")`).

| Tool | Notes |
|---|---|
| `read_file` | UTF-8, capped at 200 KB; records a grounding fact ("file exists: …") on success |
| `write_file` | create/overwrite; **role-gated** (only `engineer`/`agent`); snapshots for undo; records a grounding fact |
| `edit_file` | surgical `old_string → new_string`; `old_string` must match exactly and be unique unless `replace_all`; the precise alternative to `write_file` that avoids whole-file drift |
| `list_dir` | sorted entries, dirs suffixed `/` |

`write_file`/`edit_file` are the only mutating fs tools and they:
1. refuse non-`engineer`/`agent` roles,
2. refuse writes under a **protected path** when improve is enabled (eval isolation,
   §4.6),
3. `ctx.checkpoint.snapshot(path)` **before** mutating (so `/undo` works),
4. record a grounding fact.

### Search / navigation tools (`forge/tools/search.py`)

| Tool | Notes |
|---|---|
| `grep` | regex over file contents → `path:line: text`; prefers ripgrep (`rg`) when on PATH, falls back to a pure-Python walk; optional `include` glob; caps at 200 matches; skips `.git/.forge/__pycache__/.venv/node_modules/.pytest_cache` |
| `glob` | path glob (e.g. `**/*.py`) relative to the workspace; skips noise dirs |
| `find_symbol` | symbol-aware: definition site(s) + call sites for a function/class, backed by `CodeIndex` (doc 2 §2.9); faster/more precise than grep for "where is X defined / who calls X"; caches the index, `refresh=true` rebuilds |

### Memory tools (`forge/tools/memory_tools.py`)

| Tool | Backed by | Purpose |
|---|---|---|
| `search_context` | `ctx.context_manager.search` | search **this session's** episodic memory; returns chunk summaries + ids |
| `fetch_raw_context` | `ctx.context_manager.fetch_raw` | expand a chunk id to full raw text (counts against the live-raw cap) |
| `search_memory` | `ctx.recall.recall` | search **long-term across past sessions**; returns a short cited briefing (the cross-session pipeline, doc 2 §2.10) |

### Capability tools (`forge/tools/capabilities.py`)

The agent-only tools that drive the heavy subsystems via injected handlers:
`plan` (→ Architect) and `delegate_task` (→ test-gated Engineer). See
[doc 3 §3.8](03-forge-agent.md).

### Other tools

- `spawn_subagent` (`subagent.py`) — bounded fan-out to a nested agent (doc 3 §3.9).
- `todo_write` (`todo.py`) — a `TodoStore`-backed checklist the agent maintains.
- Promoted **skills** (`improve/skills.py:_SkillTool`) — verified procedures wrapped
  as dispatchable tools (doc 6 §6.6).

## 4.4 The shell command service — `run_command` (`forge/tools/shell.py`)

This is the **verification primitive**: the tool the whole loop-engineering thesis
rests on. It runs a shell command in the workspace and returns exit code + stdout +
stderr.

```python
class RunCommandTool(Tool):
    name = "run_command"
    # params: cmd (required), timeout (optional seconds)
```

### Execution (`_exec`, `shell.py:61`)

```python
subprocess.run(cmd, shell=True, cwd=workspace,
               capture_output=True, text=True, timeout=timeout, env=env)
```

Key behaviors:

- **Confined to the workspace** via `cwd=workspace`.
- **Hard timeout** — defaults to `tools.command_timeout_s` (120s); a timeout returns
  `ok=False` with `meta={"timed_out": True, "exit_code": -1}` rather than hanging.
- **`PYTHONPATH` injection** — the workspace is prepended to `PYTHONPATH` so a
  greenfield project can `import` its own package under a bare `pytest`/`python`.
  Without this, `pytest tests/test_x.py` couldn't import the project package (no
  install, cwd not on `sys.path` for the console script) and *every* task would fail
  verification with an `ImportError` until the iteration cap — even though the code
  was correct. This is a load-bearing detail.
- **Output truncation** — stdout/stderr each truncated to keep head+tail under
  `MAX_OUTPUT_CHARS` (20 KB), with a `[...truncated N chars...]` marker, so a noisy
  command can't blow the context window.
- **Structured result** — `ok = (returncode == 0)`; `content` is a formatted block
  (`$ cmd / exit_code / --- stdout --- / --- stderr ---`); `meta` carries the raw
  `exit_code`, `stdout`, `stderr`.

### Allowlist mode

If `tools.command_allowlist_only` is set, only commands whose first token is on
`tools.command_allowlist` may run; anything else returns a structured refusal. This
is a defense against destructive/network-mutating commands in untrusted contexts.

(Separately, the interactive agent's **permission layer** can gate `run_command`
interactively per first-token; see doc 3 §3.4. The allowlist here is the
*non-interactive* defense.)

### `run_tests` — the deterministic exit check

```python
def run_tests(test_command, workspace, timeout=120) -> ToolResult:
    return RunCommandTool._exec(test_command, workspace, timeout)
```

`run_tests` (`shell.py:105`) is a thin wrapper used by the **inner loop's verify
step** and by `/sync`. It is **deliberately separate from the model-facing tool** so
the loop's exit check never depends on the model *choosing* to call a tool — the
orchestrator runs the test directly. This is the structural guarantee behind
"the model never decides it's done; the test does."

## 4.5 The verification loop in one picture

```
engineer model
   │  proposes write_file / edit_file / run_command
   ▼
ToolRegistry.dispatch ──► RunCommandTool / WriteFileTool / …   (confined, gated)
   │  ToolResult(ok, content=exit_code+stdout+stderr)
   ▼
fed back into the model's context as a tool message
   …model eventually stops calling tools…
   ▼
Engineer._verify → run_tests(task.test_command)   ← DETERMINISTIC, not model-chosen
   pass → TaskResult(ok=True)
   fail → feed the REAL error back, iterate
```

The same `run_command` the model uses to explore is the engine behind the
non-negotiable verification gate — that symmetry is the design.

## 4.6 Eval isolation (write protection, Phase 7)

When self-improvement is enabled (`improve.enabled: true`), the test directories and
`.forge/eval/` are **write-denied** so the optimizer cannot weaken the evaluator it
is being scored against (it can't make tests pass by deleting them).

`protected_root_for` (`fs.py:27`) checks a resolved path against
`improve.protected_paths` (default `["tests/", "test/", ".forge/eval/"]`) — but
**only while `improve.enabled` is true**, so a normal Phase 1–6 run is unaffected.
`write_file`/`edit_file` refuse a protected path with a *corrective* message:

> "refused: '…' is under protected path 'tests/'. Tests and the regression suite are
> read-only. Fix the implementation so the existing tests pass; do not modify the
> tests."

The corrective phrasing matters — it redirects the model to fix the code rather than
the judge. See doc 6 §6.6 for how this anchors the whole self-improvement story.

## 4.7 Summary table — every tool

| Tool | File | Mutates? | Role-gated? | Backed by |
|---|---|---|---|---|
| `read_file` | fs.py | no | no | filesystem (+ grounding) |
| `write_file` | fs.py | yes | engineer/agent | filesystem (+ undo, eval isolation) |
| `edit_file` | fs.py | yes | engineer/agent | filesystem (+ undo, eval isolation) |
| `list_dir` | fs.py | no | no | filesystem |
| `grep` | search.py | no | no | ripgrep / Python walk |
| `glob` | search.py | no | no | `glob` |
| `find_symbol` | search.py | no | no | `CodeIndex` |
| `run_command` | shell.py | side effects | no (allowlist + agent perms) | `subprocess` |
| `search_context` | memory_tools.py | no | no | `ContextManager` |
| `fetch_raw_context` | memory_tools.py | no | no | `ContextManager` |
| `search_memory` | memory_tools.py | no | no | `CrossSessionRecall` |
| `plan` | capabilities.py | writes specs/tracker | agent | `Architect` |
| `delegate_task` | capabilities.py | runs engineer | agent | `Engineer` |
| `spawn_subagent` | subagent.py | depends | agent | nested `AgentLoop` |
| `todo_write` | todo.py | in-memory | agent | `TodoStore` |
| `<skill name>` | improve/skills.py | depends | engineer/agent | promoted skill module |
