"""AskUserTool — present choices to the user and wait for selection."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from crabcode_core.types.event import ChoiceRequestEvent, ChoiceResponseEvent
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class AskUserTool(Tool):
    name = "AskUser"
    description = "Present a question with options for the user to choose from."
    is_read_only = True
    is_concurrency_safe = False  # blocks on user input
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question or prompt to display to the user.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of options for the user to choose from.",
            },
            "multiple": {
                "type": "boolean",
                "description": (
                    "If true, user can select multiple options. "
                    "Default: false (single select)."
                ),
                "default": False,
            },
        },
        "required": ["question", "options"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Present a question with choices to the user when you are unsure "
            "about the next step and want their input. This tool pauses "
            "execution and waits for the user to select one or more options.\n\n"
            "Use this tool when:\n"
            "- There are multiple reasonable approaches and you need the user's preference\n"
            "- You want to confirm the direction before making significant changes\n"
            "- The user might have context you don't about which option is best\n\n"
            "Do NOT use this tool when:\n"
            "- The answer is obvious or there's a clear best approach\n"
            "- The user already told you what to do\n"
            "- You just need a simple yes/no confirmation (use text response instead)\n\n"
            "The tool returns the selected option(s) as a list of strings."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        question = tool_input.get("question", "")
        options = tool_input.get("options", [])
        multiple = tool_input.get("multiple", False)

        if not options:
            return ToolResult(
                result_for_model="Error: at least one option is required",
                is_error=True,
            )

        if len(options) == 1:
            return ToolResult(
                result_for_model=f"User selected: {options[0]}",
                data={"selected": [options[0]]},
            )

        if not context.choice_queue or not context.tool_event_queue:
            # No frontend connected — auto-select the first option
            return ToolResult(
                result_for_model=f"Auto-selected (no interactive frontend): {options[0]}",
                data={"selected": [options[0]]},
            )

        # Generate a unique ID for this choice request
        request_id = str(uuid.uuid4())

        # Emit the choice request event through the tool_event_queue
        request = ChoiceRequestEvent(
            tool_use_id=request_id,
            question=question,
            options=options,
            multiple=multiple,
        )
        await context.tool_event_queue.put(request)

        # Wait for the user's response
        while True:
            response: ChoiceResponseEvent = await context.choice_queue.get()
            if response.tool_use_id == request_id:
                break
            # Put back responses for other requests
            await context.choice_queue.put(response)
            await asyncio.sleep(0.05)

        if response.cancelled:
            return ToolResult(
                result_for_model="User cancelled the selection.",
                is_error=True,
            )

        selected = response.selected
        if multiple:
            result_text = f"User selected: {', '.join(selected)}"
        else:
            result_text = f"User selected: {selected[0] if selected else '(none)'}"

        return ToolResult(
            result_for_model=result_text,
            data={"selected": selected},
        )
