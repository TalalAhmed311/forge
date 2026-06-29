"""Phase 7 — self-improvement layer (lessons, skills, regression gate).

Wraps an improvement loop around the task loop: run -> evaluate -> reflect ->
update -> apply next run. Nothing here touches model weights; all gains accrue to
memory (lessons) and a verified skill library, across runs. The load-bearing
guardrail is eval isolation (tools/fs.py) + the regression gate (regression.py):
the optimizer must never be able to edit the evaluator.
"""

from forge.improve.lessons import Lesson, LessonStore
from forge.improve.regression import GateResult, RegressionGate
from forge.improve.skills import SkillCandidate, SkillLibrary

__all__ = [
    "Lesson",
    "LessonStore",
    "GateResult",
    "RegressionGate",
    "SkillCandidate",
    "SkillLibrary",
]
