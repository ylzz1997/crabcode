"""Agent Team tools — LLM-callable tools for managing teams.

These tools allow the lead agent to create teams, spawn teammates,
send messages, manage tasks, and shut down teams through tool calls.
"""

from __future__ import annotations

from typing import Any

from crabcode_core.team.manager import TeamManager
from crabcode_core.team.models import TeammateRole
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class TeamCreateTool(Tool):
    name = "TeamCreate"
    description = "Create a new agent team for coordinated multi-agent work."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Name for the team (also used as team ID).",
            },
            "max_teammates": {
                "type": "integer",
                "description": "Maximum number of teammates (default: 8).",
            },
        },
        "required": ["name"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        try:
            team_id = await manager.create_team(
                tool_input["name"],
                max_teammates=tool_input.get("max_teammates"),
            )
            return ToolResult(
                data={"team_id": team_id},
                result_for_model=f"Team created: {team_id}",
            )
        except ValueError as exc:
            return ToolResult(result_for_model=f"Error: {exc}", is_error=True)


class TeamSpawnTool(Tool):
    name = "TeamSpawn"
    description = "Spawn a teammate agent within a team."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {
                "type": "string",
                "description": "The team to add the teammate to.",
            },
            "role": {
                "type": "string",
                "enum": ["lead", "worker", "researcher", "reviewer"],
                "description": "Role for the teammate (default: worker).",
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The task/prompt for the teammate agent.",
            },
            "name": {
                "type": "string",
                "description": "Optional display name for the teammate.",
            },
            "model_profile": {
                "type": "string",
                "description": "Optional model profile override (enables multi-model teams).",
            },
        },
        "required": ["team_id", "prompt"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        try:
            agent_id = await manager.add_teammate(
                tool_input["team_id"],
                role=TeammateRole(tool_input.get("role", "worker")),
                prompt=tool_input["prompt"],
                name=tool_input.get("name"),
                model_profile=tool_input.get("model_profile"),
            )
            return ToolResult(
                data={"agent_id": agent_id},
                result_for_model=f"Teammate spawned: {agent_id}",
            )
        except ValueError as exc:
            return ToolResult(result_for_model=f"Error: {exc}", is_error=True)


class TeamMessageTool(Tool):
    name = "TeamMessage"
    description = "Send a message to a specific teammate in your team."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Your team ID."},
            "to": {"type": "string", "description": "Agent ID of the recipient."},
            "text": {"type": "string", "minLength": 1, "description": "Message content."},
        },
        "required": ["team_id", "to", "text"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        from_agent = context.agent_id or ""
        msg = await manager.send_message(
            tool_input["team_id"],
            from_agent=from_agent,
            to_agent=tool_input["to"],
            text=tool_input["text"],
        )
        if msg is None:
            return ToolResult(result_for_model="Error: message delivery failed", is_error=True)
        return ToolResult(
            data={"message_id": msg.id, "delivered": True},
            result_for_model=f"Message delivered to {tool_input['to'][:8]}",
        )


class TeamBroadcastTool(Tool):
    name = "TeamBroadcast"
    description = "Broadcast a message to all teammates in your team (except yourself)."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Your team ID."},
            "text": {"type": "string", "minLength": 1, "description": "Message content."},
        },
        "required": ["team_id", "text"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        from_agent = context.agent_id or ""
        messages = await manager.broadcast(
            tool_input["team_id"],
            from_agent=from_agent,
            text=tool_input["text"],
        )
        return ToolResult(
            data={"recipient_count": len(messages)},
            result_for_model=f"Broadcast delivered to {len(messages)} teammate(s)",
        )


class TeamStatusTool(Tool):
    name = "TeamStatus"
    description = "View team status, teammate states, and task board summary."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID to inspect."},
        },
        "required": ["team_id"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        status = manager.get_team_status(tool_input["team_id"])
        if not status:
            return ToolResult(result_for_model=f"Error: team '{tool_input['team_id']}' not found", is_error=True)

        lines = [
            f"Team: {status['team_id']}  State: {status['state']}",
            f"Teammates: {status['teammate_count']}/{status['max_teammates']}",
        ]
        for t in status["teammates"]:
            name = t.get("name") or t["agent_id"][:8]
            lines.append(f"  {name} · {t['role']} · {t['state']}")
        tasks = status["tasks"]
        lines.append(
            f"Tasks: {tasks['total']} total "
            f"({tasks['pending']} pending, {tasks['claimed']} claimed, "
            f"{tasks['completed']} done, {tasks['failed']} failed)"
        )
        return ToolResult(data=status, result_for_model="\n".join(lines))


class TeamTaskAddTool(Tool):
    name = "TeamTaskAdd"
    description = "Add a task to the team's shared task board."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID."},
            "description": {"type": "string", "minLength": 1, "description": "Task description."},
        },
        "required": ["team_id", "description"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        try:
            task_id = await manager.add_task(tool_input["team_id"], tool_input["description"])
            return ToolResult(
                data={"task_id": task_id},
                result_for_model=f"Task added: {task_id}",
            )
        except ValueError as exc:
            return ToolResult(result_for_model=f"Error: {exc}", is_error=True)


class TeamTaskClaimTool(Tool):
    name = "TeamTaskClaim"
    description = "Claim a pending task from the team's task board. Only one agent can claim each task."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID."},
            "task_id": {"type": "string", "description": "Task ID to claim."},
        },
        "required": ["team_id", "task_id"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        agent_id = context.agent_id or ""
        claimed = await manager.claim_task(tool_input["team_id"], tool_input["task_id"], agent_id)
        if not claimed:
            return ToolResult(
                result_for_model="Error: task not found or already claimed",
                is_error=True,
            )
        return ToolResult(
            data={"task_id": tool_input["task_id"], "claimed": True},
            result_for_model=f"Task claimed: {tool_input['task_id'][:8]}",
        )


class TeamTaskCompleteTool(Tool):
    name = "TeamTaskComplete"
    description = "Mark a claimed task as completed with a result."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID."},
            "task_id": {"type": "string", "description": "Task ID to complete."},
            "result": {"type": "string", "description": "Result or summary of the completed task."},
        },
        "required": ["team_id", "task_id"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        completed = await manager.complete_task(
            tool_input["team_id"], tool_input["task_id"], tool_input.get("result", "")
        )
        if not completed:
            return ToolResult(
                result_for_model="Error: task not found or not in claimed state",
                is_error=True,
            )
        return ToolResult(
            data={"task_id": tool_input["task_id"], "completed": True},
            result_for_model=f"Task completed: {tool_input['task_id'][:8]}",
        )


class TeamShutdownTool(Tool):
    name = "TeamShutdown"
    description = "Shut down a team: cancel all teammates and clean up resources."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID to shut down."},
        },
        "required": ["team_id"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.team_manager
        if not manager:
            return ToolResult(result_for_model="Error: team manager unavailable", is_error=True)
        shutdown = await manager.shutdown_team(tool_input["team_id"])
        if not shutdown:
            return ToolResult(result_for_model=f"Error: team '{tool_input['team_id']}' not found", is_error=True)
        return ToolResult(
            data={"team_id": tool_input["team_id"], "shutdown": True},
            result_for_model=f"Team shut down: {tool_input['team_id']}",
        )
