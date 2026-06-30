# Diagram · Inner Build/Verify Loop (state machine)

The engineer's per-task loop — the heart of Forge's loop-engineering thesis. The
model never decides it is done; a deterministic test run is the only success exit,
and three guardrails guarantee the loop can never flail forever.

```mermaid
stateDiagram-v2
    [*] --> Thinking: task + context + test command

    Thinking --> CheckBudget: provider.complete()
    CheckBudget --> Escalate: wall-clock budget exceeded
    CheckBudget --> Decide: within budget

    Decide --> ToolCalls: model emitted tool calls
    Decide --> Verify: no tool calls (model thinks it's done)
    Decide --> Escalate: model called escalate()

    ToolCalls --> Thinking: dispatch (edit/run/read), feed results back

    Verify --> Done: run_tests() PASS
    Verify --> CheckProgress: run_tests() FAIL

    CheckProgress --> Escalate: same failure xN (no progress)
    CheckProgress --> Thinking: feed REAL error back, iterate

    Thinking --> Escalate: iteration cap reached

    Done --> [*]: TaskResult(ok=True)
    Escalate --> [*]: TaskResult(escalate=True, question)

    note right of Verify
        Deterministic gate.
        run_tests() is called by the
        loop, not chosen by the model.
    end note

    note right of CheckProgress
        Guardrails:
        1. max_inner_iters (count)
        2. max_seconds (wall-clock)
        3. no_progress_repeats (same
           failure signature N times)
    end note
```

## Reading it

- **`Verify` is the only path to `Done`** — and it runs the test command directly, so
  a confident-but-wrong model is caught and the *actual* error text is fed back into
  the next `Thinking` step.
- **Every exit is bounded** — the three guardrails all route to `Escalate`, which
  hands the task back to the architect with a concrete question instead of failing
  silently or spinning.
- **`escalate()` is a pseudo-tool** — advertised to the model but intercepted by the
  loop, so "I'm blocked, replan" travels through the normal tool-calling channel
  without a real side effect.
