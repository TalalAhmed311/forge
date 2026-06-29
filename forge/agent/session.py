"""The interactive `forge agent` session: REPL + the integration layer.

The agent is the conversational FRONT DOOR over Forge's existing subsystems —
not a separate brain. It shares the same `.forge/` state and drives:
  * memory      — build_memory bundle; router cross-session briefing injected per
                  turn; episodic events written; a card promoted on close.
  * sessions    — registered in SessionRegistry (+ the Postgres sessions table).
  * clarifier   — the first request is resolved-or-asked via clarity_check.
  * architect   — the `plan` tool / `/plan` writes specs + tracker tasks.
  * engineer    — the `delegate_task` tool / `/delegate` runs the test-gated loop.
  * improve     — reflection after delegated tasks (when enabled).
On top of that it adds the interactive layer: surgical edits, permissions, undo
checkpoints, navigation, todos, @-mentions, and persistence/resume.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Optional

from forge.agent.checkpoint import CheckpointManager
from forge.agent.loop import AgentLoop
from forge.agent.permissions import MODES, PermissionManager
from forge.agents._prompts import load_prompt
from forge.memory.context_manager import GatheredContext
from forge.memory.tracker import SURFACE_BACKEND, Task, Tracker
from forge.project import ForgeProject
from forge.providers.base import Message
from forge.tools.base import ToolContext
from forge.tools.capabilities import DelegateTaskTool, PlanTool
from forge.tools.factory import agent_tools
from forge.tools.subagent import SpawnSubagentTool
from forge.tools.todo import TodoStore, TodoWriteTool

PROJECT_DOC_NAMES = ("FORGE.md", "CLAUDE.md")

HELP = """\
commands:
  /help              show this help
  /mode <m>          permission mode: default | acceptEdits | plan | bypass
  /plan <text>       architect writes specs + tracker tasks for a build
  /delegate <text>   hand the next/most-relevant task to the test-gated engineer
  /status            show the project tracker (tasks + progress)
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
@path mentions inline a file. Ctrl-C interrupts a turn; Ctrl-D/quit exits.
"""


def expand_mentions(text: str, workspace: str) -> str:
    out = text
    for token in re.findall(r"(?:^|\s)@([^\s]+)", text):
        path = os.path.join(workspace, token)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    out += f"\n\n@{token}:\n```\n{fh.read()[:20000]}\n```"
            except OSError:
                pass
    return out


def load_project_doc(workspace: str) -> str:
    for name in PROJECT_DOC_NAMES:
        path = os.path.join(workspace, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return f"\n\n## Project instructions ({name})\n{fh.read()}"
            except OSError:
                return ""
    return ""


class AgentSession:
    def __init__(self, workspace: str, config, registry, mode: str = "default",
                 sink=None, resume: bool = False, improve: bool = False) -> None:
        self.workspace = os.path.abspath(workspace)
        self.config = config
        self.registry = registry
        self.print = sink or (lambda m="": print(m, flush=True))
        self.provider = registry.for_role("engineer")

        self.project = ForgeProject(root=self.workspace)
        self.project.ensure_dirs()
        self.tracker = Tracker(self.project.tracker_path)
        if not self.tracker.exists():
            self.tracker.init_empty(project=os.path.basename(self.workspace) or "project")
        self.state_path = os.path.join(self.workspace, ".forge", "agent", "session.json")
        self.project_name = os.path.basename(self.workspace) or "project"
        self._first_turn = True

        # -- sessions + memory (shared with the batch path) -------------------
        from forge.memory.sessions import SessionRegistry
        self.session_registry = SessionRegistry(self.project.sessions_path)
        self.session_id = self.session_registry.next_id()
        self.recall = None
        self.long_term = None
        self.episodic = None
        self.pg_sessions = None
        self._card_summarizer = None
        if config.memory.get("long_term", False):
            from forge.memory.factory import build_memory
            bundle = build_memory(config, self.project_name, self.session_id,
                                  aggregator_provider=self._safe_role("router"))
            self.recall = bundle.recall
            self.long_term = bundle.long_term
            self.episodic = bundle.episodic
            self.pg_sessions = bundle.session_store
            self._card_summarizer = self._safe_role("clarifier")
            self.print("• memory: " + "; ".join(bundle.notes))
        self.session_registry.start(self.session_id, "interactive agent session")
        if self.pg_sessions is not None:
            self.pg_sessions.start(self.session_id, "interactive agent session")

        # -- improve (optional) ----------------------------------------------
        self.improve_enabled = bool(improve) or bool(
            config.get("improve", "enabled", default=False))
        self.reflector = None
        if self.improve_enabled:
            self._setup_improve()

        # -- interactive layer ------------------------------------------------
        self.checkpoint = CheckpointManager()
        self.permissions = PermissionManager(
            mode=mode, approver=self._approve,
            command_allowlist=config.get("tools", "command_allowlist", default=[]))
        self.todos = TodoStore()
        self.tool_ctx = ToolContext(
            workspace=self.workspace, config=config, role="agent",
            checkpoint=self.checkpoint, recall=self.recall,
            project_name=self.project_name, session_id=self.session_id)

        tools = agent_tools()
        tools.add(TodoWriteTool(self.todos))
        tools.add(SpawnSubagentTool(self._run_subagent))
        tools.add(PlanTool(self._run_plan))
        tools.add(DelegateTaskTool(self._run_delegate))
        if self.recall is not None:
            from forge.tools.memory_tools import SearchMemoryTool
            tools.add(SearchMemoryTool())

        system = load_prompt("agent") + load_project_doc(self.workspace)
        self.loop = AgentLoop(
            provider=self.provider, tools=tools, tool_ctx=self.tool_ctx,
            permissions=self.permissions, system_prompt=system,
            on_event=self._on_event, compactor=self._safe_role("clarifier"))
        self._closed = False
        if resume:
            self._load()

    # -- subsystem wiring -------------------------------------------------- #

    def _safe_role(self, role):
        try:
            return self.registry.for_role(role)
        except Exception:
            return None

    def _setup_improve(self) -> None:
        from forge.improve.lessons import LessonStore
        from forge.improve.regression import RegressionGate
        from forge.improve.reflect import Reflector
        from forge.improve.skills import SkillLibrary
        self.lesson_store = LessonStore(self.project.lessons_path)
        gate = RegressionGate(workspace=self.workspace, eval_dir=self.project.eval_dir)
        self.skill_library = SkillLibrary(skills_dir=self.project.skills_dir, gate=gate)
        self.reflector = Reflector(
            lesson_store=self.lesson_store, skill_library=self.skill_library,
            provider=self._safe_role("architect"))

    def _tier1_text(self) -> str:
        from forge.memory.projectmd import dir_tree
        parts = [self.tracker.read_text() or "(empty tracker)"]
        if os.path.isfile(self.project.project_md_path):
            with open(self.project.project_md_path, "r", encoding="utf-8") as fh:
                parts.append(fh.read())
        try:
            parts.append("## Current project structure\n```\n"
                         + dir_tree(self.workspace) + "\n```")
        except Exception:
            pass
        return "\n\n".join(parts)

    def _episodic(self, etype: str, content: str, task_id: str = "", agent: str = "agent") -> None:
        if self.episodic is None:
            return
        try:
            from forge.memory.events import EpisodicEvent
            self.episodic.append(EpisodicEvent(
                session_id=self.session_id, agent=agent, type=etype,
                content=content[:1000], task_id=task_id, meta={}))
        except Exception:
            pass

    # -- capability handlers (architect + engineer) ------------------------ #

    def _run_plan(self, requirement: str) -> str:
        from forge.agents.architect import Architect
        architect = Architect(provider=self._safe_role("architect") or self.provider,
                              specs_dir=self.project.specs_dir)
        gathered = GatheredContext(tier1=self._tier1_text())
        plan = architect.plan(requirement, gathered, self.tracker, session_id=self.session_id)
        if not plan.ok or plan.num_tasks == 0:
            return f"planning produced no tasks ({plan.error or 'check the request'})"
        data = self.tracker.read()
        new = [t for t in data.tasks if t.id.startswith(f"{self.session_id}-")]
        lines = [f"planned {plan.num_tasks} task(s); specs in "
                 f".forge/specs/{self.session_id}/ ({', '.join(data.arch_refs[-3:])})"]
        lines += [f"  {t.id} [{'FE' if t.surface != SURFACE_BACKEND else 'BE'}] {t.title}"
                  f"  (test: {t.test_command})" for t in new]
        self._episodic("decision", f"planned: {requirement}", agent="architect")
        return "\n".join(lines)

    def _run_delegate(self, title: str, test_command: str, task_id: Optional[str]) -> str:
        from forge.agents.engineer import Engineer
        from forge.tools.factory import engineer_tools

        # Reuse an existing tracker task if the id matches, else an ad-hoc one.
        task = None
        if task_id:
            task = next((t for t in self.tracker.read().tasks if t.id == task_id), None)
        if task is None:
            task = Task(id=task_id or f"{self.session_id}-D{self._next_delegate_n()}",
                        title=title, test_command=test_command, surface=SURFACE_BACKEND)
        gathered = GatheredContext(tier1=self._tier1_text())
        if self.improve_enabled and self.reflector and self.reflector.lessons:
            gathered.lessons = [l.render() for l in self.lesson_store.retrieve(title, k=5)]
        engineer = Engineer(
            provider=self.provider, tools=engineer_tools(),
            max_inner_iters=self.config.loop["max_inner_iters"],
            command_timeout_s=self.config.get("tools", "command_timeout_s", default=120),
            prompt_name="engineer",
            reporter=lambda m, level=1: self.print("    eng│ " + m) if level == 1 else None,
            event_sink=lambda t, c, tid, meta=None: self._episodic(t, c, tid, "engineer"),
            max_seconds=self.config.loop.get("max_seconds", 0),
            no_progress_repeats=self.config.loop.get("no_progress_repeats", 3))
        result = engineer.run_task(task, gathered, self.tool_ctx)
        if result.ok:
            self.tracker.mark_done(task.id, result.summary)
            self._persist_card(task, result)
        if self.improve_enabled and self.reflector:
            try:
                self.reflector.reflect(task, result, "\n".join(result.trace))
            except Exception:
                pass
        status = "PASSED" if result.ok else ("ESCALATED" if result.escalate else "FAILED")
        return (f"task {task.id} {status} after {result.iterations} iteration(s). "
                f"{result.summary or result.reason or result.question}")

    def _next_delegate_n(self) -> int:
        existing = [t.id for t in self.tracker.read().tasks]
        return sum(1 for tid in existing if "-D" in tid) + 1

    def _persist_card(self, task, result) -> None:
        if self.long_term is None:
            return
        try:
            summary = (result.summary or task.title).strip()
            if self._card_summarizer is not None:
                from forge.providers.base import Message as M
                summary = (self._card_summarizer.complete([
                    M("system", "In 1-2 sentences state what was built and why, naming "
                      "concrete files/functions. Ground in the notes; no preamble."),
                    M("user", f"Task: {task.title}\nVerified by: {task.test_command}\n"
                      f"Trace:\n{chr(10).join(result.trace)[:3000]}")]).text.strip()
                           or summary)
            from forge.memory.longterm import Document
            self.long_term.add_document(Document(
                session_id=self.session_id, doc_id=task.id, content=summary,
                summary=summary[:300], kind="tool_result", agent="engineer",
                task_id=task.id, project=self.project_name))
            self._episodic("summary", summary, task.id, "engineer")
        except Exception:
            pass

    # -- per-turn memory injection ----------------------------------------- #

    def _briefing(self, message: str) -> str:
        if self.recall is None:
            return message
        try:
            briefing = self.recall.recall(message, project=self.project_name,
                                          exclude_session=self.session_id)
            text = briefing.render()
            if text.strip():
                return f"{text}\n\n---\n{message}"
        except Exception:
            pass
        return message

    # -- providers / subagent ---------------------------------------------- #

    def _run_subagent(self, task: str) -> str:
        sub_tools = agent_tools()
        sub_tools.add(TodoWriteTool(TodoStore()))
        sub = AgentLoop(
            provider=self.provider, tools=sub_tools, tool_ctx=self.tool_ctx,
            permissions=self.permissions, system_prompt=load_prompt("agent"),
            on_event=lambda k, d: self._on_event(k, d, prefix="    sub│ "))
        return sub.run_turn(task)

    # -- rendering + episodic --------------------------------------------- #

    def _on_event(self, kind: str, data: dict, prefix: str = "") -> None:
        if kind == "assistant_text":
            self.print(prefix + data["text"])
        elif kind == "tool_call":
            self.print(f"{prefix}⚙ {data['name']}({_short(data['args'])})")
        elif kind == "tool_result":
            mark = "✓" if data["ok"] else "✗"
            self.print(f"{prefix}  {mark} {_short(data['content'], 200)}")
            name = data.get("name", "")
            if data["ok"] and name in ("write_file", "edit_file", "run_command"):
                self._episodic("tool_result", f"{name}: {data['content']}", agent="agent")
        elif kind == "permission_denied":
            self.print(f"{prefix}  ✗ denied: {data['reason']}")
        elif kind == "compaction":
            self.print(f"{prefix}… compacted earlier context")
        elif kind == "error":
            self.print(f"{prefix}! {data['text']}")

    def _approve(self, name: str, args: dict, preview: str) -> str:
        if not sys.stdin.isatty():
            return "no"
        self.print(f"\n⚠ {name} wants to run:\n{preview}")
        try:
            ans = input("  allow? [y]es / [N]o / [a]lways: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "no"
        if ans in ("a", "always"):
            return "always"
        return "yes" if ans in ("y", "yes") else "no"

    # -- REPL -------------------------------------------------------------- #

    def repl(self, initial: Optional[str] = None) -> int:
        self.print(f"forge agent — {self.workspace}  (session {self.session_id}, "
                   f"mode: {self.permissions.mode})")
        self.print("type /help for commands, /quit to exit.\n")
        pending = initial
        try:
            while True:
                if pending is not None:
                    line, pending = pending, None
                else:
                    try:
                        line = input("\nyou › ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                if not line:
                    continue
                if line.startswith("/"):
                    if self._slash(line):
                        break
                    continue
                message = expand_mentions(line, self.workspace)
                # Slice 2: clarify the FIRST substantive request (resolve or ask).
                if self._first_turn:
                    self._first_turn = False
                    intent = self._clarify(message)
                    if intent is None:  # needs_user: question printed, await next msg
                        continue
                    message = intent
                message = self._briefing(message)  # Slice 1: router cross-session memory
                try:
                    self.loop.run_turn(message)
                except KeyboardInterrupt:
                    self.loop.interrupted = True
                    self.print("\n[interrupted]")
                self._save()
        finally:
            self._close()
        self.print("bye")
        return 0

    def _clarify(self, message: str) -> Optional[str]:
        clarifier = self._safe_role("clarifier")
        if clarifier is None:
            return message
        try:
            from forge.clarity import clarity_check
            from forge.memory.context_manager import SimpleContextManager
            cm = SimpleContextManager(tier1_provider=self._tier1_text)
            intent = clarity_check(message, clarifier=clarifier, context_manager=cm)
        except Exception:
            return message
        if intent.needs_user:
            self.print(f"\n❓ {intent.question}")
            return None
        return intent.resolved or message

    # -- slash commands ---------------------------------------------------- #

    def _slash(self, line: str) -> bool:
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd in ("/quit", "/exit"):
            return True
        if cmd == "/help":
            self.print(HELP)
        elif cmd == "/mode":
            self.print(f"mode → {rest}" if self.permissions.set_mode(rest)
                       else f"unknown mode; choose: {', '.join(MODES)}")
        elif cmd == "/plan":
            self.print(self._run_plan(rest) if rest else "usage: /plan <what to build>")
        elif cmd == "/delegate":
            if not rest:
                self.print("usage: /delegate <task title> (uses the next tracker task's test)")
            else:
                nxt = self.tracker.next_task()
                tc = nxt.test_command if nxt else "python -m pytest -q"
                self.print(self._run_delegate(rest, tc, nxt.id if nxt else None))
        elif cmd == "/status":
            self.print(self._status())
        elif cmd == "/sync":
            marked = self._reconcile_tracker()
            self.print(f"synced: marked {len(marked)} task(s) done"
                       + (f" ({', '.join(marked)})" if marked else "")
                       + "\n" + self._status())
        elif cmd == "/memory":
            self.print(self._memory_search(rest))
        elif cmd == "/todos":
            self.print(self.todos.render())
        elif cmd == "/tools":
            self.print(", ".join(self.loop.tools.names()))
        elif cmd == "/diff":
            self.print(self._diff())
        elif cmd == "/undo":
            reverted = self.checkpoint.undo()
            self.print(f"reverted {len(reverted)} file(s)" if reverted else "nothing to undo")
        elif cmd == "/clear":
            self.loop.history = [self.loop.history[0]]
            self.todos.set([])
            self._save()
            self.print("conversation cleared")
        elif cmd == "/init":
            self._init_doc()
        elif cmd == "/commit":
            self.print(self._commit(rest or "checkpoint via forge agent"))
        else:
            self.print(f"unknown command {cmd}; /help for the list")
        return False

    def _reconcile_tracker(self) -> list:
        """Sync the tracker to reality: run each pending task's test command and
        mark the ones that now pass as done. Bridges interactive free-building
        (edit_file/run_command) — which doesn't touch the tracker — back to it."""
        from forge.tools.shell import run_tests
        timeout = self.config.get("tools", "command_timeout_s", default=120)
        marked = []
        for t in self.tracker.read().tasks:
            if t.done or not t.test_command:
                continue
            res = run_tests(t.test_command, self.workspace, timeout)
            if res.ok:
                self.tracker.mark_done(t.id, "verified by tracker sync")
                marked.append(t.id)
        return marked

    def _status(self) -> str:
        data = self.tracker.read()
        done = sum(1 for t in data.tasks if t.done)
        lines = [f"tracker: {done}/{len(data.tasks)} task(s) done"]
        nxt = self.tracker.next_task()
        for t in data.tasks:
            box = "x" if t.done else " "
            mark = "  ← NEXT" if nxt and t.id == nxt.id else ""
            lines.append(f"  [{box}] {t.id}  {t.title}{mark}")
        return "\n".join(lines)

    def _memory_search(self, query: str) -> str:
        if self.recall is None:
            return "long-term memory is not enabled"
        if not query:
            return "usage: /memory <query>"
        try:
            return self.recall.recall(query, project=self.project_name,
                                      exclude_session=self.session_id).render() or "(nothing)"
        except Exception as exc:  # noqa: BLE001
            return f"recall failed: {exc}"

    # -- persistence + close ----------------------------------------------- #

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            data = {
                "session_id": self.session_id,
                "history": [{"role": m.role, "content": m.content,
                             "tool_calls": m.tool_calls, "tool_call_id": m.tool_call_id}
                            for m in self.loop.history],
                "todos": self.todos.items,
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _load(self) -> None:
        if not os.path.isfile(self.state_path):
            self.print("(no saved session to resume; starting fresh)")
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        msgs = [Message(role=m.get("role", "user"), content=m.get("content", ""),
                        tool_calls=m.get("tool_calls"), tool_call_id=m.get("tool_call_id"))
                for m in data.get("history", []) if isinstance(m, dict)]
        if msgs:
            self.loop.history = [self.loop.history[0]] + [m for m in msgs if m.role != "system"]
            self._first_turn = False  # already in conversation
        self.todos.set(data.get("todos", []) or [])
        turns = sum(1 for m in self.loop.history if m.role == "user")
        self.print(f"(resumed session: {turns} prior user turn(s))")

    def _close(self) -> None:
        """Record the session + promote a memory card + refresh PROJECT.md."""
        if self._closed:
            return
        self._closed = True
        try:
            self._reconcile_tracker()  # check off planned tasks whose tests now pass
            done_ids = [t.id for t in self.tracker.read().tasks if t.done]
            goal = self.tracker.read().goal
            self.session_registry.finish(self.session_id, "done", goal=goal, tasks=done_ids)
            if self.pg_sessions is not None:
                self.pg_sessions.finish(self.session_id, "done", goal=goal, tasks=done_ids)
            self._promote_session_card()
            from forge.memory.projectmd import generate_project_md
            generate_project_md(self.project, self.tracker, self.session_registry)
        except Exception:
            pass

    def _promote_session_card(self) -> None:
        if self.long_term is None or self._card_summarizer is None:
            return
        convo = "\n".join(f"{m.role}: {m.content[:600]}" for m in self.loop.history
                          if m.role in ("user", "assistant") and m.content)
        if not convo.strip():
            return
        try:
            from forge.providers.base import Message as M
            summary = self._card_summarizer.complete([
                M("system", "Summarize this coding session into a 2-3 sentence memory "
                  "card: what was built/changed, naming concrete files. No preamble."),
                M("user", convo[:8000])]).text.strip()
            if not summary:
                return
            from forge.memory.longterm import Document
            self.long_term.add_document(Document(
                session_id=self.session_id, doc_id=f"{self.session_id}-session",
                content=summary, summary=summary[:300], kind="summary",
                agent="agent", task_id="", project=self.project_name))
        except Exception:
            pass

    # -- git / init helpers ------------------------------------------------ #

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.workspace,
                              capture_output=True, text=True)

    def _diff(self) -> str:
        if os.path.isdir(os.path.join(self.workspace, ".git")):
            return self._git("diff", "--stat").stdout or "(no uncommitted changes)"
        if self.checkpoint.has_changes():
            return "changed this turn:\n" + "\n".join(
                "  " + os.path.relpath(p, self.workspace)
                for p in self.checkpoint.changed_paths())
        return "(no tracked changes; not a git repo)"

    def _commit(self, msg: str) -> str:
        if not os.path.isdir(os.path.join(self.workspace, ".git")):
            if self._git("init").returncode != 0:
                return "git init failed"
        self._git("add", "-A")
        res = self._git("commit", "-m", msg)
        return (res.stdout or res.stderr or "committed").strip()

    def _init_doc(self) -> None:
        from forge.memory.projectmd import dir_tree
        try:
            tree = dir_tree(self.workspace)
        except Exception:
            tree = "(unavailable)"
        self.print("generating FORGE.md…")
        try:
            text = self.provider.complete([
                Message("system", "You write tight, accurate project READMEs."),
                Message("user", "Write a concise FORGE.md (overview, components, how to "
                        "run/test, conventions) based ONLY on this structure:\n" + tree),
            ]).text.strip()
        except Exception as exc:  # noqa: BLE001
            self.print(f"failed: {exc}")
            return
        with open(os.path.join(self.workspace, "FORGE.md"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        self.print("wrote FORGE.md")


def _short(value, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"
