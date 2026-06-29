"""The reflect step (Phase 7 §6) — trace -> lesson and/or skill candidate.

Called after a task resolves. It reads the task's trace and its evaluate outcome
(pass/fail + test output) and:
  * emits a LESSON when a run reveals a recurring-looking failure->fix;
  * emits a SKILL CANDIDATE (staged, NOT promoted) when a run produced a clean,
    reusable, *passing* solution.

Promotion through the regression gate is deliberately NOT done here — never in the
hot loop (§6.4). A reflector model does the analysis when available; a
deterministic heuristic keeps reflect useful offline / under the mock provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from forge._jsonutil import extract_json
from forge.improve.lessons import Lesson, LessonStore
from forge.improve.skills import SkillCandidate, SkillLibrary
from forge.providers.base import Message, Provider

REFLECT_PROMPT = (
    "You are Forge's reflection step. You are given one task, its outcome, and the "
    "execution trace. Extract durable improvements.\n\n"
    "Return STRICT JSON only:\n"
    "{\n"
    '  "lesson": {"trigger": "...", "rule": "...", "evidence": "...", '
    '"confidence": 0.8} | null,\n'
    '  "skill":  {"name": "snake_case_id", "when_to_use": "...", '
    '"signature": "run(workspace, args) -> dict", "code": "def run(workspace, '
    'args):\\n    ..."} | null\n'
    "}\n\n"
    "Rules:\n"
    "- Emit a lesson when a failure->fix happened that would recur (terse trigger "
    "and rule).\n"
    "- Emit a skill ONLY if the task PASSED and produced a clean, reusable "
    "procedure; the code must define run(workspace, args) -> {'ok': bool, "
    "'output': str}.\n"
    "- Use null for anything you cannot extract. Do not invent."
)


@dataclass
class Reflection:
    lesson_id: Optional[str] = None
    skill_name: Optional[str] = None


class Reflector:
    def __init__(
        self,
        lesson_store: LessonStore,
        skill_library: SkillLibrary,
        provider: Optional[Provider] = None,
        lessons_enabled: bool = True,
        skills_enabled: bool = True,
    ) -> None:
        self.lessons = lesson_store
        self.skills = skill_library
        self.provider = provider
        self.lessons_enabled = lessons_enabled
        self.skills_enabled = skills_enabled

    def reflect(self, task, result, trace_text: str = "") -> Reflection:
        trace = trace_text or "\n".join(getattr(result, "trace", []) or [])
        outcome = (
            "passed" if result.ok else "escalated" if result.escalate else "failed"
        )
        data = self._analyze(task, outcome, trace)
        reflection = Reflection()

        lesson = (data or {}).get("lesson")
        if self.lessons_enabled and isinstance(lesson, dict) and lesson.get("rule"):
            reflection.lesson_id = self.lessons.add(
                Lesson(
                    id="",
                    trigger=str(lesson.get("trigger", "")).strip(),
                    rule=str(lesson["rule"]).strip(),
                    evidence=str(lesson.get("evidence", "")).strip(),
                    confidence=float(lesson.get("confidence", 0.8) or 0.8),
                    source_trace=getattr(result, "log_path", "") or "",
                )
            )

        skill = (data or {}).get("skill")
        # Skills are only staged from a PASSING run with real code (§4.1).
        if (
            self.skills_enabled
            and result.ok
            and isinstance(skill, dict)
            and skill.get("name")
            and skill.get("code")
        ):
            reflection.skill_name = self.skills.propose(
                SkillCandidate(
                    name=str(skill["name"]).strip(),
                    code=str(skill["code"]),
                    signature=str(skill.get("signature", "")),
                    when_to_use=str(skill.get("when_to_use", "")),
                    certifying_trace=getattr(result, "log_path", "") or "",
                )
            )
        return reflection

    def _analyze(self, task, outcome: str, trace: str) -> Optional[dict]:
        if self.provider is not None:
            messages = [
                Message("system", REFLECT_PROMPT),
                Message(
                    "user",
                    f"Task: {getattr(task, 'id', '?')} — {getattr(task, 'title', '')}\n"
                    f"Outcome: {outcome}\n\nTrace:\n{trace[:8000]}",
                ),
            ]
            data = extract_json(self.provider.complete(messages).text)
            if data is not None:
                return data
        # Heuristic fallback: a non-passing run yields a lesson from the trace.
        return self._heuristic(outcome, trace)

    @staticmethod
    def _heuristic(outcome: str, trace: str) -> Optional[dict]:
        if outcome == "passed":
            return None
        signature = ""
        for line in reversed(trace.splitlines()):
            low = line.lower()
            if "error" in low or "failed" in low or "exception" in low:
                signature = line.strip()[:140]
                break
        if not signature:
            return None
        return {
            "lesson": {
                "trigger": f"trace shows: {signature}",
                "rule": (
                    "revisit this failure mode early; it recurred in a past run "
                    "before being resolved"
                ),
                "evidence": signature,
                "confidence": 0.5,
            },
            "skill": None,
        }
