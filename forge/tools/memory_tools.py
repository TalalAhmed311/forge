"""Memory tools: search_context, fetch_raw_context (Sections 8.3, 8.4, 12).

These are thin model-facing wrappers over the context manager. `fetch_raw_context`
counts against the live-raw cap so several whole segments can't flood the window.
"""

from __future__ import annotations

from forge.tools.base import Tool, ToolContext, ToolResult


class SearchContextTool(Tool):
    name = "search_context"
    description = (
        "Search episodic memory (past turns, tool outputs, prior reasoning) for a "
        "query. Returns chunk summaries with ids; expand one with fetch_raw_context."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        cm = ctx.context_manager
        if cm is None:
            return ToolResult(ok=False, content="no context manager available")
        hits = cm.search(args["query"])
        if not hits:
            return ToolResult(ok=True, content="(no matching memory)")
        lines = [f"[{h.chunk_id}] ({h.pathway}) {h.summary}" for h in hits]
        return ToolResult(ok=True, content="\n".join(lines))


class SearchMemoryTool(Tool):
    name = "search_memory"
    description = (
        "Search LONG-TERM memory across PAST sessions for work related to the "
        "current task (prior decisions, APIs, files). Returns a short cited "
        "briefing; useful when a new task builds on something done before."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        recall = ctx.recall
        if recall is None:
            return ToolResult(ok=False, content="long-term memory is not enabled")
        briefing = recall.recall(
            args["query"],
            project=ctx.project_name or None,
            exclude_session=ctx.session_id or None,
        )
        text = briefing.render()
        return ToolResult(
            ok=True,
            content=text or "(no relevant prior context found)",
            meta={"cited": briefing.cited},
        )


class FetchRawContextTool(Tool):
    name = "fetch_raw_context"
    description = (
        "Fetch the full raw segment for a chunk id returned by search_context. "
        "Use only when a summary is insufficient."
    )
    parameters = {
        "type": "object",
        "properties": {"chunk_id": {"type": "string"}},
        "required": ["chunk_id"],
    }

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        cm = ctx.context_manager
        if cm is None:
            return ToolResult(ok=False, content="no context manager available")
        raw = cm.fetch_raw(args["chunk_id"])
        if raw is None:
            return ToolResult(ok=False, content=f"no such chunk: {args['chunk_id']}")
        return ToolResult(ok=True, content=raw, meta={"chunk_id": args["chunk_id"]})
