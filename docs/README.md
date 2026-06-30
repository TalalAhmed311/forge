# Forge — Engineering Documentation

This folder is the engineering reference for Forge: a model-agnostic, loop-engineered
CLI coding agent. The top-level [`README.md`](../README.md) is the *user* guide
(install, quickstart, configuration). These documents are the *internals* — written
for an engineer who has to read, extend, or debug the system.

Forge's central idea is **loop engineering**: it does not answer once, it *acts,
observes a verifiable result (a passing test), and repeats* until the goal is met.
Everything below is in service of that idea.

## Read in this order

| # | Document | What it covers |
|---|---|---|
| 1 | [Architecture overview](01-architecture.md) | The whole system on one page: the two front doors, the shared engine, the `.forge/` state directory, the data-flow, and the design principles that recur everywhere. **Start here.** |
| 2 | [Memory management](02-memory.md) | The three memory tiers in full detail: the tracker (tier-1), episodic short-term (Redis), long-term cross-session (pgvector), embeddings, the multi-pathway router, RRF fusion, progressive disclosure, the code index, sessions, and graceful degradation. |
| 3 | [The interactive agent (`forge agent`)](03-forge-agent.md) | The Claude-Code-style REPL: the model-driven loop, permission modes, undo checkpoints, context compaction, slash commands, subagents, and how it *drives* the shared engine (architect + engineer) rather than duplicating it. |
| 4 | [Tools & the shell command service](04-tools-and-shell.md) | The tool abstraction (`Tool`/`ToolRegistry`/`ToolContext`), provider tool-call normalization, the filesystem/search/memory/capability tools, and `run_command` — the verification primitive — including workspace confinement, timeouts, allowlists, and eval isolation. |
| 5 | [The autonomous run (`forge run`)](05-autonomous-run.md) | The orchestrator: the two nested loops, the clarifier → architect → engineer pipeline, FE/BE routing, escalation handling, session lifecycle, and how a run survives interruption and resumes. |
| 6 | [The harness engine & loops](06-harness-and-loops.md) | A focused treatment of the loops themselves — inner build/verify, outer plan/progress, the agent conversation loop, the provider parse-retry loop — plus the Phase 7 self-improvement harness (lessons, gated skills, eval isolation, harness self-edit proposals). |

## The one-paragraph summary

Forge exposes **one engine** through **two front doors**. `forge run` is autonomous
(fire-and-forget: plan, build, verify, done). `forge agent` is interactive
(conversational, human-in-the-loop). Both share the same `.forge/` state and the
same subsystems: a **clarifier** that resolves vague prompts, an **architect** that
plans a requirement into an ordered, test-bearing task list, **two senior engineers**
(backend + frontend) that drive each task to a passing test, a **three-tier memory**
system (verbatim tracker, episodic Redis stream, cross-session pgvector store), a
**provider layer** that normalizes every model backend behind one interface, and an
optional **self-improvement harness** that learns lessons and verified skills without
ever being able to game its own evaluator.

## Conventions in these docs

- File references are written as `path:line` relative to the repo root, e.g.
  `forge/orchestrator.py:463`.
- "Section N" references (e.g. *Section 9*) are the original design-spec section
  numbers, which the source code comments cite throughout. They are kept here so the
  docs and the code comments line up.
- "Phase N" refers to the build phases the project was developed in; Phase 6 added
  episodic/cross-session memory and Phase 7 added self-improvement.
