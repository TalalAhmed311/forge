"""The Architect agent (Section 7) — plans, never writes code.

Turns a clarified requirement into specs + an ordered task list with per-task
test commands, written into tier-1 state. After planning it steps back; it
re-enters only on escalation (Section 9.3), where it may revise specs/tasks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from forge._jsonutil import extract_json
from forge.agents._prompts import load_prompt
from forge.memory.context_manager import GatheredContext
from forge.memory.tracker import SURFACE_BACKEND, SURFACE_FRONTEND, Task, Tracker
from forge.providers.base import Message, Provider


def _normalize_surface(value) -> str:
    """Map an architect-supplied surface tag to a routing target.

    Anything that looks frontend/UI/client -> frontend; everything else (including
    unset) -> backend, the default route.
    """

    text = str(value or "").strip().lower()
    if text in ("frontend", "front-end", "fe", "ui", "ux", "client"):
        return SURFACE_FRONTEND
    return SURFACE_BACKEND


_JSON_FIX_PROMPT = (
    "Your previous response was not valid, parseable JSON (or it had no tasks). "
    "Return ONLY a single valid JSON object — no prose, no markdown code fences. "
    "Rules: (1) escape every double quote inside a string value as \\\"; "
    "(2) do NOT embed raw JSON examples with double quotes inside the spec text — "
    "describe request/response shapes in prose or use single quotes; "
    "(3) no stray or duplicate closing braces; (4) the `tasks` array is MANDATORY "
    "and must contain at least one task. Re-emit the corrected plan now."
)


@dataclass
class PlanResult:
    ok: bool
    num_tasks: int = 0
    error: str = ""
    raw: str = ""   # the model's raw response, for debugging bad plans


class Architect:
    def __init__(self, provider: Provider, specs_dir: str) -> None:
        self.provider = provider
        self.specs_dir = specs_dir
        self.system_prompt = load_prompt("architect")
        self.last_raw = ""        # raw text of the most recent plan response
        self._session_id = "S1"   # set per plan/escalation; namespaces task ids + specs

    def plan(
        self, requirement: str, gathered: GatheredContext, tracker: Tracker,
        session_id: str = "S1",
    ) -> PlanResult:
        self._session_id = session_id
        data = self._ask(
            f"Requirement:\n{requirement}\n\nProject context:\n{gathered.render()}\n\n"
            f"This is session {session_id}. Produce the strict JSON plan."
        )
        if data is None:
            return PlanResult(ok=False, raw=self.last_raw,
                              error="architect returned no parseable JSON")
        return self._apply_plan(data, tracker)

    def handle_escalation(
        self,
        question: str,
        failed_task: Task,
        gathered: GatheredContext,
        tracker: Tracker,
        session_id: str = "S1",
    ) -> PlanResult:
        self._session_id = session_id
        data = self._ask(
            f"The engineer escalated on task {failed_task.id} "
            f"({failed_task.title}).\nIts question:\n{question}\n\n"
            f"Current project context:\n{gathered.render()}\n\n"
            "Revise the plan to resolve this. Return the strict JSON plan; include "
            "any new or replacement tasks needed."
        )
        tracker.append_decision(f"{failed_task.id}: escalation — {question}")
        if data is None:
            return PlanResult(ok=False, error="architect returned no parseable JSON")
        return self._apply_plan(data, tracker, escalation=True)

    # -- internals --------------------------------------------------------- #

    def _ask(self, user_content: str, max_retries: int = 2) -> Optional[dict]:
        """Ask for the plan JSON, retrying with corrective feedback if the model
        returns unparseable JSON or no tasks.

        Weaker models (e.g. gpt-4o-mini) intermittently emit malformed plan JSON —
        stray closing braces, or unescaped double quotes inside the long markdown
        spec values (an embedded `{"k": "v"}` example breaks the outer string).
        `extract_json` repairs the brace case; for the rest we re-prompt with the
        exact failure, which small models reliably fix. Returns the best parsed
        dict seen (so an explicit empty-tasks plan still surfaces as 'no tasks').
        """

        messages = [
            Message("system", self.system_prompt),
            Message("user", user_content),
        ]
        last_parsed: Optional[dict] = None
        for attempt in range(max_retries + 1):
            completion = self.provider.complete(messages)
            self.last_raw = completion.text or ""
            data = extract_json(self.last_raw)
            if data is not None:
                last_parsed = data
                if data.get("tasks"):
                    return data
            if attempt < max_retries:
                messages.append(Message("assistant", self.last_raw[:4000] or "(empty)"))
                messages.append(Message("user", _JSON_FIX_PROMPT))
        return last_parsed

    def _apply_plan(
        self, data: dict, tracker: Tracker, escalation: bool = False
    ) -> PlanResult:
        # 1. Write spec files into this session's folder (specs/<session>/) so
        #    sessions don't overwrite each other.
        sid = self._session_id
        refs = []
        raw_specs = data.get("specs")
        specs = dict(raw_specs) if isinstance(raw_specs, dict) else {}
        # Tolerate weaker models that emit spec files as TOP-LEVEL keys (e.g.
        # "architecture.md") instead of nesting them under "specs" — otherwise
        # only the one file that did get nested (usually overview.md) is written.
        for key, val in data.items():
            if key in ("goal", "specs", "tasks"):
                continue
            if isinstance(key, str) and key.lower().endswith((".md", ".txt")) \
                    and isinstance(val, str):
                specs.setdefault(key, val)
        if specs:
            session_specs = os.path.join(self.specs_dir, sid)
            os.makedirs(session_specs, exist_ok=True)
            for filename, content in specs.items():
                safe = os.path.basename(str(filename))
                with open(os.path.join(session_specs, safe), "w", encoding="utf-8") as fh:
                    fh.write(str(content))
                refs.append(f"specs/{sid}/{safe}")

        # 2. Parse tasks.
        new_tasks = []
        for raw in data.get("tasks") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            # Namespace ids by session (S2-T1) so they never collide with tasks
            # from prior sessions in the shared tracker.
            raw_id = str(raw["id"])
            task_id = raw_id if raw_id.startswith(f"{sid}-") else f"{sid}-{raw_id}"
            new_tasks.append(
                Task(
                    id=task_id,
                    title=str(raw.get("title", "")).strip(),
                    test_command=str(raw.get("test_command", "")).strip(),
                    surface=_normalize_surface(raw.get("surface")),
                )
            )

        # 3. Update tier-1 state in one atomic write.
        td = tracker.read()
        if data.get("goal"):
            td.goal = str(data["goal"])
        for ref in refs:
            if ref not in td.arch_refs:
                td.arch_refs.append(ref)
        existing = {t.id for t in td.tasks}
        for t in new_tasks:
            if t.id in existing:
                # Replace an unfinished task definition (escalation may revise it).
                for i, cur in enumerate(td.tasks):
                    if cur.id == t.id and not cur.done:
                        td.tasks[i] = t
            else:
                td.tasks.append(t)
        tracker.write(td, agent="architect")

        return PlanResult(ok=True, num_tasks=len(new_tasks), raw=self.last_raw)
