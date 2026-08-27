"""Tool system - built-in tools and tool registry."""

from crabcode_core.types.tool import Tool, ToolResult, ToolContext


def get_default_tools() -> list[Tool]:
    """Return the default set of built-in tools."""
    from crabcode_core.tools.agent import (
        AgentCancelTool,
        AgentSendInputTool,
        AgentStatusTool,
        AgentWaitTool,
    )
    from crabcode_core.tools.ask_user import AskUserTool
    from crabcode_core.tools.checklist import ChecklistTool
    from crabcode_core.tools.checkpoint import CheckpointTool
    from crabcode_core.tools.bash import BashTool
    from crabcode_core.tools.browser import BrowserTool
    from crabcode_core.tools.image import ImageTool
    from crabcode_core.tools.file_read import FileReadTool
    from crabcode_core.tools.file_edit import FileEditTool
    from crabcode_core.tools.file_write import FileWriteTool
    from crabcode_core.tools.grep import GrepTool
    from crabcode_core.tools.goal import CreateGoalTool, GetGoalTool, UpdateGoalTool
    from crabcode_core.tools.glob import GlobTool
    from crabcode_core.tools.lint import LintTool
    from crabcode_core.tools.memory import MemoryTool
    from crabcode_core.tools.monitor import MonitorManager, MonitorTool, TaskListTool, TaskStopTool
    from crabcode_core.tools.peer import ListAgentsTool, SendMessageTool
    from crabcode_core.tools.revert import RevertTool
    from crabcode_core.tools.switch_mode import SwitchModeTool
    from crabcode_core.tools.schedule import (
        ScheduleCancelTool,
        ScheduleCreateTool,
        ScheduleListTool,
        SchedulePauseTool,
        ScheduleResumeTool,
        ScheduleRunTool,
        ScheduleStatusTool,
    )
    from crabcode_core.tools.team import (
        TeamBroadcastTool,
        TeamCreateTool,
        TeamMessageTool,
        TeamShutdownTool,
        TeamSpawnTool,
        TeamStatusTool,
        TeamTaskAddTool,
        TeamTaskClaimTool,
        TeamTaskCompleteTool,
    )
    from crabcode_core.tools.web_search import WebSearchTool

    monitor_manager = MonitorManager()

    return [
        BashTool(),
        FileReadTool(),
        FileEditTool(),
        FileWriteTool(),
        GrepTool(),
        GlobTool(),
        WebSearchTool(),
        BrowserTool(),
        ImageTool(),
        LintTool(),
        MemoryTool(),
        MonitorTool(monitor_manager),
        TaskListTool(monitor_manager),
        TaskStopTool(monitor_manager),
        AgentStatusTool(),
        AgentWaitTool(),
        AgentCancelTool(),
        AgentSendInputTool(),
        ListAgentsTool(),
        SendMessageTool(),
        AskUserTool(),
        CheckpointTool(),
        ChecklistTool(),
        CreateGoalTool(),
        GetGoalTool(),
        UpdateGoalTool(),
        SwitchModeTool(),
        TeamCreateTool(),
        TeamSpawnTool(),
        TeamMessageTool(),
        TeamBroadcastTool(),
        TeamStatusTool(),
        TeamTaskAddTool(),
        TeamTaskClaimTool(),
        TeamTaskCompleteTool(),
        TeamShutdownTool(),
        RevertTool(),
        ScheduleCreateTool(),
        ScheduleListTool(),
        ScheduleCancelTool(),
        ScheduleStatusTool(),
        SchedulePauseTool(),
        ScheduleResumeTool(),
        ScheduleRunTool(),
    ]


__all__ = ["Tool", "ToolResult", "ToolContext", "get_default_tools"]
