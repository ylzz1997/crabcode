"""Tool system - built-in tools and tool registry."""

from crabcode_core.types.tool import Tool, ToolResult, ToolContext


def get_default_tools() -> list[Tool]:
    """Return the default set of built-in tools."""
    from crabcode_core.tools.bash import BashTool
    from crabcode_core.tools.file_read import FileReadTool
    from crabcode_core.tools.file_edit import FileEditTool
    from crabcode_core.tools.file_write import FileWriteTool
    from crabcode_core.tools.grep import GrepTool
    from crabcode_core.tools.glob import GlobTool
    from crabcode_core.tools.lint import LintTool
    from crabcode_core.tools.memory import MemoryTool

    return [
        BashTool(),
        FileReadTool(),
        FileEditTool(),
        FileWriteTool(),
        GrepTool(),
        GlobTool(),
        LintTool(),
        MemoryTool(),
    ]


__all__ = ["Tool", "ToolResult", "ToolContext", "get_default_tools"]
