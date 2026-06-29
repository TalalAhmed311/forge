"""Summary-first progressive disclosure (Section 8.4) — our simplification.

The master sees cheap SUMMARIES by default and pulls raw via `fetch_raw_context`.
Two safeguards make that safe without the paper's per-segment small models:

  * Auto-expand: if a chunk was activated by a RAW-text pathway (P_vec / P_kw)
    but its summary does not explain why it matched (the matched query terms are
    absent from the summary), expand it to raw automatically — without waiting
    for the master to ask. This closes the buried-detail failure (the "necklace
    hides Sweden" case).
  * Live-raw cap: bound how many raw segments are simultaneously in context, so
    dumping whole segments can't recreate lost-in-the-middle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge.memory.episodic import ChunkStore, tokenize
from forge.memory.router import Activation

RAW_PATHWAYS = {"vec", "kw"}


@dataclass
class Disclosure:
    summaries: list = field(default_factory=list)   # list[(chunk_id, summary, pathway)]
    raws: list = field(default_factory=list)         # list[(chunk_id, raw)]


def _summary_explains_match(query: str, chunk) -> bool:
    """Term-overlap test (Section 17): does the summary cover the matched terms?

    The "matched terms" are query terms that appear in the chunk's raw text. If
    none of those terms appear in the summary, the summary fails to explain why
    the chunk matched and we must expand it.
    """

    q = set(tokenize(query))
    raw_terms = set(chunk.tokens)
    matched = q & raw_terms
    if not matched:
        return True  # nothing concrete matched in raw; summary is as good as it gets
    summary_terms = set(tokenize(chunk.summary))
    return bool(matched & summary_terms)


def disclose(
    query: str,
    activations: list[Activation],
    store: ChunkStore,
    max_live_raw: int = 3,
) -> Disclosure:
    out = Disclosure()
    raw_budget = max_live_raw

    for act in activations:
        chunk = store.get(act.chunk_id)
        if chunk is None:
            continue
        pathway = "+".join(sorted(act.pathways))

        raw_only_match = bool(act.pathways & RAW_PATHWAYS)
        needs_expand = raw_only_match and not _summary_explains_match(query, chunk)

        if needs_expand and raw_budget > 0:
            out.raws.append((chunk.id, chunk.raw))
            raw_budget -= 1
        else:
            out.summaries.append((chunk.id, chunk.summary, pathway))
    return out
