from __future__ import annotations

from forge.tools.base import ToolRegistry
from forge.tools.fs import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from forge.tools.memory_tools import FetchRawContextTool, SearchContextTool
from forge.tools.search import FindSymbolTool, GlobTool, GrepTool
from forge.tools.shell import RunCommandTool


def engineer_tools() -> ToolRegistry:
    """Section 6.3: read/write/list/run + memory recall."""

    return ToolRegistry(
        [
            ReadFileTool(),
            WriteFileTool(),
            ListDirTool(),
            RunCommandTool(),
            SearchContextTool(),
            FetchRawContextTool(),
        ]
    )


def agent_tools() -> ToolRegistry:
    """Claude-Code-style toolset for the interactive agent: navigation + surgical
    edits + write + shell + memory. (todo_write and spawn_subagent need injected
    state and are added by the session.)"""

    return ToolRegistry(
        [
            ReadFileTool(),
            ListDirTool(),
            GlobTool(),
            GrepTool(),
            FindSymbolTool(),
            EditFileTool(),
            WriteFileTool(),
            RunCommandTool(),
        ]
    )


def architect_tools() -> ToolRegistry:
    """The architect inspects but does not write code."""

    return ToolRegistry(
        [
            ReadFileTool(),
            ListDirTool(),
            RunCommandTool(),
            SearchContextTool(),
            FetchRawContextTool(),
        ]
    )
