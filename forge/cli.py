"""Forge CLI (Section 13).

    forge init                 create .forge/ in the current project
    forge run "<prompt>"       clarity -> architect -> engineer loop
    forge resume               read the tracker, continue from NEXT
    forge status               print tracker tasks + progress
    forge ask "<question>"     ad-hoc query answered from context
    forge config               show resolved provider/model per role

Per-role overrides (`--architect-provider`, `--engineer-model`, ...) and a
`--mock` switch (run every role offline) are allowed for quick experiments.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Optional

from forge._env import load_project_env
from forge.config import ROLES, load_config
from forge.memory.tracker import Tracker
from forge.orchestrator import Orchestrator
from forge.project import ForgeProject
from forge.providers.registry import Registry
from forge.setup import run_setup


def _build_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {"roles": {}}
    for role in ROLES:
        spec = {}
        prov = getattr(args, f"{role}_provider", None)
        model = getattr(args, f"{role}_model", None)
        if args.mock:
            spec = {"provider": "mock", "model": "mock"}
        if prov:
            spec["provider"] = prov
        if model:
            spec["model"] = model
        if spec:
            overrides["roles"][role] = spec
    if not overrides["roles"]:
        del overrides["roles"]
    return overrides


def _reporting(args: argparse.Namespace):
    """Return (sink, verbosity) for run/resume.

    --quiet => no output. Default => level-1 summary. -v => level 2 (model text,
    full tool I/O, test output). -vv => level 3 (plans, specs, full context).
    """

    if getattr(args, "quiet", False):
        return None, 0
    verbosity = 1 + (getattr(args, "verbose", 0) or 0)
    return (lambda msg: print(msg, flush=True)), verbosity


def _make_orchestrator(
    args: argparse.Namespace, extra: Optional[dict] = None,
    reporter=None, verbosity: int = 1,
) -> Orchestrator:
    project = ForgeProject(root=os.path.abspath(args.dir))
    overrides = _build_overrides(args)
    if extra:
        overrides = {**overrides, **extra}
    config = load_config(project.config_path, overrides=overrides)
    registry = Registry(config)
    return Orchestrator(project, config, registry, reporter=reporter, verbosity=verbosity)


# -- command handlers ------------------------------------------------------- #


def cmd_init(args) -> int:
    project = ForgeProject(root=os.path.abspath(args.dir))
    if project.exists():
        print(f".forge/ already exists at {project.forge_dir}")
        return 0
    project.ensure_dirs()
    name = os.path.basename(project.root) or "project"
    Tracker(project.tracker_path).init_empty(project=name)
    if not os.path.exists(project.config_path):
        _write_starter_config(project.config_path)
    open(project.decisions_path, "a").close()
    print(f"initialized Forge project at {project.forge_dir}")
    return 0


def reset_project(project: ForgeProject) -> list:
    """Clear .forge/ state but KEEP config.yaml, then re-init an empty tracker.

    Returns the names of the entries that were removed.
    """

    removed = []
    for entry in sorted(os.listdir(project.forge_dir)):
        if entry == "config.yaml":
            continue  # preserve the user's model configuration
        path = os.path.join(project.forge_dir, entry)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
        removed.append(entry)
    # Fresh, empty project state (config kept).
    project.ensure_dirs()
    Tracker(project.tracker_path).init_empty(
        project=os.path.basename(project.root) or "project"
    )
    open(project.decisions_path, "a").close()
    return removed


def cmd_reset(args) -> int:
    project = ForgeProject(root=os.path.abspath(args.dir))
    if not project.exists():
        print("nothing to reset — no .forge/ here")
        return 0
    if not args.yes and sys.stdin.isatty():
        print("This clears tracker, specs, logs, episodic memory, and learned "
              "lessons/skills in:")
        print(f"  {project.forge_dir}")
        print("config.yaml is kept. This cannot be undone.")
        if input("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    removed = reset_project(project)
    print(f"reset {project.forge_dir} (kept config.yaml)")
    if removed:
        print(f"  cleared: {', '.join(removed)}")
    return 0


def cmd_setup(args) -> int:
    project = ForgeProject(root=os.path.abspath(args.dir))
    # `--role` may be repeated and/or comma-separated; default is all roles.
    selected: list = []
    for item in args.role or []:
        selected.extend(r.strip() for r in item.split(",") if r.strip())
    if selected:
        bad = [r for r in selected if r not in ROLES]
        if bad:
            print(f"unknown role(s): {', '.join(bad)}. Valid: {', '.join(ROLES)}")
            return 1
        # Preserve canonical ROLES order, de-duplicated.
        roles = tuple(r for r in ROLES if r in selected)
    else:
        roles = ROLES
    run_setup(project, roles=roles)
    return 0


def _maybe_setup(args) -> None:
    """Launch the wizard before a run when there's no config yet.

    Skipped with --mock, --no-setup, or when stdin isn't a TTY (scripted use).
    """

    project = ForgeProject(root=os.path.abspath(args.dir))
    if (
        getattr(args, "mock", False)
        or getattr(args, "no_setup", False)
        or os.path.exists(project.config_path)
        or not sys.stdin.isatty()
    ):
        return
    print("No model configuration found — let's set one up.")
    run_setup(project)


def cmd_run(args) -> int:
    _maybe_setup(args)
    sink, verbosity = _reporting(args)
    extra = {"improve": {"enabled": True}} if getattr(args, "improve", False) else None
    orch = _make_orchestrator(args, extra=extra, reporter=sink, verbosity=verbosity)
    if not orch.project.exists():
        orch.project.ensure_dirs()
        Tracker(orch.project.tracker_path).init_empty(
            project=os.path.basename(orch.project.root) or "project"
        )
    report = orch.run(args.prompt)
    rc = _print_report(report)
    # Keep the session alive for follow-up requests instead of exiting (Section 13).
    if getattr(args, "interactive", False):
        return _interactive_loop(args)
    return rc


def cmd_session(args) -> int:
    """Open a persistent interactive session. Runs an optional initial prompt,
    then stays alive to accept more requests (build / improve / ask / status)."""

    _maybe_setup(args)
    if getattr(args, "prompt", None):
        sink, verbosity = _reporting(args)
        orch = _make_orchestrator(args, reporter=sink, verbosity=verbosity)
        if not orch.project.exists():
            orch.project.ensure_dirs()
            Tracker(orch.project.tracker_path).init_empty(
                project=os.path.basename(orch.project.root) or "project"
            )
        _print_report(orch.run(args.prompt))
    return _interactive_loop(args)


_REPL_HELP = (
    "\nInteractive session — the process stays alive so you can keep going on the\n"
    "same project. Each build request starts a NEW continuation session.\n"
    "  <text>            build/extend the project with this request\n"
    "  /improve          reflect + gate/promote skills + harness proposals\n"
    "  /status           show tracker progress\n"
    "  /ask <question>   answer a question from project context (no code changes)\n"
    "  /resume           continue from the next unfinished task\n"
    "  /help             show this help\n"
    "  /quit  (or Ctrl-D)  exit\n"
)


def _interactive_loop(args) -> int:
    """Read-eval-print loop. Each iteration builds a fresh orchestrator so every
    build request gets its own session id (S2, S3, …) — a true continuation."""

    if not sys.stdin.isatty():
        # Non-interactive stdin (scripted/piped): nothing to read, don't hang.
        print("[session] no TTY; nothing more to do. Re-run `forge run` to continue.")
        return 0
    print(_REPL_HELP)
    sink, verbosity = _reporting(args)
    while True:
        try:
            line = input("forge› ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[session] bye")
            return 0
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        low = cmd.lower()
        if low in ("/quit", "/exit", "quit", "exit"):
            print("[session] bye")
            return 0
        if low in ("/help", "help", "?"):
            print(_REPL_HELP)
            continue
        try:
            if low == "/status":
                cmd_status(args)
            elif low == "/improve":
                cmd_improve(argparse.Namespace(**{**vars(args), "status": False,
                                                  "rollback": None}))
            elif low == "/ask":
                if not rest.strip():
                    print("usage: /ask <question>")
                    continue
                orch = _make_orchestrator(args)
                print(orch.ask(rest.strip()))
            elif low == "/resume":
                orch = _make_orchestrator(args, reporter=sink, verbosity=verbosity)
                _print_report(orch.resume())
            else:
                # Anything else is a build request → a fresh continuation session.
                orch = _make_orchestrator(args, reporter=sink, verbosity=verbosity)
                _print_report(orch.run(line))
        except Exception as exc:  # keep the session alive across per-request errors
            print(f"[session] request failed: {exc}")


def cmd_resume(args) -> int:
    sink, verbosity = _reporting(args)
    orch = _make_orchestrator(args, reporter=sink, verbosity=verbosity)
    report = orch.resume()
    return _print_report(report)


def cmd_status(args) -> int:
    project = ForgeProject(root=os.path.abspath(args.dir))
    tracker = Tracker(project.tracker_path)
    if not tracker.exists():
        print("no tracker found; run `forge init` or `forge run` first")
        return 1
    data = tracker.read()
    done = sum(1 for t in data.tasks if t.done)
    print(f"Project: {data.project}")
    print(f"Goal: {data.goal or '(not set)'}")
    print(f"Progress: {done}/{len(data.tasks)} tasks done\n")
    for t in data.tasks:
        box = "x" if t.done else " "
        tag = "FE" if t.surface == "frontend" else "BE"
        marker = "" if t.done else "  <- NEXT" if t.id == _next_id(data) else ""
        print(f"  [{box}] {t.id}  [{tag}] {t.title}{marker}")
    return 0


def cmd_ask(args) -> int:
    orch = _make_orchestrator(args)
    print(orch.ask(args.question))
    return 0


def cmd_improve(args) -> int:
    # `forge improve` is explicitly about self-improvement, so enable it even if
    # the project config leaves it off by default.
    orch = _make_orchestrator(args, extra={"improve": {"enabled": True}})

    if args.rollback:
        ok = orch.rollback_skill(args.rollback)
        print(f"rollback {args.rollback}: {'ok' if ok else 'failed'}")
        return 0 if ok else 1

    if args.status:
        status = orch.improve_status()
        active = status.get("lessons_active", [])
        demoted = status.get("lessons_demoted", [])
        print(f"Lessons: {len(active)} active, {len(demoted)} demoted")
        for l in active:
            print(f"  [{l.id}] (when {l.trigger}) {l.rule}  "
                  f"[uses={l.uses} wins={l.wins}]")
        skills = status.get("skills", {})
        print(f"\nSkills: {len(skills)} promoted, "
              f"{len(status.get('skills_staged', []))} staged")
        for name, entry in skills.items():
            print(f"  {name} v{entry['current_version']} — {entry.get('when_to_use','')}")
        for name in status.get("skills_staged", []):
            print(f"  (staged) {name}")
        return 0

    # Default: seed the frozen suite, then gate + promote staged skills.
    seeded = orch.seed_eval()
    if seeded:
        print(f"seeded regression suite with: {', '.join(seeded)}")
    promoted = orch.promote_pending()
    if not promoted:
        print("no staged skills to promote")
    for name, ok in promoted:
        print(f"promote {name}: {'PROMOTED' if ok else 'rejected by gate'}")
    proposals = orch.run_harness()
    for p in proposals:
        print(f"harness proposal written: {p.id} (review manually)")
    return 0


def cmd_config(args) -> int:
    project = ForgeProject(root=os.path.abspath(args.dir))
    config = load_config(project.config_path, overrides=_build_overrides(args))
    registry = Registry(config)
    print("Resolved roles:")
    for role, info in registry.describe().items():
        print(
            f"  {role:10s} {info['provider']}/{info['model']} "
            f"(context_window={info['context_window']})"
        )
    return 0


# -- helpers ---------------------------------------------------------------- #


def _next_id(data) -> Optional[str]:
    return next((t.id for t in data.tasks if not t.done), None)


def _print_report(report) -> int:
    if report.completed:
        print(f"completed: {', '.join(report.completed)}")
    if report.status == "needs_user":
        print(f"\n[needs input] {report.question}")
        return 2
    if report.status == "done":
        print(f"\n✓ {report.message}")
        return 0
    print(f"\n✗ {report.status}: {report.message}")
    if report.failed_task:
        print(f"  blocked at: {report.failed_task}")
        print("  progress is saved. Next steps:")
        print("    • forge resume                 — retry from the blocked task")
        print("    • forge status                 — see what's done")
        print(f"    • edit .forge/PROJECT_TRACKER.md to simplify/split {report.failed_task}, "
              "or raise loop.max_inner_iters in .forge/config.yaml")
    return 1


def _write_starter_config(path: str) -> None:
    content = (
        "# Forge config — see Section 14. Defaults apply for anything omitted.\n"
        "roles:\n"
        "  architect:         { provider: anthropic, model: claude-opus-4-8 }\n"
        "  engineer:          { provider: ollama,    model: qwen-coder, num_ctx: 32768 }  # Senior Software Engineer (backend)\n"
        "  frontend_engineer: { provider: ollama,    model: qwen-coder, num_ctx: 32768 }  # Senior UI/UX Engineer (frontend)\n"
        "  router:            { provider: ollama,    model: qwen, num_ctx: 8192 }\n"
        "  clarifier:         { provider: openai,    model: gpt-4o-mini }\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="Forge — CLI coding agent")
    parser.add_argument("--dir", default=".", help="target project directory")
    parser.add_argument("--mock", action="store_true", help="run all roles offline (mock provider)")
    parser.add_argument("--no-setup", dest="no_setup", action="store_true",
                        help="don't auto-launch the setup wizard on `run`")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress live progress output on run/resume")
    parser.add_argument("--improve", action="store_true",
                        help="enable self-improvement (reflection) for this run; "
                             "note: tests/ become read-only while improve is on")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="more detail: -v shows model text + full tool I/O + "
                             "test output; -vv adds plans, specs, and full context")
    for role in ROLES:
        parser.add_argument(f"--{role}-provider", dest=f"{role}_provider")
        parser.add_argument(f"--{role}-model", dest=f"{role}_model")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create .forge/").set_defaults(func=cmd_init)
    p_reset = sub.add_parser("reset", help="clear .forge/ state but keep config.yaml")
    p_reset.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p_reset.set_defaults(func=cmd_reset)
    p_setup = sub.add_parser("setup", help="interactively choose provider+model per role")
    p_setup.add_argument(
        "--role", action="append", metavar="ROLE",
        help=f"reconfigure only this role (repeatable / comma-separated): {', '.join(ROLES)}",
    )
    p_setup.set_defaults(func=cmd_setup)
    p_run = sub.add_parser("run", help="run a prompt end to end")
    p_run.add_argument("prompt")
    p_run.add_argument("-i", "--interactive", action="store_true",
                       help="after the run, stay alive for follow-up requests")
    p_run.set_defaults(func=cmd_run)
    p_sess = sub.add_parser(
        "session", help="open a persistent interactive session (stays alive)")
    p_sess.add_argument("prompt", nargs="?", default=None,
                        help="optional initial build request")
    p_sess.set_defaults(func=cmd_session)
    sub.add_parser("resume", help="continue from NEXT task").set_defaults(func=cmd_resume)
    sub.add_parser("status", help="show tracker progress").set_defaults(func=cmd_status)
    p_ask = sub.add_parser("ask", help="ask a question about the project")
    p_ask.add_argument("question")
    p_ask.set_defaults(func=cmd_ask)
    sub.add_parser("config", help="show resolved provider/model per role").set_defaults(func=cmd_config)
    p_imp = sub.add_parser("improve", help="reflect, gate + promote staged skills")
    p_imp.add_argument("--status", action="store_true", help="show lessons + skills")
    p_imp.add_argument("--rollback", metavar="SKILL", help="roll a skill back one version")
    p_imp.set_defaults(func=cmd_improve)
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Load `.env` (provider keys) before any provider is resolved. Real
    # environment variables always take precedence.
    load_project_env(os.path.abspath(args.dir))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
