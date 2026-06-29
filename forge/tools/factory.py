"""Standard tool sets per role (Section 6.3)."""

from __future__ import annotations

from forge.tools.base import ToolRegistry
from forge.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from forge.tools.memory_tools import FetchRawContextTool, SearchContextTool
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
