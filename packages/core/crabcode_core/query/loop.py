"""Query loop — the agentic turn loop, heart of the system.

Ported from src/query.ts. Sends messages to the API, processes tool calls,
and loops until the model stops using tools.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Coroutine

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.event import (
    CompactEvent,
    CoreEvent,
    ErrorEvent,
    PermissionRequestEvent,
    StreamModeEvent,
    StreamTextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
)
from crabcode_core.types.message import (
    AssistantMessage,
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from crabcode_core.types.tool import PermissionBehavior, Tool, ToolContext, ToolResult

logger = get_logger(__name__)


@dataclass
class QueryParams:
    messages: list[Message]
    system_prompt: list[str]
    user_context: dict[str, str]
    system_context: dict[str, str]
    tools: list[Tool]
    tool_context: ToolContext
    api_adapter: APIAdapter
    max_turns: int = 0
    permission_manager: Any = None
    permission_queue: asyncio.Queue | None = None
    hook_manager: Any = None


def _append_system_context(
    system_prompt: list[str],
    context: dict[str, str],
) -> list[str]:
    """Append system context entries to the end of system prompt."""
    if not context:
        return system_prompt
    extra = "\n".join(f"{k}: {v}" for k, v in context.items() if v)
    if extra:
        return [*system_prompt, extra]
    return system_prompt


def _prepend_user_context(
    messages: list[Message],
    user_context: dict[str, str],
) -> list[Message]:
    """Prepend user context as a meta user message at the start."""
    if not user_context:
        return messages

    ctx_parts = []
    for key, value in user_context.items():
        ctx_parts.append(f"# {key}\n{value}")
    ctx_text = "\n".join(ctx_parts)

    meta_msg = create_user_message(
        content=(
            f"<system-reminder>\n"
            f"As you answer the user's questions, you can use the following context:\n"
            f"{ctx_text}\n\n"
            f"IMPORTANT: this context may or may not be relevant to your tasks. "
            f"You should not respond to this context unless it is highly relevant "
            f"to your task.\n</system-reminder>\n"
        ),
    )

    return [meta_msg, *messages]


def _find_tool(tools: list[Tool], name: str) -> Tool | None:
    for tool in tools:
        if tool.name == name:
            return tool
    return None


async def _run_tools(
    tool_use_blocks: list[ToolUseBlock],
    assistant_msg: AssistantMessage,
    tools: list[Tool],
    context: ToolContext,
    hook_manager: Any = None,
) -> AsyncGenerator[tuple[list[Message], CoreEvent] | CoreEvent, None]:
    """Execute tool calls, yielding result events and mid-execution events.

    Runs concurrency-safe tools in parallel, others sequentially.
    Yields either (messages, ToolResultEvent) tuples or standalone
    CoreEvents (e.g. ChoiceRequestEvent) emitted by tools during execution.

    When a tool puts events into context.tool_event_queue (e.g. AskUserTool
    emitting a ChoiceRequestEvent), this function monitors the queue concurrently
    with the tool execution and yields those events immediately so the frontend
    can respond (e.g. show a choice UI) while the tool is still awaiting input.
    """
    import asyncio

    safe: list[ToolUseBlock] = []
    unsafe: list[ToolUseBlock] = []
    for block in tool_use_blocks:
        tool = _find_tool(tools, block.name)
        if tool and tool.is_concurrency_safe:
            safe.append(block)
        else:
            unsafe.append(block)

    def _hook_feedback_messages(tag: str, feedbacks: list[str] | None) -> list[Message]:
        if not feedbacks:
            return []
        msgs: list[Message] = []
        for text in feedbacks:
            if not text:
                continue
            msgs.append(
                create_user_message(
                    content=f"<{tag}>\n{text}\n</{tag}>"
                )
            )
        return msgs

    async def execute_one(block: ToolUseBlock) -> tuple[list[Message], CoreEvent]:
        tool = _find_tool(tools, block.name)
        if not tool:
            msg = create_tool_result_message(
                tool_use_id=block.id,
                result=f"Error: unknown tool '{block.name}'",
                is_error=True,
                source_tool_assistant_uuid=assistant_msg.uuid,
            )
            event = ToolResultEvent(
                tool_use_id=block.id,
                tool_name=block.name,
                result=f"Error: unknown tool '{block.name}'",
                is_error=True,
            )
            return [msg], event

        validation_error = await tool.validate_input(block.input)
        if validation_error:
            msg = create_tool_result_message(
                tool_use_id=block.id,
                result=f"Validation error: {validation_error}",
                is_error=True,
                source_tool_assistant_uuid=assistant_msg.uuid,
            )
            event = ToolResultEvent(
                tool_use_id=block.id,
                tool_name=block.name,
                result=f"Validation error: {validation_error}",
                is_error=True,
            )
            return [msg], event

        extra_messages: list[Message] = []
        if hook_manager:
            pre_result = await hook_manager.run(
                "pre_tool_call",
                {
                    "tool_name": block.name,
                    "tool_input": block.input,
                    "tool_use_id": block.id,
                    "agent_id": context.agent_id,
                },
                cwd=context.cwd,
                env=context.env,
            )
            extra_messages.extend(
                _hook_feedback_messages(
                    "pre-tool-call-hook",
                    pre_result.feedback,
                )
            )
            if pre_result.blocked:
                reason = "; ".join(pre_result.details or []) or "blocked by pre_tool_call hook"
                msg = create_tool_result_message(
                    tool_use_id=block.id,
                    result=f"Hook blocked tool call: {reason}",
                    is_error=True,
                    source_tool_assistant_uuid=assistant_msg.uuid,
                )
                event = ToolResultEvent(
                    tool_use_id=block.id,
                    tool_name=block.name,
                    result=f"Hook blocked tool call: {reason}",
                    is_error=True,
                )
                return [*extra_messages, msg], event

        try:
            result = await tool.call(block.input, context)
        except Exception as e:
            logger.exception("Tool execution failed: %s", block.name)
            result = ToolResult(
                result_for_model=f"Error executing tool: {e}",
                is_error=True,
            )

        if hook_manager:
            post_result = await hook_manager.run(
                "post_tool_call",
                {
                    "tool_name": block.name,
                    "tool_input": block.input,
                    "tool_use_id": block.id,
                    "agent_id": context.agent_id,
                    "tool_result": result.result_for_model,
                    "tool_is_error": result.is_error,
                },
                cwd=context.cwd,
                env=context.env,
            )
            extra_messages.extend(
                _hook_feedback_messages(
                    "post-tool-call-hook",
                    post_result.feedback,
                )
            )
            if post_result.blocked:
                reason = "; ".join(post_result.details or []) or "post_tool_call hook failed"
                result = ToolResult(
                    result_for_model=f"{result.result_for_model}\n\nPost hook error: {reason}",
                    result_for_display=result.result_for_display,
                    is_error=True,
                )

        msg = create_tool_result_message(
            tool_use_id=block.id,
            result=result.result_for_model,
            is_error=result.is_error,
            source_tool_assistant_uuid=assistant_msg.uuid,
        )
        event = ToolResultEvent(
            tool_use_id=block.id,
            tool_name=block.name,
            result=result.result_for_model,
            is_error=result.is_error,
            result_for_display=result.result_for_display,
        )
        return [*extra_messages, msg], event

    async def _run_with_event_drain(
        coro: Coroutine[Any, Any, tuple[list[Message], CoreEvent]],
    ) -> AsyncGenerator[tuple[list[Message], CoreEvent] | CoreEvent, None]:
        """Run a tool coroutine while draining mid-execution events from the queue.

        This solves the deadlock: a tool like AskUserTool puts a ChoiceRequestEvent
        into tool_event_queue, then blocks on choice_queue. Without draining, the
        event would never reach the frontend and the tool would wait forever.
        """
        task = asyncio.ensure_future(coro)
        queue = context.tool_event_queue

        if not queue:
            result = await task
            yield result
            return

        while not task.done():
            # Wait for either the tool to finish or an event to arrive
            get_event = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait(
                {task, get_event},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if get_event in done:
                yield get_event.result()
            else:
                get_event.cancel()
                try:
                    await get_event
                except asyncio.CancelledError:
                    pass

            # Drain any remaining events before checking task
            while True:
                try:
                    yield queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        result = task.result()
        # Final drain
        while True:
            try:
                yield queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        yield result

    # Safe tools run in parallel (they shouldn't emit mid-execution events)
    if safe:
        safe_results = await asyncio.gather(*(execute_one(b) for b in safe))
        for item in safe_results:
            yield item

    # Unsafe tools run sequentially with event draining
    for block in unsafe:
            async for item in _run_with_event_drain(execute_one(block)):
                yield item


async def query_loop(
    params: QueryParams,
) -> AsyncGenerator[CoreEvent, None]:
    """The main agentic turn loop.

    Sends messages to the API, processes streaming response, executes
    tool calls, and loops until no more tool calls are made.
    """
    messages = list(params.messages)
    turn_count = 0
    total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    while True:
        turn_count += 1

        full_system = _append_system_context(
            params.system_prompt, params.system_context
        )
        messages_for_api = _prepend_user_context(messages, params.user_context)

        tool_schemas = [t.to_api_schema() for t in params.tools if t.is_enabled]

        model_config = ModelConfig(
            model=params.api_adapter.config.model if hasattr(params.api_adapter, 'config') else "claude-sonnet-4-20250514",
            max_tokens=16384,
            timeout=getattr(params.api_adapter.config, 'timeout', 300) if hasattr(params.api_adapter, 'config') else 300,
        )

        assistant_content: list[ContentBlock] = []
        tool_use_blocks: list[ToolUseBlock] = []
        current_text = ""
        current_thinking = ""
        current_tool: dict[str, str] = {}
        emitted_mode: str = ""

        def _flush_thinking_block() -> None:
            nonlocal current_thinking
            if not current_thinking:
                return
            assistant_content.append(ThinkingBlock(thinking=current_thinking))
            current_thinking = ""

        yield StreamModeEvent(mode="requesting")

        try:
            # Wrap stream with timeout to avoid hanging indefinitely
            async def _stream_with_timeout():
                async for chunk in params.api_adapter.stream_message(
                    messages=messages_for_api,
                    system=full_system,
                    tools=tool_schemas,
                    config=model_config,
                ):
                    yield chunk

            stream = _stream_with_timeout()
            chunk = None
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(),
                        timeout=model_config.timeout,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield ErrorEvent(
                        message=f"API request timed out after {model_config.timeout}s",
                        recoverable=True,
                    )
                    return
                if chunk.type == "text":
                    _flush_thinking_block()
                    if emitted_mode != "responding":
                        yield StreamModeEvent(mode="responding")
                        emitted_mode = "responding"
                    yield StreamTextEvent(text=chunk.text)
                    current_text += chunk.text

                elif chunk.type == "thinking":
                    if emitted_mode != "thinking":
                        yield StreamModeEvent(mode="thinking")
                        emitted_mode = "thinking"
                    yield ThinkingEvent(text=chunk.text)
                    current_thinking += chunk.text

                elif chunk.type == "tool_use_start":
                    _flush_thinking_block()
                    if current_text:
                        assistant_content.append(TextBlock(text=current_text))
                        current_text = ""
                    if emitted_mode != "tool-input":
                        yield StreamModeEvent(mode="tool-input")
                        emitted_mode = "tool-input"
                    current_tool = {
                        "id": chunk.tool_use_id,
                        "name": chunk.tool_name,
                        "input_json": "",
                    }

                elif chunk.type == "tool_use_delta":
                    current_tool["input_json"] = current_tool.get("input_json", "") + chunk.tool_input_json

                elif chunk.type == "tool_use_end":
                    input_json = chunk.tool_input_json or current_tool.get("input_json", "{}")
                    try:
                        tool_input = json.loads(input_json)
                    except json.JSONDecodeError:
                        tool_input = {}

                    block = ToolUseBlock(
                        id=chunk.tool_use_id or current_tool.get("id", ""),
                        name=chunk.tool_name or current_tool.get("name", ""),
                        input=tool_input,
                    )
                    assistant_content.append(block)
                    tool_use_blocks.append(block)

                    yield ToolUseEvent(
                        tool_name=block.name,
                        tool_input=block.input,
                        tool_use_id=block.id,
                    )
                    current_tool = {}

                elif chunk.type == "message_start":
                    if chunk.usage:
                        total_usage["input_tokens"] += chunk.usage.get("input_tokens", 0)

                elif chunk.type == "message_delta":
                    if chunk.usage:
                        total_usage["output_tokens"] += chunk.usage.get("output_tokens", 0)

                elif chunk.type == "message_stop":
                    pass

                elif chunk.type == "error":
                    yield ErrorEvent(
                        message=chunk.error,
                        recoverable=False,
                    )
                    return

        except Exception as e:
            logger.exception("Query loop failed")
            yield ErrorEvent(message=str(e), recoverable=False)
            return

        _flush_thinking_block()
        if current_text:
            assistant_content.append(TextBlock(text=current_text))

        if assistant_content:
            assistant_msg = create_assistant_message(content=assistant_content)
            messages.append(assistant_msg)
        else:
            yield TurnCompleteEvent(
                reason="empty_response",
                turn_count=turn_count,
                usage=total_usage,
            )
            params.messages[:] = messages
            return

        if not tool_use_blocks:
            yield TurnCompleteEvent(
                reason="end_turn",
                turn_count=turn_count,
                usage=total_usage,
            )
            params.messages[:] = messages
            return

        approved_blocks: list[ToolUseBlock] = []

        for block in tool_use_blocks:
            tool = _find_tool(params.tools, block.name)

            if not params.permission_manager or not params.permission_queue:
                approved_blocks.append(block)
                continue

            if not tool:
                approved_blocks.append(block)
                continue

            perm = params.permission_manager.check(tool, block.input)

            if perm.behavior == PermissionBehavior.ALLOW:
                approved_blocks.append(block)
            elif perm.behavior == PermissionBehavior.DENY:
                reason = perm.reason or "denied by rules"
                msg = create_tool_result_message(
                    tool_use_id=block.id,
                    result=f"Permission denied: {reason}",
                    is_error=True,
                    source_tool_assistant_uuid=assistant_msg.uuid,
                )
                messages.append(msg)
                yield ToolResultEvent(
                    tool_use_id=block.id,
                    tool_name=block.name,
                    result=f"Permission denied: {reason}",
                    is_error=True,
                )
            elif perm.behavior == PermissionBehavior.ASK:
                yield PermissionRequestEvent(
                    tool_name=block.name,
                    tool_input=block.input,
                    tool_use_id=block.id,
                )
                response = await params.permission_queue.get()
                if response.allowed:
                    if response.always_allow:
                        params.permission_manager.add_allow_rule(tool.name)
                    approved_blocks.append(block)
                else:
                    msg = create_tool_result_message(
                        tool_use_id=block.id,
                        result=(
                            "Permission denied by user. Do not retry the same "
                            "tool call. Explain what you wanted to do and ask "
                            "for guidance."
                        ),
                        is_error=True,
                        source_tool_assistant_uuid=assistant_msg.uuid,
                    )
                    messages.append(msg)
                    yield ToolResultEvent(
                        tool_use_id=block.id,
                        tool_name=block.name,
                        result="Permission denied by user.",
                        is_error=True,
                    )

        if approved_blocks:
            yield StreamModeEvent(mode="tool-running")
            async for item in _run_tools(
                approved_blocks,
                assistant_msg,
                params.tools,
                params.tool_context,
                params.hook_manager,
            ):
                if isinstance(item, tuple):
                    msgs, event = item
                    messages.extend(msgs)
                    yield event
                else:
                    # Mid-execution event (e.g. ChoiceRequestEvent)
                    yield item

        if params.max_turns and turn_count >= params.max_turns:
            yield TurnCompleteEvent(
                reason="max_turns_reached",
                turn_count=turn_count,
                usage=total_usage,
            )
            params.messages[:] = messages
            return
