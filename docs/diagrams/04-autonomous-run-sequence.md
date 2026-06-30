# Diagram · Autonomous Run Sequence (`forge run`)

The clarifier → architect → engineer pipeline over time, including the outer
plan/progress loop, the inner build/verify loop, escalation, and memory injection.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant O as Orchestrator
    participant C as Clarifier
    participant A as Architect
    participant M as Memory
    participant E as Engineer (BE/FE)
    participant T as Tests (run_command)

    U->>O: forge run "<goal>"
    O->>C: clarity_check(prompt)
    alt ambiguous & unresolvable
        C-->>O: needs_user(question)
        O-->>U: ask ONE question, exit(2)
    else resolved
        C-->>O: resolved intent
    end

    O->>A: plan(intent, context)
    A->>A: strict-JSON plan (retry on bad JSON / no tasks)
    A-->>O: specs/<session>/*.md + FE/BE-tagged tasks

    loop OUTER LOOP — tracker.next_task() until empty
        O->>O: route by surface tag (BE | FE)
        O->>M: gather context + inject recall briefing (ONCE)
        M-->>O: tier-1 + cross-session + session slice

        rect rgb(245,245,245)
        note over E,T: INNER LOOP — build / verify (max_inner_iters)
        loop until tests pass / guardrail
            E->>E: model proposes tool calls
            E->>T: edit code, run_command
            T-->>E: exit code + stdout + stderr
            alt model stops calling tools
                E->>T: run_tests(task.test_command)
                alt pass
                    T-->>E: ✓ done
                else fail
                    T-->>E: ✗ real error fed back
                end
            end
        end
        end

        alt ok
            E-->>O: TaskResult(ok)
            O->>O: mark_done, reindex code
            O->>M: promote distilled card
        else escalate (cap / time / no-progress / model)
            E-->>O: escalate(question)
            O->>A: handle_escalation (bounded by max_escalations)
            A-->>O: revised plan
        else fail
            E-->>O: TaskResult(fail)
            O-->>U: stop — progress saved (forge resume)
        end
    end

    O->>M: finish sessions + regenerate PROJECT.md
    O-->>U: ✓ all tasks complete
```

## Reading it

- **The test is the only success exit** — the engineer signals "done" by stopping
  tool calls, and the orchestrator then runs the test directly (it never trusts the
  model's self-assessment).
- **Failures escalate, they don't crash** — iteration cap, wall-clock budget, and
  no-progress detection all hand the task back to the architect (bounded by
  `max_escalations`) rather than looping forever.
- **Memory is injected once** per task and a distilled card is promoted on success.
- **Restart-safe** — a `fail` stops the run but the tracker on disk is up to date, so
  `forge resume` continues from the next unfinished task.
