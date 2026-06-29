"""Phase 3 exit test: resolve unambiguous prompts silently, ask exactly one
question on a genuinely ambiguous one."""

from __future__ import annotations

import json

from forge.clarity import clarity_check, is_unambiguous
from forge.memory.context_manager import SimpleContextManager
from forge.providers.base import Completion
from forge.providers.mock import MockProvider


def _cm():
    return SimpleContextManager(tier1_provider=lambda: "(tier1)")


def test_unambiguous_prompt_passes_through():
    prompt = "add a retry decorator to forge/providers/ollama.py with 3 attempts"
    assert is_unambiguous(prompt)
    intent = clarity_check(prompt, MockProvider(), _cm())
    assert intent.resolved == prompt
    assert not intent.needs_user


def test_external_reference_prompts_consult_the_clarifier():
    """A long, fluent prompt that leans on info we were NOT given (a doc, a prior
    agreement) must still reach the clarifier model — not be waved through by the
    word-count gate — so it can ask for the missing detail."""

    for prompt in (
        "Implement the endpoints from the design doc I gave you",
        "Add the fields to Note that we discussed earlier",
        "Build it the way we agreed in the meeting",
    ):
        assert not is_unambiguous(prompt), prompt
    # A concrete, self-contained request still passes straight through.
    assert is_unambiguous("Add a delete(note_id) method to NotesStore with a test")


def test_vague_prompt_resolved_from_context():
    clarifier = MockProvider(script=[
        Completion(text=json.dumps({"confident": True, "resolved": "fix add() in calc.py"}))
    ])
    intent = clarity_check("fix the calc thing", clarifier, _cm())
    assert not intent.needs_user
    assert intent.resolved == "fix add() in calc.py"


def test_vague_prompt_asks_one_question():
    clarifier = MockProvider(script=[
        Completion(text=json.dumps({"confident": False, "question": "Which auth module?"}))
    ])
    intent = clarity_check("fix the auth thing", clarifier, _cm())
    assert intent.needs_user
    assert intent.question == "Which auth module?"


def test_unparseable_clarifier_falls_back_to_asking():
    clarifier = MockProvider(script=[Completion(text="I am not JSON at all")])
    intent = clarity_check("fix it please now", clarifier, _cm())
    assert intent.needs_user
    assert intent.question
