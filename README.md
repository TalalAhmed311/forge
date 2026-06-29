# Forge — CLI Coding Agent

Forge is a model-agnostic command-line coding agent built on **loop engineering**:
it doesn't answer once, it acts, observes the result, and repeats until a
*verifiable* goal (a passing test) is met. Two nested loops run around one durable
state file, `PROJECT_TRACKER.md`:

- **Inner loop (build/verify):** the engineer agent edits code and runs the
  project's tests; failures feed back with their real error output until tests
  pass. The model never decides it's done — the test does.
- **Outer loop (plan/progress):** the architect agent turns a requirement into an
  ordered task list; the engineer clears it one task at a time.

See [FORGE_SPEC.md](FORGE_SPEC.md) for the full design and
[DECISIONS.md](DECISIONS.md) for implementation choices.

## Install

Forge needs **Python 3.9+**. Install it into a virtual environment:

```bash
cd /path/to/forge

# 1. Create + activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux (bash/zsh)
# .venv\Scripts\activate           # Windows PowerShell
# source .venv/bin/activate.fish   # fish shell

# 2. Install Forge. Pick the extras you want:
pip install -e .                   # core + the `forge` command
pip install -e ".[memory]"         # + Redis/pgvector drivers (persistent memory)
pip install -e ".[dev,memory]"     # + pytest, for running the tests
```

`pip install` defines the **`forge` command** via the entry point in
`pyproject.toml` — portable, no hardcoded paths. It lives in `.venv/bin/forge`, so
it's available whenever the venv is active:

```bash
forge --help
python -m pytest -q                # 89 tests should pass (needs the [dev] extra)
```

Run `deactivate` to leave the venv; re-activate with `source .venv/bin/activate`
in any new shell before using `forge`. (`.venv/` is already gitignored.)

Forge talks to every model provider over plain HTTP, so no provider SDK is
required. API keys come from the environment: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`; Ollama targets a local base URL (see
[Running with Ollama](#running-with-ollama-local-models)).

## Quickstart

```bash
forge init                          # create .forge/ in the current project
forge setup                         # interactively pick a provider+model per role
forge run "make tests/test_auth.py pass"   # streams a live progress summary
forge run "..." -v                  # verbose: + model text, full tool I/O, test output, task list
forge run "..." -vv                 # very verbose: + plans, specs, and the full task context
forge run "..." --quiet             # silent: print only the final result
forge status                        # tasks + progress
forge resume                        # continue from NEXT after an interruption
forge reset                         # clear .forge/ state but KEEP config.yaml (fresh start)
forge ask "where is config loaded?"  # answered from context, no code changes
forge config                        # show resolved provider/model per role
forge improve                       # reflect, gate + promote staged skills (Phase 7)
forge improve --status              # show learned lessons + skill library
forge improve --rollback <skill>    # one-step undo of a promoted skill
```

Try it offline with the deterministic mock provider:

```bash
forge --mock run "build a thing"
```

## First-time setup (interactive)

`forge setup` walks each role — architect, clarifier, engineer, frontend_engineer,
router — and asks:

1. **which provider** — Claude (Anthropic), OpenAI, DeepSeek, or Ollama;
2. **which model** — a menu of that provider's models, or type a custom id;
3. for hosted providers, **the API key** — but only if it isn't already in your
   environment/`.env` (otherwise it's skipped); a newly entered key is saved to
   `<project>/.env`;
4. for Ollama, it lists your **installed models** and **pulls** the one you pick if
   it isn't local yet.

It writes the result to `.forge/config.yaml`. `forge run` auto-launches it the first
time if no config exists (skip with `--no-setup`, or in non-interactive/`--mock` use).

Reconfigure just one or a few roles without walking all five — the rest are left
untouched:

```bash
forge setup --role engineer                 # only the backend engineer
forge setup --role engineer --role router   # repeatable …
forge setup --role engineer,frontend_engineer   # … or comma-separated
```

## Configuration

`.forge/config.yaml` (everything is optional; sane defaults apply):

```yaml
roles:
  architect:         { provider: anthropic, model: claude-opus-4-8 }
  engineer:          { provider: ollama,    model: qwen-coder, num_ctx: 32768 }  # Senior Software Engineer (backend)
  frontend_engineer: { provider: ollama,    model: qwen-coder, num_ctx: 32768 }  # Senior UI/UX Engineer (frontend)
  router:            { provider: ollama,    model: qwen, num_ctx: 8192 }
  clarifier:         { provider: openai,    model: gpt-4o-mini }
memory:
  engine: simple        # or "episodic" for the Phase 6 retrieval engine
improve:
  enabled: false        # Phase 7 self-improvement; off => exact Phase 1-6 behavior
```

Per-role overrides are available for quick experiments without editing the file:

```bash
forge --architect-provider openai --architect-model gpt-4o \
      --engineer-provider ollama --engineer-model qwen2.5-coder run "..."
```

## Running with Ollama (local models)

Forge's default config runs the engineers on a **local Ollama** model, so you can
build with no API keys and nothing leaving your machine.

**1. Install Ollama and start it.** Get it from <https://ollama.com>. The desktop app
starts the server automatically; otherwise run `ollama serve`. It listens on
`http://localhost:11434` by default — which is exactly what Forge's Ollama adapter
targets.

**2. Pull the models you'll use:**

```bash
ollama pull qwen2.5-coder      # engineer / frontend_engineer (coding)
ollama pull qwen2.5            # router / clarifier (lighter)
ollama list                    # confirm the exact tag names
```

**3. Point the roles at the tags you pulled.** The model name in `config.yaml` must
match an `ollama list` tag *exactly* (the spec's `qwen-coder` is a placeholder — real
tags look like `qwen2.5-coder`). A fully-local `.forge/config.yaml`:

```yaml
roles:
  architect:         { provider: ollama, model: qwen2.5-coder, num_ctx: 32768 }
  engineer:          { provider: ollama, model: qwen2.5-coder, num_ctx: 32768 }
  frontend_engineer: { provider: ollama, model: qwen2.5-coder, num_ctx: 32768 }
  router:            { provider: ollama, model: qwen2.5,       num_ctx: 8192 }
  clarifier:         { provider: ollama, model: qwen2.5,       num_ctx: 8192 }
```

Or override per run without editing the file:

```bash
forge --engineer-provider ollama --engineer-model qwen2.5-coder run "..."
```

**Notes specific to Ollama:**
- **`num_ctx` is set explicitly** (default 32768) and is what the context manager
  budgets against. Don't rely on Ollama's small default — it silently truncates
  context and is the #1 cause of erratic local-model behavior. Raise it if your model
  and RAM allow.
- **Tool-call parsing is retried.** Local models emit malformed tool JSON more often, so
  the adapter re-prompts (default 3 times) before giving up. Prefer a model with good
  tool-calling support (the `qwen2.5-coder` family works well).
- **Remote / custom host:** set `base_url` per role in `config.yaml`, e.g.
  `{ provider: ollama, model: qwen2.5-coder, base_url: http://192.168.1.50:11434 }`.
- **Verify it's reachable:** `curl http://localhost:11434/api/tags` should list your
  models. `forge config` then shows each role's resolved model and context window.

## Memory: short-term (Redis) + long-term (pgvector)

Three tiers, mapping the E-mem model onto Forge:

| Tier | What | Where |
|---|---|---|
| Procedural | prompts + skills + standards | `forge/agents/prompts/`, `.forge/skills/` |
| Episodic (short-term) | ordered, **agent/task-tagged** session events | Redis stream (per project, TTL) |
| Long-term | per-doc **summary + full-text(BM25) + embedding** | Postgres/pgvector |

Long-term memory is **on by default**. Bring the backends up:

```bash
docker compose up -d                 # redis + pgvector
pip install -e ".[memory]"           # redis, psycopg, pgvector drivers
ollama pull nomic-embed-text         # local 768-dim embeddings (open source, no API key)
```

**Embeddings are local** — `nomic-embed-text` via Ollama; nothing leaves the
machine and no embedding API key is needed.

**Write vs inject policy** (write often & cheap, read rarely & up-front):
- **Write** to short-term after each durable step (cheap append to Redis).
- **Promote** to long-term at task completion as a *distilled card* (what was
  built — files, outcome, decision — not the raw transcript; the raw stays on disk).
- **Inject** once, at the **start of each task**: a cross-session briefing + this
  session's slice. The engineer **pulls more on demand** with `search_memory` —
  never re-injected per step.

**Cross-session recall.** The `search_memory` tool queries long-term across all
three pathways, fuses them with **Reciprocal Rank Fusion** (scale-free; never sums
incomparable scores), and the **`router` role** synthesizes a short **cited
briefing** — raw fetched on demand. That's the router role's real job.

If Redis/Postgres/Ollama-embeddings are unreachable, memory **degrades to
in-memory** and the run continues — the tracker on disk is the durability backstop,
so nothing blocks. Set `memory.long_term: false` to turn it off entirely.

## Senior team: FE/BE routing

The architect leads two senior engineers and tags each task's **surface**. The
orchestrator routes by tag: `[BE]` tasks → the **Senior Software Engineer** (`engineer`,
the default), `[FE]` tasks → the **Senior UI/UX Engineer** (`frontend_engineer`). The
architecture spec defines the API contract both build against; full-stack work is split
into a backend task and a frontend task at that contract. Tone is professional — no
roasting. Point both roles at the same model to run a single backend, or give the
frontend role its own model.

## Self-improvement (Phase 7)

With `improve.enabled: true`, Forge wraps an improvement loop around the task
loop: after each task it **reflects** on the trace, extracts a **lesson**
(advisory rule, retrieved into future contexts) or stages a **skill** (a verified,
reusable procedure). Skills are promoted to callable tools **only** after passing
a frozen regression suite in `.forge/eval/` — and the engineer is structurally
**denied write access** to that suite and to `tests/`, so it can never make tests
pass by weakening them. Everything is append-only and reversible (`--rollback`).
See [FORGE_PHASE7_SELF_IMPROVEMENT.md](FORGE_PHASE7_SELF_IMPROVEMENT.md).

## Architecture

```
orchestrator (two loops)
├── clarity check     clarifier: resolve-from-context-then-ask     (Section 10)
├── architect    →    specs + ordered, FE/BE-tagged tasks          (Section 7)
├── engineers    →    Senior Software (BE) + Senior UI/UX (FE),
│                     build/verify inner loop, gated by tests      (Section 6)
├── providers/        one interface; openai/anthropic/ollama/
│                     deepseek + mock; per-role routing            (Section 5)
├── memory/           tier-1 tracker (verbatim) + tier-2 episodic  (Sections 7, 8)
├── tools/            read / write / list / run / search / fetch    (Section 12)
├── grounding         evidence-cited claims + verification backstop (Section 11)
└── improve/          lessons + verified skills + eval isolation    (Phase 7)
```

## Tests

```bash
python3 -m pytest -q            # 89 tests
```

The suite mirrors the spec's phase exit tests and the later additions: provider
tool-call normalization, the engineer driving a planted failing test to green,
clarity resolution, a 3-task tracker cleared end-to-end with `resume`, Phase 6
cross-session recall + buried-detail recovery, Phase 7 self-improvement (eval
isolation, lessons, gated skill promotion), FE/BE task routing, and the
interactive setup wizard.
