# 6 · The Harness Engine & Loops

> A focused treatment of the loops themselves — the heart of Forge's "loop
> engineering" — and the Phase 7 self-improvement harness that wraps them. If doc 5
> is "what a run does," this is "why the loops are shaped the way they are, and how
> the system improves itself safely."

Sources: `forge/orchestrator.py`, `forge/agents/engineer.py`, `forge/agent/loop.py`,
`forge/providers/ollama.py`, `forge/improve/*.py`.

## 6.1 Four loops, one philosophy

Forge contains four distinct loops. They differ in their **exit condition**, and that
difference is the whole design.

| Loop | Where | One iteration | Exit condition |
|---|---|---|---|
| **Outer (plan/progress)** | `orchestrator._work_outer_loop` | take the next task, run it | task list empty, or a task fails/stalls |
| **Inner (build/verify)** | `engineer.run_task` | model edits → run the test | the **test passes** (deterministic) |
| **Agent conversation** | `agent/loop.py:run_turn` | model emits tool calls → dispatch | the model stops calling tools (human is the gate) |
| **Provider parse-retry** | `ollama.complete` | call the model → parse tool JSON | valid tool call, or retries exhausted |
| **Improvement (Phase 7)** | wrapped around the outer loop | reflect after each task | runs once per task; promotion is offline |

The unifying philosophy: **a single model response is unreliable; a model inside a
loop with a verifiable exit becomes reliable.** The most important exit is the inner
loop's — a real test run, never the model's self-assessment.

## 6.2 The inner loop in depth (build/verify)

This is the load-bearing loop. Detailed walk-through in [doc 5 §5.6]; here is *why*
each piece exists.

```
            ┌───────────────────────────────────────────────┐
            │  history = system(+grounding) + task(+context) │
            └───────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────▼──────────────────────────┐
        │ for i in 0..max_inner_iters:                        │
        │                                                     │
        │   guardrail: wall-clock budget exceeded? → escalate │
        │   completion = model.complete(history, tools+escalate)
        │                                                     │
        │   ┌── tool calls? ──────────────────────────────┐  │
        │   │  escalate called → hand back to architect    │  │
        │   │  else dispatch, trace, feed results back ─────┼──┘ (loop)
        │   └──────────────────────────────────────────────┘
        │                                                     │
        │   no tool calls ⇒ model thinks it's done           │
        │      verdict = run_tests(test_command)  ◄── TRUTH   │
        │      pass → DONE                                    │
        │      fail → guardrail: same failure ×N? → escalate  │
        │             else feed REAL error back ──────────────┼──┘ (loop)
        └─────────────────────────────────────────────────────┘
                 iteration cap reached → escalate
```

**Why "no tool call ⇒ verify."** The model signals completion by *stopping* tool
calls. The loop never takes that at face value — it runs `run_tests` directly (doc 4
§4.4). Even if the model is confident and wrong, the test catches it and the *actual*
error text (`Tests failed. Output: …`) is appended, so the next iteration sees ground
truth, not a vague restatement.

**Why three guardrails.** Without them, a model can loop forever on an unsatisfiable
task. The iteration cap bounds *count*; `max_seconds` bounds *wall-clock*; the
no-progress detector catches the specific pathology of "the same failure repeats with
no change" (usually a wrong test command or an impossible plan) and escalates *fast*
rather than wasting the whole iteration budget. All three end in **escalation**, not
silent failure — the architect gets a chance to revise.

**Why `escalate` is a pseudo-tool.** It is advertised in the tool specs so the model
can call it naturally, but it is intercepted by the loop (`engineer.py:144`) rather
than dispatched. This keeps "I'm blocked, replan" inside the model's normal
tool-calling channel without a real side effect.

## 6.3 The outer loop in depth (plan/progress)

The outer loop (`orchestrator.py:463`) is a cursor over the tracker:

- It is **stateless across iterations** except for the tracker and a per-task
  escalation counter — which is exactly why `forge resume` works (re-read the
  tracker, continue at `next_task()`).
- Each iteration routes by surface tag, gathers context, **injects memory once**, and
  runs one inner loop.
- Three terminal outcomes per task: **ok** (mark done, reindex, promote a card),
  **escalate** (architect revises; bounded by `max_escalations`), **fail** (stop, but
  progress is already durable on disk).

The tracker is touched at exactly three points — architect writes, engineer reads via
`gather`, orchestrator marks progress — which is the minimal coupling that makes the
whole thing restart-safe.

## 6.4 The agent conversation loop

`AgentLoop.run_turn` (doc 3 §3.3) is the interactive counterpart. Its exit condition
is **the model stops calling tools** — because here the **human is the verification
gate** (you review and steer between turns). It adds the things a long-lived
conversation needs that a one-shot task doesn't: a per-turn undo checkpoint,
permission gating on mutations, and **context compaction** at 75% of the window. It
shares the same provider + tool layer as the engineer; only the exit discipline
differs.

## 6.5 The provider parse-retry loop

The smallest loop, but essential for local models. `OllamaProvider.complete` (doc 4
§4.2) runs **parse → validate → retry**: on malformed tool JSON it re-prompts the
model with the exact parse error appended. This is layered *above* the HTTP retry
loop in `_http.py` (which retries transient 429/5xx with jittered backoff). The two
loops handle orthogonal failure modes — bad model output vs. flaky network — and keep
the higher loops (inner/outer) from ever seeing a malformed tool call.

## 6.6 The self-improvement harness (Phase 7)

With `improve.enabled: true`, an improvement loop wraps the task loop. After each
task the orchestrator **reflects** on the trace and extracts either a **lesson** or a
staged **skill**. The defining property: **self-modification is gated by a frozen
test suite the agent cannot edit**, so it becomes *improvement* rather than *drift*.

```
              after each task (in the hot loop)
                         │
                         ▼
                  Reflector.reflect(task, result, trace)
                  ├─ LESSON  → appended to lessons.jsonl (advisory, cheap)
                  └─ SKILL   → STAGED only (never promoted in the hot loop)

              offline, on `forge improve` (out of the hot loop)
                         │
                         ▼
                  seed eval suite  →  gate each staged skill  →  promote if it passes
                                      (frozen .forge/eval/, write-denied to the agent)
```

This is a three-rung ladder of increasing blast radius and decreasing automation:

### Rung 1 — Lessons (`forge/improve/lessons.py`)

A **lesson** is a short natural-language rule learned from a run (the Reflexion
pattern): `(when <trigger>) <rule>`. Lessons are:

- **advisory** — injected into context as text, *not* ground truth; they bypass the
  code regression gate because they can't directly break anything;
- **retrieved by BM25** over `trigger + rule` (reusing the existing `BM25Index` — no
  second retrieval system) and injected top-k into each gather via the context
  manager's `lessons_hook`;
- **scored** — the orchestrator records use/win outcomes (`_score_lessons`); a lesson
  that is used `demote_after_uses` (8) times with a win rate below `demote_win_rate`
  (0.25) is **auto-demoted** to inactive. Append-only: a lesson is never deleted,
  only flipped inactive.

### Rung 2 — Skills (`forge/improve/skills.py`)

A **skill** is a *verified, reusable procedure* promoted to a callable tool (the
Voyager pattern), so the agent stops re-deriving solutions it already got right. The
contract: a skill module defines `run(workspace, args) -> {"ok": bool, "output":
str}`.

Lifecycle (append-only, versioned, reversible):

- **propose / stage** — reflect writes a candidate to `.forge/skills/_staged/`. Only
  staged from a **passing** run with real code. *Never promoted in the hot loop.*
- **promote** (`forge improve`) — gated: the staged module must import cleanly **and**
  the frozen regression suite must still pass. Promotion copies it to a new version
  file `name__vN.py` and points the index at it. Promotion **never overwrites** a
  prior version.
- **use** — promoted skills become `_SkillTool` instances dispatchable by the
  engineers; each use records uses/regressions.
- **rollback** (`forge improve --rollback`) — just points the index at a prior
  version. Files are never deleted.

### Rung 3 — Harness self-edit (`forge/improve/harness.py`)

The largest blast radius (a prompt/config change affects *every future run*), so it
is **optional and human-gated**. `HarnessAnalyzer` mines recurring failure signatures
from `.forge/logs/`, builds reviewable **proposals** (a rationale + a suggested
prompt edit + a regression-gate result), and writes them to `.forge/proposals/`.
**Nothing here is ever auto-applied** — adopting a proposal is a manual step.

### The keystone — the regression gate (`forge/improve/regression.py`)

Nothing — no skill, no harness edit — is promoted without clearing a **frozen
regression suite** in `.forge/eval/`:

- **seeded** from the project's current passing tests (append-only — the suite only
  grows, never overwrites a frozen case);
- run **by path** (`pytest .forge/eval/`) so it is independent of the project's own
  mutable, agent-writable test dir;
- **write-denied to the agent** via eval isolation (doc 4 §4.6) — the engineer
  literally cannot edit the evaluator it is scored against.

This is the non-gameable signal. Combined with eval isolation, it is what makes
"the agent improves itself" safe: the agent can change its code and propose new
skills, but it can only *keep* a change that still passes a test suite it cannot
touch.

### Reflection (`forge/improve/reflect.py`)

`Reflector.reflect` reads the task, outcome, and trace and returns a `Reflection`
(`lesson_id?`, `skill_name?`). It uses a reflector model (the architect's) when
available and a deterministic heuristic otherwise (a non-passing run yields a lesson
from the last error line in the trace), so reflection stays useful offline / under
the mock provider. Crucially, **a skill is only staged from a passing run with real
code** (`reflect.py:88`), and **promotion is never done here** — only staging.

### `forge improve` operations (`orchestrator.py:558`)

```bash
forge improve              # seed the eval suite, then gate + promote staged skills,
                           #   then write harness proposals (review manually)
forge improve --status     # show active/demoted lessons + promoted/staged skills
forge improve --rollback <skill>   # one-step undo of a promoted skill
```

`cmd_improve` (`cli.py:343`) enables improve even if config leaves it off, because the
command is explicitly about self-improvement.

### Safety invariants (the whole point)

1. **Off by default.** `improve.enabled: false` ⇒ behavior is *exactly* Phase 1–6.
2. **The gate is non-gameable.** Frozen suite + eval isolation ⇒ the agent can't make
   tests pass by weakening them.
3. **Promotion is out of the hot loop.** Reflection stages; `forge improve` gates and
   promotes. The hot loop is never blocked by gating.
4. **Everything is append-only and reversible.** Lessons flip inactive (never
   deleted); skills are versioned (never overwritten); rollback is one step; harness
   edits are proposals, never auto-applied.
5. **Reflection must never break a run.** All reflect/score hooks are wrapped in
   `try/except`.

## 6.7 How the loops compose

```
forge run
  └─ OUTER LOOP (plan/progress) ───────────────────────────────────┐
       per task:                                                    │
       ├─ INNER LOOP (build/verify) ──────────────┐                 │
       │    per iteration:                         │                 │
       │    └─ PROVIDER LOOP (parse-retry)         │                 │
       │         └─ HTTP LOOP (transient retry)    │                 │
       │    exit: run_tests passes                 │                 │
       ├─ ok → mark_done, card, reindex            │                 │
       ├─ escalate → architect revises (bounded)   │                 │
       └─ IMPROVEMENT (Phase 7): reflect → lesson/staged skill ──────┘
          (promotion happens later, offline, behind the frozen gate)
```

Each loop has a clear, single exit condition; failures bubble up one level (a stuck
inner loop escalates to the outer loop's architect; an unparseable tool call retries
at the provider level before the inner loop ever sees it). That layering — verifiable
exits at every level, failures that escalate rather than crash — is the harness
engine.

## 6.8 Tuning knobs (config)

From `forge/config.py`:

```yaml
loop:
  max_inner_iters: 15        # inner-loop iteration cap per task
  max_outer_tasks: 100       # safety cap on the outer loop
  max_seconds: 240           # per-task wall-clock budget (0 disables)
  no_progress_repeats: 3     # identical failures in a row → escalate early
improve:
  enabled: false             # Phase 7 master switch (off ⇒ exact Phase 1-6 behavior)
  lessons: { enabled: true, inject_top_k: 5, demote_after_uses: 8, demote_win_rate: 0.25 }
  skills:  { enabled: true, auto_promote: false }
  protected_paths: ["tests/", "test/", ".forge/eval/"]   # eval isolation
  harness_self_edit: { enabled: false }
```

The orchestrator-level escalation cap (`max_escalations = 2`) is set in code
(`orchestrator.py:129`) and bounds architect ping-pong per task.
