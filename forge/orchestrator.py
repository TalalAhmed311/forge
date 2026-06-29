"""Orchestrator — the two nested loops (Section 9).

Inner loop: engineer <-> tests (Section 6). Outer loop: the tracker hands out
tasks one at a time until empty. The tracker is touched at three points
(architect writes, engineer reads via gather, orchestrator marks progress), which
is exactly why a run survives restarts: re-run, read the tracker, resume at NEXT.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from forge.agents.architect import Architect
from forge.agents.engineer import Engineer
from forge.clarity import clarity_check
from forge.config import Config
from forge.grounding import GroundingCache
from forge.memory.context_manager import (
    ContextManager,
    EpisodicContextManager,
    SimpleContextManager,
)
from forge.memory.tracker import SURFACE_BACKEND, SURFACE_FRONTEND, Tracker
from forge.project import ForgeProject
from forge.improve.lessons import LessonStore
from forge.improve.reflect import Reflector
from forge.improve.regression import RegressionGate
from forge.improve.skills import SkillLibrary
from forge.providers.registry import Registry
from forge.tools.base import ToolContext
from forge.tools.factory import engineer_tools


def _short(value, limit: int = 100) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


_CARD_DISTILL_PROMPT = (
    "You are writing a long-term memory card for a completed coding task. In 1-2 "
    "sentences, state WHAT was built and WHY, naming the concrete files, endpoints, "
    "types, or decisions a future task would need. Ground every detail in the "
    "provided notes/trace — do not invent. Be terse; no preamble."
)


@dataclass
class RunReport:
    status: str  # "done" | "needs_user" | "failed" | "no_tasks"
    message: str = ""
    question: str = ""
    completed: list[str] = field(default_factory=list)
    failed_task: str = ""


class Orchestrator:
    def __init__(
        self,
        project: ForgeProject,
        config: Config,
        registry: Registry,
        context_manager: Optional[ContextManager] = None,
        reporter=None,
        verbosity: int = 1,
    ) -> None:
        self.project = project
        self.config = config
        self.registry = registry
        self.tracker = Tracker(project.tracker_path)
        # Level-aware progress sink. `reporter` is a plain Callable[[str], None]
        # (e.g. print); messages are filtered by their level vs `verbosity`:
        #   1 = default summary · 2 = -v (model text, full tool I/O, test output)
        #   3 = -vv (plans, specs, full context). 0/None = quiet.
        sink = reporter or (lambda _m: None)
        self.verbosity = verbosity

        def _report(msg: str, level: int = 1) -> None:
            if level <= verbosity:
                sink(msg)

        self.report = _report

        # Opt-in feature tracing (FORGE_TRACE=1): writes human-readable evidence
        # of the clarifier, router/recall, and improve-reflection steps into
        # .forge/trace/, so each feature is observable after a run.
        self._trace_enabled = bool(os.environ.get("FORGE_TRACE"))

        # Tier-1 text fed verbatim into every gather: the tracker plus any specs.
        self.context_manager = context_manager or self._build_context_manager()

        # Grounding cache mirrors confirmed facts into the tracker (Section 11).
        self.grounding = GroundingCache(on_add=self.tracker.append_fact)

        # Two senior implementers: backend (default route) and frontend. The
        # architect tags each task's surface; the outer loop routes accordingly.
        self.backend_engineer = Engineer(
            provider=registry.for_role("engineer"),
            tools=engineer_tools(),
            max_inner_iters=config.loop["max_inner_iters"],
            command_timeout_s=config.tools["command_timeout_s"],
            prompt_name="engineer",
            reporter=self.report,
        )
        self.frontend_engineer = Engineer(
            provider=registry.for_role("frontend_engineer"),
            tools=engineer_tools(),
            max_inner_iters=config.loop["max_inner_iters"],
            command_timeout_s=config.tools["command_timeout_s"],
            prompt_name="engineer_frontend",
            reporter=self.report,
        )
        # Back-compat alias: `engineer` is the backend/generalist engineer.
        self.engineer = self.backend_engineer
        self.architect = Architect(
            provider=registry.for_role("architect"),
            specs_dir=project.specs_dir,
        )
        # Cap how many times one task may bounce back to the architect.
        self.max_escalations = 2

        # Phase 7 — self-improvement. Absent/disabled => exact Phase 1-6 behavior.
        self._injected_lessons: list = []
        self._setup_improvement()

        # Persistent memory (Redis short-term + pgvector long-term). Off by default.
        self._setup_memory()

    def _setup_memory(self) -> None:
        import os as _os
        from forge.memory.sessions import SessionRegistry

        self.recall = None
        self.long_term_store = None
        self.episodic_log = None
        self.pg_sessions = None  # PgSessionStore mirror, when Postgres is reachable
        self._card_summarizer = None
        self.project_name = _os.path.basename(self.project.root) or "project"
        # Session identity (S1, S2, …) from the registry. A run on a project that
        # already has sessions / a PROJECT.md is a CONTINUATION.
        self.session_registry = SessionRegistry(self.project.sessions_path)
        self.session_id = self.session_registry.next_id()
        self.is_continuation = (
            self.session_registry.has_prior()
            or _os.path.isfile(self.project.project_md_path)
        )

        if not self.config.memory.get("long_term", False):
            return
        from forge.memory.factory import build_memory
        from forge.tools.memory_tools import SearchMemoryTool

        # The `router` role finally earns its keep: it aggregates cross-session hits.
        bundle = build_memory(
            self.config, self.project_name, self.session_id,
            aggregator_provider=self.registry.for_role("router"),
        )
        self.recall = bundle.recall
        self.long_term_store = bundle.long_term
        self.episodic_log = bundle.episodic
        self.pg_sessions = bundle.session_store

        # Route recall internals to the feature trace (FORGE_TRACE=1).
        if self.recall is not None and self._trace_enabled:
            def _router_trace(rec: dict) -> None:
                self._write_trace("router", "ROUTER / CROSS-SESSION RECALL", {
                    "aggregator_model": rec.get("aggregated_by"),
                    "query": rec.get("query"),
                    "retrieved_per_pathway": rec.get("retrieved"),
                    "fused_rrf": rec.get("fused"),
                    "candidates": rec.get("candidates"),
                    "cited": rec.get("cited"),
                    "briefing": rec.get("briefing") or "(no relevant prior context)",
                })
            self.recall.trace_sink = _router_trace
        # Cheap model that distills completed tasks into clean memory cards (out
        # of the hot loop). The clarifier role is the lightest summarizer.
        self._card_summarizer = self.registry.for_role("clarifier")
        # Give both engineers the cross-session search tool + a cheap event sink
        # (continuous writes to short-term memory, tagged with the agent).
        for eng, agent in ((self.backend_engineer, "engineer"),
                           (self.frontend_engineer, "frontend_engineer")):
            eng.tools.add(SearchMemoryTool())
            eng.event_sink = self._make_event_sink(agent)
        self.report("• memory: " + "; ".join(bundle.notes))

    def _make_event_sink(self, agent: str):
        from forge.memory.events import EpisodicEvent

        def sink(event_type: str, content: str, task_id: str, meta=None) -> None:
            if self.episodic_log is None:
                return
            try:
                self.episodic_log.append(EpisodicEvent(
                    session_id=self.session_id, agent=agent, type=event_type,
                    content=content[:1000], task_id=task_id, meta=meta or {},
                ))
            except Exception:
                pass  # memory is best-effort; never break the loop

        return sink

    def _setup_improvement(self) -> None:
        cfg = self.config.get("improve", default={}) or {}
        self.improve_enabled = bool(cfg.get("enabled", False))
        self.lesson_store = None
        self.skill_library = None
        self.reflector = None
        if not self.improve_enabled:
            return

        lessons_cfg = cfg.get("lessons", {}) or {}
        skills_cfg = cfg.get("skills", {}) or {}
        self._inject_top_k = int(lessons_cfg.get("inject_top_k", 5))

        self.lesson_store = LessonStore(
            self.project.lessons_path,
            demote_after_uses=int(lessons_cfg.get("demote_after_uses", 8)),
            demote_win_rate=float(lessons_cfg.get("demote_win_rate", 0.25)),
        )
        self.regression_gate = RegressionGate(
            workspace=self.project.root, eval_dir=self.project.eval_dir
        )
        self.skill_library = SkillLibrary(
            skills_dir=self.project.skills_dir, gate=self.regression_gate
        )
        # Reflector uses the architect's model (no dedicated role in config).
        self.reflector = Reflector(
            lesson_store=self.lesson_store,
            skill_library=self.skill_library,
            provider=self.registry.for_role("architect"),
            lessons_enabled=bool(lessons_cfg.get("enabled", True)),
            skills_enabled=bool(skills_cfg.get("enabled", True)),
        )

        # Inject lessons + skill catalog into every gather (§3.3, §4.4). The
        # lessons hook records what it injected so we can score it afterward.
        def lessons_hook(query: str) -> list:
            if not (self.lesson_store and self.reflector.lessons_enabled):
                return []
            found = self.lesson_store.retrieve(query, k=self._inject_top_k)
            self._injected_lessons = found
            return [lesson.render() for lesson in found]

        self.context_manager.set_improve_hooks(
            lessons_hook=lessons_hook,
            skills_catalog_hook=(
                self.skill_library.catalog
                if skills_cfg.get("enabled", True)
                else None
            ),
        )

        # Promoted skills become callable tools for both engineers (§4.4).
        if skills_cfg.get("enabled", True):
            for tool in self.skill_library.tool_objects():
                self.backend_engineer.tools.add(tool)
                self.frontend_engineer.tools.add(tool)

    def _build_context_manager(self) -> ContextManager:
        mem = self.config.memory
        if mem.get("engine") == "episodic":
            return EpisodicContextManager(
                tier1_provider=self._tier1_text,
                workspace=self.project.root,
                chunk_tokens=mem.get("chunk_tokens", 8000),
                chunk_overlap=mem.get("chunk_overlap", 800),
                max_activated_chunks=mem.get("max_activated_chunks", 8),
                max_live_raw=mem.get("max_live_raw", 3),
            )
        return SimpleContextManager(tier1_provider=self._tier1_text)

    # -- tier-1 assembly --------------------------------------------------- #

    def _tier1_text(self) -> str:
        parts = [self.tracker.read_text() or "(empty tracker)"]

        # Consolidated prior-state summary first (the "read me first" file).
        if os.path.isfile(self.project.project_md_path):
            with open(self.project.project_md_path, "r", encoding="utf-8") as fh:
                parts.append(fh.read())

        # This session's own specs (specs/<session>/).
        session_specs = self.project.session_specs_dir(self.session_id)
        if os.path.isdir(session_specs):
            for name in sorted(os.listdir(session_specs)):
                path = os.path.join(session_specs, name)
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as fh:
                        parts.append(f"## spec ({self.session_id}): {name}\n{fh.read()}")

        # A live directory tree so the architect/engineers orient to what exists.
        try:
            from forge.memory.projectmd import dir_tree
            parts.append("## Current project structure\n```\n"
                         + dir_tree(self.project.root) + "\n```")
        except Exception:
            pass
        return "\n\n".join(parts)

    def _tool_ctx(self, role: str = "engineer") -> ToolContext:
        return ToolContext(
            workspace=self.project.root,
            config=self.config,
            role=role,
            grounding=self.grounding,
            context_manager=self.context_manager,
            recall=self.recall,
            project_name=self.project_name,
            session_id=self.session_id,
        )

    @property
    def _window(self) -> int:
        return self.registry.for_role("engineer").context_window

    def _engineer_for(self, task) -> Engineer:
        """Route a task to the right senior engineer by its surface tag."""

        if getattr(task, "surface", SURFACE_BACKEND) == SURFACE_FRONTEND:
            return self.frontend_engineer
        return self.backend_engineer

    # -- public entry points ----------------------------------------------- #

    def run(self, user_prompt: str) -> RunReport:
        self.session_registry.start(self.session_id, user_prompt)
        if self.pg_sessions is not None:
            self.pg_sessions.start(self.session_id, user_prompt)
        if self.is_continuation:
            self.report(f"• Continuation — session {self.session_id} on an existing "
                        "project (orienting from PROJECT.md + current files)")
        self.report("• Clarifying the request…")
        intent = clarity_check(
            user_prompt,
            clarifier=self.registry.for_role("clarifier"),
            context_manager=self.context_manager,
        )
        self._write_trace("clarifier", "CLARIFIER", {
            "model": self._role_model("clarifier"),
            "user_input": user_prompt,
            "needs_user": intent.needs_user,
            "question": intent.question or "(none — resolved from context)",
            "resolved": intent.resolved or "(asked the user instead)",
        })
        if intent.needs_user:
            self.report("• Need a clarification from you.")
            self._finish_session("needs_user")
            return RunReport(status="needs_user", question=intent.question)

        self.report(f"• Architect is planning ({self.session_id}): {_short(intent.resolved)}")
        plan = self.architect.plan(
            intent.resolved,
            self.context_manager.gather(intent.resolved, self._window),
            self.tracker,
            session_id=self.session_id,
        )
        if not plan.ok:
            self._save_plan_debug(plan.raw)
            self._finish_session("failed")
            return RunReport(status="failed", message=f"planning failed: {plan.error}")
        # A plan with no tasks for THIS session is a failure, not a vacuous success.
        if plan.num_tasks == 0:
            path = self._save_plan_debug(plan.raw)
            self._finish_session("failed")
            return RunReport(
                status="failed",
                message=(
                    "the architect produced no tasks — its plan likely failed to "
                    f"format. Raw response saved to {path}. Try rephrasing the "
                    "request, or use a stronger architect model."
                ),
            )
        self.report(f"• Plan ready: {plan.num_tasks} task(s).")
        # -v: list this session's planned tasks; -vv: dump the spec refs too.
        for t in self.tracker.read().tasks:
            if not t.id.startswith(f"{self.session_id}-"):
                continue
            tag = "FE" if t.surface == SURFACE_FRONTEND else "BE"
            self.report(f"    - {t.id} [{tag}] {t.title}  (test: {t.test_command})", level=2)
        self.report(f"    specs in {self.project.session_specs_dir(self.session_id)}: "
                    f"{', '.join(self.tracker.read().arch_refs) or '(none)'}", level=3)

        report = self._work_outer_loop()
        self._close_session(report)
        return report

    def resume(self) -> RunReport:
        if not self.tracker.exists():
            return RunReport(status="failed", message="no tracker; run `forge run` first")
        report = self._work_outer_loop()
        self._close_session(report)
        return report

    def _role_model(self, role: str) -> str:
        info = self.registry.describe().get(role, {})
        return f"{info.get('provider', '?')}/{info.get('model', '?')}"

    def _write_trace(self, feature: str, title: str, fields: dict) -> None:
        """Append a human-readable evidence block to .forge/trace/<feature>.log."""

        if not self._trace_enabled:
            return
        try:
            trace_dir = os.path.join(self.project.forge_dir, "trace")
            os.makedirs(trace_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines = [f"=== {title} [{self.session_id}] {stamp} ==="]
            for key, val in fields.items():
                if isinstance(val, str) and "\n" in val:
                    lines.append(f"{key}:")
                    lines.extend("    " + ln for ln in val.splitlines())
                elif isinstance(val, (list, dict)):
                    import json as _json
                    lines.append(f"{key}: {_json.dumps(val, ensure_ascii=False)}")
                else:
                    lines.append(f"{key}: {val}")
            block = "\n".join(lines) + "\n\n"
            with open(os.path.join(trace_dir, f"{feature}.log"), "a",
                      encoding="utf-8") as fh:
                fh.write(block)
        except Exception:
            pass  # tracing must never break a run

    def _finish_session(self, status: str) -> None:
        """Mark the session terminal in BOTH registries (file + Postgres) for the
        early-exit paths (needs_user / planning failure) that skip _close_session."""

        self.session_registry.finish(self.session_id, status)
        if self.pg_sessions is not None:
            self.pg_sessions.finish(self.session_id, status)

    def _close_session(self, report) -> None:
        """Record the session outcome and refresh the consolidated PROJECT.md."""

        try:
            done_ids = [t.id for t in self.tracker.read().tasks
                        if t.done and t.id.startswith(f"{self.session_id}-")]
            goal = self.tracker.read().goal
            self.session_registry.finish(
                self.session_id, report.status, goal=goal, tasks=done_ids,
            )
            if self.pg_sessions is not None:
                self.pg_sessions.finish(
                    self.session_id, report.status, goal=goal, tasks=done_ids,
                )
            from forge.memory.projectmd import generate_project_md
            generate_project_md(self.project, self.tracker, self.session_registry)
        except Exception:
            pass  # bookkeeping must never break the run's result

    # -- the outer loop ---------------------------------------------------- #

    def _work_outer_loop(self) -> RunReport:
        completed: list[str] = []
        max_tasks = self.config.loop.get("max_outer_tasks", 100)
        self._escalations: dict[str, int] = {}

        for _ in range(max_tasks):
            task = self.tracker.next_task()
            if task is None:
                return RunReport(
                    status="done", message="all tasks complete", completed=completed
                )

            self._injected_lessons = []  # filled by the lessons hook during gather
            engineer = self._engineer_for(task)
            who = "Senior UI/UX Engineer" if engineer is self.frontend_engineer else "Senior Software Engineer"
            tag = "FE" if task.surface == SURFACE_FRONTEND else "BE"
            self.report(f"\n▶ {task.id} [{tag}] {task.title}  → {who}")
            gathered = self.context_manager.gather(task.title, self._window)
            self._inject_memory(gathered, task)  # once, at task start
            result = engineer.run_task(task, gathered, self._tool_ctx("engineer"))
            result.log_path = self._log_trace(task.id, result.trace)
            # Episodic memory records the engineer's work (Section 8.1).
            self.context_manager.append("\n".join(result.trace))
            self._score_lessons(result.ok)
            self._reflect(task, result)

            if result.ok:
                self.report(f"✓ {task.id} done ({result.iterations} iteration(s))")
                self.tracker.mark_done(task.id, result.summary)
                completed.append(task.id)
                # The engineer may have added/changed symbols; refresh the index.
                if hasattr(self.context_manager, "reindex_code"):
                    self.context_manager.reindex_code()
                self._persist_task(task, result)  # long-term + episodic memory
                continue

            if result.escalate:
                self.report(f"↑ {task.id} escalated to the architect")
                # Bound re-escalation per task so a permanently-stuck task can't
                # ping-pong with the architect forever (Section 9.3 + 6.2).
                self._escalations[task.id] = self._escalations.get(task.id, 0) + 1
                if self._escalations[task.id] > self.max_escalations:
                    return RunReport(
                        status="failed",
                        message=(
                            f"task {task.id} escalated {self._escalations[task.id]} "
                            f"times without progress: {result.question}"
                        ),
                        failed_task=task.id,
                        completed=completed,
                    )
                esc = self.architect.handle_escalation(
                    result.question, task, gathered, self.tracker,
                    session_id=self.session_id,
                )
                if not esc.ok:
                    return RunReport(
                        status="failed",
                        message=f"escalation unresolved: {esc.error}",
                        failed_task=task.id,
                        completed=completed,
                    )
                continue

            return RunReport(
                status="failed",
                message=result.reason or "task failed",
                failed_task=task.id,
                completed=completed,
            )

        return RunReport(
            status="failed",
            message=f"hit max_outer_tasks ({max_tasks})",
            completed=completed,
        )

    def ask(self, question: str) -> str:
        """Ad-hoc query answered from context, no code changes (Section 13)."""

        from forge.providers.base import Message

        gathered = self.context_manager.gather(question, self._window)
        provider = self.registry.for_role("architect")
        messages = [
            Message(
                "system",
                "You answer questions about this project strictly from the provided "
                "context. If the answer is not in context, say so. Do not invent "
                "files, symbols, or APIs.",
            ),
            Message("user", f"Context:\n{gathered.render()}\n\nQuestion: {question}"),
        ]
        return provider.complete(messages).text

    # -- Phase 7: `forge improve` operations ------------------------------- #

    def seed_eval(self) -> list:
        """Seed the frozen regression suite from the project's current tests."""

        if not self.improve_enabled:
            return []
        return self.regression_gate.seed_from(["tests", "test"])

    def promote_pending(self) -> list:
        """Gate + promote every staged skill candidate (never inline; §6.4)."""

        if not (self.improve_enabled and self.skill_library):
            return []
        return [
            (name, self.skill_library.promote(name))
            for name in self.skill_library.staged()
        ]

    def rollback_skill(self, name: str, version: Optional[int] = None) -> bool:
        if not (self.improve_enabled and self.skill_library):
            return False
        return self.skill_library.rollback(name, version)

    def improve_status(self) -> dict:
        if not self.improve_enabled:
            return {"enabled": False}
        lessons = self.lesson_store.all() if self.lesson_store else []
        return {
            "enabled": True,
            "lessons_active": [l for l in lessons if l.active],
            "lessons_demoted": [l for l in lessons if not l.active],
            "skills": self.skill_library._index if self.skill_library else {},
            "skills_staged": self.skill_library.staged() if self.skill_library else [],
        }

    def run_harness(self) -> list:
        """Produce reviewable (never auto-applied) harness-edit proposals (7d)."""

        if not (self.improve_enabled and self.config.get(
            "improve", "harness_self_edit", "enabled", default=False
        )):
            return []
        from forge.improve.harness import HarnessAnalyzer

        analyzer = HarnessAnalyzer(
            logs_dir=self.project.logs_dir,
            proposals_dir=os.path.join(self.project.forge_dir, "proposals"),
            gate=self.regression_gate,
        )
        proposals = analyzer.propose()
        analyzer.write_proposals(proposals)
        return proposals

    # -- persistent memory hooks ------------------------------------------- #

    DURABLE_KINDS = ("summary", "decision", "tool_result", "escalation", "test_result")

    def _inject_memory(self, gathered, task) -> None:
        """Inject memory ONCE at task start (never per step):

          * cross-session briefing (long-term, past sessions, via the router);
          * this-session slice of durable events (short-term, what's been done).
        """

        if self.recall is not None:
            try:
                briefing = self.recall.recall(
                    task.title, project=self.project_name,
                    exclude_session=self.session_id,
                )
                gathered.cross_session = briefing.render()
            except Exception:
                pass
        if self.episodic_log is not None:
            try:
                durable = [e for e in self.episodic_log.all()
                           if e.type in self.DURABLE_KINDS]
                gathered.session_slice = [
                    f"- [{e.agent}/{e.task_id or '-'}] {e.type}: {e.content[:160]}"
                    for e in durable[-8:]
                ]
            except Exception:
                pass

    def _persist_task(self, task, result) -> None:
        """Promote a completed task to long-term as a DISTILLED card (not the raw
        trace) + a short-term summary event. Best-effort; never breaks the run."""

        surface = "frontend" if task.surface == SURFACE_FRONTEND else "backend"
        agent = "frontend_engineer" if surface == "frontend" else "engineer"
        try:
            facts = self._card_facts(task, result, surface)
            summary = self._card_summary(task, result, facts)
            if self.episodic_log is not None:
                from forge.memory.events import EpisodicEvent

                self.episodic_log.append(EpisodicEvent(
                    session_id=self.session_id, agent=agent, type="summary",
                    content=summary, task_id=task.id, meta={"surface": surface},
                ))
            if self.long_term_store is not None:
                from forge.memory.longterm import Document

                self.long_term_store.add_document(Document(
                    session_id=self.session_id, doc_id=task.id,
                    content=summary + "\n\n" + facts,
                    summary=summary[:300],
                    kind="tool_result", agent=agent, task_id=task.id,
                    project=self.project_name,
                ))
        except Exception:
            pass

    @staticmethod
    def _card_facts(task, result, surface: str) -> str:
        """Deterministic backbone of the card — files touched + outcome + test."""

        import re

        files = sorted({
            m.group(1)
            for line in result.trace
            for m in [re.search(r"write_file\(\{'path': '([^']+)'", line)]
            if m
        })
        lines = [
            f"Task {task.id} [{surface}]: {task.title}",
            f"Verified by: {task.test_command}",
        ]
        if files:
            lines.append("Files: " + ", ".join(files))
        if result.log_path:
            lines.append(f"(raw trace: {result.log_path})")
        return "\n".join(lines)

    def _card_summary(self, task, result, facts: str) -> str:
        """A 1-2 sentence summary of what was built and why (cheap model, out of
        the hot loop). Falls back to the engineer's own summary if unavailable."""

        fallback = (result.summary or task.title).strip()
        if self._card_summarizer is None:
            return fallback
        try:
            from forge.providers.base import Message

            messages = [
                Message("system", _CARD_DISTILL_PROMPT),
                Message(
                    "user",
                    f"Notes:\n{facts}\n\nEngineer's summary: {result.summary}\n\n"
                    f"Trace excerpt:\n{chr(10).join(result.trace)[:3000]}",
                ),
            ]
            text = self._card_summarizer.complete(messages).text.strip()
            return text[:400] if text else fallback
        except Exception:
            return fallback

    # -- Phase 7: improvement hooks ---------------------------------------- #

    def _score_lessons(self, helped: bool) -> None:
        """Bump uses/wins for lessons injected into this task's context (§3.1)."""

        if not (self.improve_enabled and self.lesson_store):
            return
        for lesson in self._injected_lessons:
            self.lesson_store.record_outcome(lesson.id, helped=helped)
        self._injected_lessons = []

    def _reflect(self, task, result) -> None:
        """Extract a lesson and/or stage a skill candidate after a task (§6).

        Reflection must never break the run; swallow its errors.
        """

        if not (self.improve_enabled and self.reflector):
            return
        try:
            reflection = self.reflector.reflect(task, result, "\n".join(result.trace))
            self._write_trace("improve", "IMPROVE / REFLECTION", {
                "task": f"{task.id} — {task.title}",
                "outcome": "passed" if result.ok else "failed/escalated",
                "lesson_added": reflection.lesson_id or "(none)",
                "skill_staged": reflection.skill_name or "(none)",
                "note": "staged skills are gated + promoted by `forge improve`",
            })
        except Exception:
            pass

    # -- logging ----------------------------------------------------------- #

    def _save_plan_debug(self, raw: str) -> str:
        """Write the architect's raw plan response so bad plans are debuggable."""

        self.project.ensure_dirs()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(self.project.logs_dir, f"{stamp}-architect-plan.log")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw or "(empty response)")
        except OSError:
            return "(could not write debug log)"
        return os.path.relpath(path, self.project.root)

    def _log_trace(self, task_id: str, trace: list[str]) -> str:
        self.project.ensure_dirs()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(self.project.logs_dir, f"{stamp}-{task_id}.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(trace) + "\n")
        return os.path.relpath(path, self.project.root)
