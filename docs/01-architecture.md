# 1 · Architecture Overview

> The whole system on one page. Read this before the deep-dives.

## 1.1 The thesis: loop engineering

Forge is not a "prompt the model and print the answer" tool. It is built on the
premise that a model's single response is *unreliable*, but a model placed inside a
loop with a **deterministic, verifiable exit condition** becomes reliable. The exit
condition is almost always **a passing test command**.

This produces a system with two nested loops wrapped around one durable state file:

- **Inner loop (build / verify)** — the *engineer* edits code and runs the task's
  test command. On failure, the *real error output* is fed back and the loop
  iterates. The model never decides it is "done" — the test does.
  (`forge/agents/engineer.py`)
- **Outer loop (plan / progress)** — the *architect* turns a requirement into an
  ordered task list; the orchestrator hands the engineer one task at a time until
  the list is empty. (`forge/orchestrator.py`)

The durable state is `PROJECT_TRACKER.md` (`forge/memory/tracker.py`). Because it is
written atomically and reloaded on every run, a crash or Ctrl-C is survivable:
re-run, read the tracker, resume at the next unfinished task.

## 1.2 Two front doors, one brain

```
                       ┌──────────────────────────────────────────┐
   forge run  ───────► │              SHARED ENGINE                │
   (autonomous)        │                                           │
                       │  clarifier → architect → engineers        │
   forge agent ──────► │  + memory (3 tiers) + tracker + sessions  │
   (interactive)       │  + tools + grounding + improve            │
                       │                                           │
                       └──────────────────────────────────────────┘
                                        │
                                        ▼
                          .forge/  (per-project state on disk)
```

The two entry points are **not** separate implementations. They share the same
`.forge/` directory and call the same subsystems:

| Front door | Source | Style | When to use |
|---|---|---|---|
| `forge run` | `forge/orchestrator.py` via `forge/cli.py:cmd_run` | Autonomous, fire-and-forget | You can state a goal and walk away |
| `forge agent` | `forge/agent/session.py` via `forge/cli.py:cmd_agent` | Interactive REPL, human-in-the-loop | Understanding a repo and steering changes turn-by-turn |

The interactive agent **drives** the heavy subsystems rather than reimplementing
them: its `plan` tool calls the same `Architect`, its `delegate_task` tool calls the
same test-gated `Engineer`, and it reads/writes the same memory, tracker, and
session registry. See `forge/agent/session.py:_run_plan` and `_run_delegate`.

## 1.3 The subsystems

```
forge/
├── cli.py            argument parsing + command handlers (the executable surface)
├── orchestrator.py   the autonomous engine: two nested loops, FE/BE routing
├── clarity.py        vague-prompt handling: resolve-from-context-then-ask
├── grounding.py      anti-hallucination: confirmed-facts cache + discipline rules
├── project.py        the .forge/ layout (paths only)
├── config.py         defaults + deep-merge + validation
├── setup.py          interactive provider/model wizard
│
├── agents/           the "thinking" roles
│   ├── architect.py  plans → specs + ordered, FE/BE-tagged tasks (never writes code)
│   ├── engineer.py   the inner build/verify loop body (the only role that edits code)
│   └── prompts/      system prompts per role (loaded at runtime)
│
├── agent/            the INTERACTIVE layer (forge agent)
│   ├── session.py    the REPL + integration with the shared engine
│   ├── loop.py       the model-driven conversational tool loop
│   ├── permissions.py  permission modes (default/acceptEdits/plan/bypass)
│   └── checkpoint.py   per-turn working-tree snapshots for /undo
│
├── memory/           the three tiers + retrieval (see doc 2)
│   ├── tracker.py    tier-1 authoritative state (PROJECT_TRACKER.md)
│   ├── events.py     short-term episodic log (in-memory | Redis streams)
│   ├── episodic.py   chunking + dual representation + BM25 + hashing embedder
│   ├── longterm.py   cross-session store (in-memory | pgvector)
│   ├── embeddings.py local Ollama embeddings (nomic-embed-text) + hashing fallback
│   ├── router.py     multi-pathway activation (global/vec/kw)
│   ├── fusion.py     reciprocal rank fusion
│   ├── recall.py     the cross-session recall pipeline (search → fuse → aggregate)
│   ├── aggregator.py the router model synthesizes a cited briefing
│   ├── context_manager.py  assembles tier-1 + tier-2 under a token budget
│   ├── code_index.py the code-symbol pathway (ctags-style)
│   ├── disclosure.py summary-first disclosure with auto-expand
│   ├── sessions.py   the session registry (.forge/sessions.json + Postgres mirror)
│   ├── projectmd.py  the consolidated "read me first" PROJECT.md
│   └── factory.py    builds the memory bundle with graceful fallback
│
├── tools/            the actuators (see doc 4)
│   ├── base.py       Tool ABC, ToolRegistry, ToolContext, ToolResult
│   ├── fs.py         read_file / write_file / edit_file / list_dir
│   ├── search.py     grep / glob / find_symbol
│   ├── shell.py      run_command — the verification primitive
│   ├── memory_tools.py  search_context / fetch_raw_context / search_memory
│   ├── capabilities.py  plan / delegate_task (agent → architect/engineer)
│   ├── subagent.py   spawn_subagent (bounded fan-out)
│   ├── todo.py       todo_write
│   └── factory.py    role-specific tool registries
│
├── providers/        the model backends (see doc 4 §4.2)
│   ├── base.py       Provider ABC + normalized Completion/tool-call shapes
│   ├── _http.py      stdlib HTTP with retry/backoff (no SDKs)
│   ├── anthropic.py  openai.py  deepseek.py  ollama.py  mock.py
│   └── registry.py   role → provider resolution + caching
│
└── improve/          Phase 7 self-improvement (see doc 6 §6.6)
    ├── reflect.py    trace → lesson and/or staged skill
    ├── lessons.py    advisory rules (Reflexion); BM25-retrieved, use/win demotion
    ├── skills.py     verified, callable, versioned procedures (Voyager)
    ├── regression.py the frozen eval gate that makes self-edit safe
    └── harness.py    human-gated harness-edit proposals
```

## 1.4 The `.forge/` state directory

State lives **inside the target project**, not in Forge's source tree — that is how
it survives restarts and how a new run knows it is a *continuation*. Layout is owned
by `forge/project.py`:

```
<project>/.forge/
├── config.yaml          provider/model per role + loop/memory/tools settings
├── PROJECT_TRACKER.md   tier-1 authoritative state (goal, tasks, facts, decisions)
├── PROJECT.md           consolidated "read me first" summary (regenerated each close)
├── DECISIONS.md         design-decision log
├── sessions.json        session registry (S1, S2, …)
├── specs/<session>/     per-session architect specs (overview/architecture/...)
├── logs/                per-task execution traces + bad-plan debug dumps
├── episodic/            (reserved) episodic on-disk artifacts
├── lessons.jsonl        Phase 7 learned lessons (append-only)
├── skills/              Phase 7 promoted skills (versioned) + _staged/
├── eval/                Phase 7 FROZEN regression suite (write-denied to the agent)
├── proposals/           Phase 7 harness-edit proposals (human review)
├── trace/               FORGE_TRACE=1 human-readable feature evidence
└── agent/session.json   the interactive agent's saved conversation (for --resume)
```

`forge reset` clears all of this except `config.yaml` (and, unless
`--keep-memory`, also wipes the project's Postgres rows and Redis keys) so a reset
is a true fresh start. See `forge/cli.py:reset_project` and `_clear_project_memory`.

## 1.5 End-to-end data flow (autonomous `forge run`)

```
user prompt
   │
   ▼
clarity_check ───────────► needs_user?  ── yes ─► ask one question, stop
   │ resolved intent
   ▼
Architect.plan ──────────► writes specs/<session>/*.md + tracker tasks (FE/BE-tagged)
   │
   ▼
OUTER LOOP: tracker.next_task()  (repeat until none left)
   │
   ├─ route by surface tag → backend_engineer | frontend_engineer
   ├─ context_manager.gather(task)   tier-1 verbatim + tier-2 retrieved
   ├─ inject memory ONCE  (cross-session briefing + this-session slice)
   │
   ▼  INNER LOOP (Engineer.run_task)
   │     model proposes tool calls → dispatch → feed results back
   │     model stops calling tools → run the task's test command
   │        pass → done;  fail → feed REAL error back and iterate
   │        guardrails: iteration cap · wall-clock budget · no-progress escalation
   │
   ├─ ok      → mark_done, reindex code, promote a distilled memory card
   ├─ escalate→ architect revises the plan (bounded re-escalation)
   └─ fail    → stop, report; progress is saved on disk
   │
   ▼
close session → finish registries → regenerate PROJECT.md
```

The interactive flow (doc 3) is the same engine, but the *model* decides each step
in a conversation instead of the orchestrator stepping a fixed task list.

## 1.6 Cross-cutting design principles

These recur in every subsystem; recognizing them makes the code predictable.

1. **One interface per concern, swappable backends.** Every model call goes through
   `Provider` (`forge/providers/base.py`); every persistent memory has an in-memory
   reference implementation *and* a service-backed one behind the same ABC
   (`EpisodicLog`, `LongTermStore`, `ContextManager`). No module imports a vendor
   SDK; HTTP is stdlib-only (`forge/providers/_http.py`).

2. **Verification over trust.** The engineer's exit is a deterministic test run, not
   the model's self-assessment (`forge/agents/engineer.py:run_task`). The
   self-improvement gate is a frozen test suite the agent cannot edit
   (`forge/improve/regression.py`).

3. **Graceful degradation, never a hard block.** If Redis/Postgres/Ollama-embeddings
   are unreachable, memory falls back to in-memory and the run continues — the
   on-disk tracker is the durability backstop (`forge/memory/factory.py`). Tracing,
   memory writes, and reflection are all wrapped so they "must never break a run."

4. **Atomic, restart-safe state.** The tracker, sessions file, lessons, and skill
   index are all written via temp-file-then-rename (`tracker.py:_atomic_write`).

5. **Write often & cheap, read rarely & up-front.** Short-term events are appended
   continuously; long-term cards are distilled at task completion; cross-session
   memory is injected *once* at task start, with the model pulling more on demand
   via `search_memory`. (`forge/orchestrator.py:_inject_memory`, `_persist_task`.)

6. **Grounding is structural, not vibes.** A confirmed-facts cache + mandatory
   discipline rules in the prompt, plus the verification loop itself as a
   hallucination detector. (`forge/grounding.py`.)

7. **Sessions are namespaced.** Task ids are prefixed by session (`S2-T1`) so runs
   never collide in the shared tracker, and recall is scoped by `project`.

## 1.7 Where to go next

- The memory system is the most intricate part → [doc 2](02-memory.md).
- To follow a real autonomous run line-by-line → [doc 5](05-autonomous-run.md).
- To understand the interactive REPL → [doc 3](03-forge-agent.md).
- For the loops themselves and self-improvement → [doc 6](06-harness-and-loops.md).
