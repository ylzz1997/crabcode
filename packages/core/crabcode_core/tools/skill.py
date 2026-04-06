"""SkillTool — lets the model invoke user-defined skills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolResult

if TYPE_CHECKING:
    from crabcode_core.skills.loader import SkillDefinition
    from crabcode_core.types.tool import ToolContext


class SkillTool(Tool):
    """Execute a user-defined skill loaded from SKILL.md files.

    The model calls this tool when the user invokes a skill by name (e.g.
    ``/commit``) or when the task naturally matches a skill's description.
    The tool expands the skill's Markdown content and returns it so the model
    can follow the instructions.
    """

    name = "Skill"
    is_read_only = True
    is_concurrency_safe = True

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Name of the skill to execute.",
            },
            "user_input": {
                "type": "string",
                "description": "The user's additional input or context for the skill.",
            },
        },
        "required": ["skill_name"],
    }

    def __init__(self, skills: list[SkillDefinition]) -> None:
        self._skills = skills
        self._skill_map = {s.name: s for s in skills}

    async def get_prompt(self, **kwargs: Any) -> str:
        lines = [
            "Execute a user-defined skill.\n",
            "When a user invokes /<skill-name> or their request clearly matches a skill's",
            "description, call this tool with the matching skill_name BEFORE generating",
            "any other response. The tool will return the skill's instructions, which you",
            "should then follow to complete the task.\n",
        ]

        if self._skills:
            lines.append("## Available skills\n")
            for skill in self._skills:
                lines.append(f"### {skill.name}")
                if skill.description:
                    lines.append(f"Description: {skill.description}")
                if skill.when_to_use:
                    lines.append(f"When to use: {skill.when_to_use}")
                lines.append("")

        lines.append(
            "IMPORTANT: Only invoke skills listed above. "
            "Do not guess or invent skill names."
        )
        return "\n".join(lines)

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        skill_name = tool_input.get("skill_name", "")
        if skill_name not in self._skill_map:
            available = ", ".join(self._skill_map) or "(none)"
            return f"Unknown skill '{skill_name}'. Available skills: {available}"
        return None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        return PermissionResult(behavior=PermissionBehavior.ALLOW)

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        skill_name = tool_input.get("skill_name", "")
        user_input = tool_input.get("user_input", "")

        skill = self._skill_map.get(skill_name)
        if not skill:
            available = ", ".join(self._skill_map) or "(none)"
            return ToolResult(
                is_error=True,
                result_for_model=f"Unknown skill '{skill_name}'. Available: {available}",
            )

        content = skill.content
        if user_input:
            # Support explicit $USER_INPUT placeholder; otherwise append
            if "$USER_INPUT" in content:
                content = content.replace("$USER_INPUT", user_input)
            else:
                content = f"{content}\n\nUser input: {user_input}"

        return ToolResult(
            result_for_model=content,
            result_for_display=f"[Skill: {skill_name}]",
        )
