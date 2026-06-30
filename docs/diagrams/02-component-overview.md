# Diagram · System Component Overview

The "complete system" picture at component: the two front
doors, the shared engine they shouldn't be confused for separate copies of, the
actuator layers (tools + providers), the three-tier memory subsystem, the on-disk
state, and the external services Forge talks to.

> Renders natively on GitHub and in any Mermaid-aware viewer. Source is editable
> text so it stays in sync with the code.

```mermaid
flowchart TB
    dev([Developer]):::ext
    cli["<b>forge CLI</b><br/>cli.py · arg parsing + command handlers"]
    dev --> cli

    %% ---------------- Front doors ----------------
    subgraph doors["Two front doors — ONE engine"]
        direction LR
        run["<b>forge run</b> — Orchestrator<br/>orchestrator.py<br/>autonomous · 2 nested loops"]
        agent["<b>forge agent</b> — AgentSession / AgentLoop<br/>agent/*.py<br/>interactive REPL · permissions · /undo · compaction"]
    end
    cli --> run
    cli --> agent

    %% ---------------- Shared engine ----------------
    subgraph engine["Shared engine (roles)"]
        direction LR
        clarity["<b>Clarifier</b><br/>clarity.py<br/>resolve-from-context-then-ask"]
        architect["<b>Architect</b><br/>agents/architect.py<br/>plans → specs + ordered tasks"]
        be["<b>Senior Software Eng (BE)</b><br/>agents/engineer.py<br/>inner build/verify loop"]
        fe["<b>Senior UI/UX Eng (FE)</b><br/>agents/engineer.py"]
        ground["Grounding cache<br/>grounding.py"]
        improve["Improve · Phase 7<br/>improve/*.py<br/>lessons · skills · frozen gate"]
    end

    run --> clarity --> architect
    architect -->|"FE/BE-tagged tasks"| be
    architect --> fe
    be -.escalate.-> architect
    fe -.escalate.-> architect

    %% agent drives the same subsystems (not a separate brain)
    agent -.->|"plan tool"| architect
    agent -.->|"delegate_task tool"| be
    agent --> clarity

    be --- ground
    be -.reflect.-> improve

    %% ---------------- Tools ----------------
    subgraph tools["Tools — the actuators · tools/*.py"]
        direction LR
        fst["read/write/edit/list<br/>fs.py"]
        sh["<b>run_command</b><br/>shell.py · verify primitive"]
        srch["grep · glob · find_symbol<br/>search.py"]
        memt["search_context · search_memory<br/>memory_tools.py"]
        cap["plan · delegate_task · spawn_subagent"]
    end
    be --> tools
    fe --> tools
    agent --> tools

    %% ---------------- Providers ----------------
    subgraph prov["Provider layer — providers/*.py"]
        direction LR
        reg["Registry<br/>role → provider"]
        norm["normalize + validate<br/>tool-calls (base.py)"]
        http["_http.py · stdlib, retry/backoff"]
    end
    clarity --> reg
    architect --> reg
    be --> reg
    fe --> reg
    agent --> reg
    reg --> norm --> http

    %% ---------------- Memory ----------------
    subgraph mem["Memory subsystem — memory/*.py"]
        direction TB
        ctx["ContextManager<br/>tier-1 + tier-2 under budget"]
        t1["<b>Tier-1 Tracker</b><br/>PROJECT_TRACKER.md · verbatim, atomic"]
        epi["Episodic (short-term)<br/>events.py · tagged stream"]
        lt["Long-term store<br/>longterm.py · summary+BM25+vector"]
        recall["Recall pipeline<br/>search → RRF fusion → router briefing"]
        sess["Sessions + PROJECT.md<br/>sessions.py · projectmd.py"]
        fac["factory.py<br/>graceful fallback"]
    end
    architect --> ctx
    be --> ctx
    fe --> ctx
    ctx --> t1
    ctx --> epi
    recall --> lt
    be -.search_memory.-> recall
    fac --> epi
    fac --> lt
    fac --> recall

    %% ---------------- State on disk ----------------
    subgraph state[".forge/ — per-project state (survives restarts)"]
        direction LR
        disk["config.yaml · PROJECT_TRACKER.md · PROJECT.md<br/>specs/ · logs/ · sessions.json<br/>lessons.jsonl · skills/ · eval/"]
    end
    t1 --> disk
    architect --> disk
    improve --> disk
    sess --> disk

    %% ---------------- External services ----------------
    subgraph ext["External services"]
        direction LR
        apis[("Model APIs<br/>Anthropic · OpenAI · DeepSeek")]:::ext
        ollama[("Ollama<br/>local models + embeddings")]:::ext
        redis[("Redis<br/>short-term stream")]:::ext
        pg[("Postgres / pgvector<br/>long-term + sessions")]:::ext
    end
    http --> apis
    http --> ollama
    epi -.-> redis
    lt -.-> pg
    sess -.-> pg
    recall -.embeddings.-> ollama

    classDef ext fill:#eee,stroke:#888,stroke-dasharray:4 3,color:#333;
    classDef def fill:#fff,stroke:#444,color:#111;
    class cli,run,agent,clarity,architect,be,fe,ground,improve,fst,sh,srch,memt,cap,reg,norm,http,ctx,t1,epi,lt,recall,sess,fac,disk def;
```

## Legend

- **Solid arrow** — direct call / data flow.
- **Dashed arrow** — optional, best-effort, or tool-mediated (e.g. `escalate`, the
  agent's capability tools, memory writes that degrade gracefully).
- **Cylinders / grey dashed boxes** — external services and on-disk state, i.e. the
  things outside the Python process.

## The three things this diagram is meant to show

1. **One engine, two doors.** `forge run` and `forge agent` both point into the *same*
   clarifier / architect / engineers / memory — the agent reaches them through its
   `plan` and `delegate_task` tools rather than owning copies.
2. **Verification is structural.** `run_command` (the verify primitive) sits in the
   tool layer and is what the inner loop runs to decide "done" — the model never
   self-certifies.
3. **Graceful degradation.** Every external service hangs off a dashed edge; if Redis
   / Postgres / Ollama-embeddings are down, memory falls back to in-memory and the
   on-disk tracker remains the durability backstop.
