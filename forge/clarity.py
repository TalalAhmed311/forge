"""Clarity / vague-prompt handling (Section 10).

Terse prompts ("fix the auth thing") are resolved with
resolve-from-context-then-ask, never silent guessing:
  1. If the prompt is already unambiguous, pass it through untouched.
  2. Otherwise do a cheap retrieval pass and let the clarifier model try to
     disambiguate from project context.
  3. Only if it still can't, ask the user exactly ONE targeted question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from forge._jsonutil import extract_json
from forge.agents._prompts import load_prompt
from forge.memory.context_manager import ContextManager
from forge.providers.base import Message, Provider

# Phrases that signal a vague reference rather than a concrete instruction.
_VAGUE_PATTERNS = [
    r"\bthe \w+ thing\b",
    r"\bfix (the|this|that|it)\b",
    r"\bmake it work\b",
    r"\bsomething('s| is) (wrong|broken|off)\b",
    r"\bit('s| is) broken\b",
    r"^\s*(fix|update|change|improve)\s+\w+\s*$",
    # References to information the agent was NOT given. Even a long, fluent prompt
    # is under-specified if it leans on an external doc/agreement we can't see, so
    # route it to the clarifier model (which will ask for the missing detail)
    # rather than letting the word-count gate wave it through as "concrete".
    r"\bwe (discussed|agreed|talked about|decided|chose|spoke about)\b",
    r"\b(the|our|that|your) (spec|design doc|doc|schema|mockup|wireframe|"
    r"requirements?|ticket|figma|diagram)\b",
    r"\byou (sent|gave|shared|provided)\b",
    r"\bas (discussed|agreed|planned|we said)\b",
    r"\b(mentioned|discussed|agreed|talked about) (earlier|before|previously|above)\b",
]


@dataclass
class Intent:
    resolved: str = ""
    needs_user: bool = False
    question: str = ""


def is_unambiguous(prompt: str) -> bool:
    """Heuristic: concrete, specific prompts skip the clarifier entirely.

    A prompt is ambiguous if it's very short, or matches a vague pattern, or
    leans on a bare pronoun without naming a concrete artifact.
    """

    text = prompt.strip()
    lowered = text.lower()
    if len(text.split()) < 4:
        return False
    for pat in _VAGUE_PATTERNS:
        if re.search(pat, lowered):
            return False
    return True


def clarity_check(
    prompt: str,
    clarifier: Provider,
    context_manager: ContextManager,
    small_window: int = 2000,
) -> Intent:
    if is_unambiguous(prompt):
        return Intent(resolved=prompt)

    # Cheap retrieval pass BEFORE bothering the user.
    gathered = context_manager.gather(prompt, small_window)
    resolved = _resolve_with_model(prompt, gathered.render(), clarifier)
    return resolved


def _resolve_with_model(prompt: str, context: str, clarifier: Provider) -> Intent:
    system = Message("system", load_prompt("clarifier"))
    user = Message(
        "user",
        f"User request:\n{prompt}\n\nProject context:\n{context}\n\n"
        "Respond with the strict JSON described above.",
    )
    completion = clarifier.complete([system, user])
    data = extract_json(completion.text) or {}

    if data.get("confident") and data.get("resolved"):
        return Intent(resolved=str(data["resolved"]))
    if data.get("question"):
        return Intent(needs_user=True, question=str(data["question"]))

    # The model gave us nothing usable: fail safe by asking, not guessing.
    return Intent(
        needs_user=True,
        question=f"Could you clarify what you mean by: \"{prompt}\"?",
    )
