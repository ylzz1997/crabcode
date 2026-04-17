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
from crabcode_core.types.config import ApiConfig
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
    ToolResultBlock,
    ToolUseBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from crabcode_core.permissions.manager import PermissionMode
from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult

logger = get_logger(__name__)


def _merge_permission_results(
    rule_perm: PermissionResult,
    tool_perm: PermissionResult,
) -> PermissionResult:
    updated_input = tool_perm.updated_input if tool_perm.updated_input is not None else rule_perm.updated_input
    permission_key = tool_perm.permission_key or rule_perm.permission_key

    if rule_perm.behavior == PermissionBehavior.DENY:
        return PermissionResult(
            behavior=PermissionBehavior.DENY,
            reason=rule_perm.reason or tool_perm.reason,
            updated_input=updated_input,
            permission_key=permission_key,
        )
    if tool_perm.behavior == PermissionBehavior.DENY:
        return PermissionResult(
            behavior=PermissionBehavior.DENY,
            reason=tool_perm.reason or rule_perm.reason,
            updated_input=updated_input,
            permission_key=permission_key,
        )
    if (
        rule_perm.behavior == PermissionBehavior.ASK
        or tool_perm.behavior == PermissionBehavior.ASK
    ):
        reason = tool_perm.reason or rule_perm.reason
        return PermissionResult(
            behavior=PermissionBehavior.ASK,
            reason=reason,
            updated_input=updated_input,
            permission_key=permission_key,
        )
    return PermissionResult(
        behavior=PermissionBehavior.ALLOW,
        reason=tool_perm.reason or rule_perm.reason,
        updated_input=updated_input,
        permission_key=permission_key,
    )


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
    agent_mode: str = "agent"  # "agent" | "plan"
    api_config: ApiConfig | None = None  # passed from session for ModelConfig
    context_window: int = 0  # resolved context window size


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


def _parse_context_limit_from_error(error_msg: str) -> int | None:
    """Try to extract the model's actual context limit from an API error message.

    Common patterns:
      - "maximum context length is 202752 tokens"
      - "context window of 128000"
    """
    import re
    m = re.search(r"(?:maximum context length|context window)[^\d]*(\d[\d,_]*)", error_msg, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", "").replace("_", ""))
    return None


def _truncate_to_fit_tokens(
    messages: list[Message],
    target_tokens: int,
    system: list[str] | None = None,
) -> list[Message]:
    """Aggressively truncate messages to fit within target_tokens.

    Strategy: drop middle messages first (fast), then truncate large
    ToolResultBlocks. Modifies messages in-place.
    """
    estimated = estimate_token_count(messages, system=system)
    if estimated <= target_tokens:
        return messages

    # Fast path: drop older messages from index 1 first (keep first & last)
    while estimated > target_tokens and len(messages) > 2:
        messages.pop(1)
        estimated = estimate_token_count(messages, system=system)

    if estimated <= target_tokens:
        return messages

    # Still over: truncate large ToolResultBlocks in remaining messages
    candidates: list[tuple[ToolResultBlock, int]] = []
    for msg in messages:
        if isinstance(msg.content, str):
            continue
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and len(block.content) > 500:
                candidates.append((block, len(block.content)))
    candidates.sort(key=lambda x: x[1], reverse=True)

    for block, original_size in candidates:
        estimated = estimate_token_count(messages, system=system)
        if estimated <= target_tokens:
            break
        overshoot = estimated / target_tokens
        if overshoot > 2:
            keep = min(500, original_size // 10)
        elif overshoot > 1.5:
            keep = min(1000, original_size // 5)
        else:
            keep = max(500, int(original_size / overshoot * 0.7))
        half = keep // 2
        block.content = (
            block.content[:half]
            + "\n\n... (truncated to fit context window) ...\n\n"
            + block.content[-half:]
        )

    return messages


_COMPACT_RESUME_PROMPT = (
    "[Conversation was compacted to fit the context window. "
    "Continue the current task from the summary and latest context "
    "without asking the user to repeat anything.]"
)

_COMPACT_EMPTY_RESPONSE_RETRY_PROMPT = (
    "[The previous attempt returned no content after compaction. "
    "Resume immediately from the compacted summary and latest context. "
    "Either continue the task or make the next tool call now.]"
)


def _append_compact_resume_prompt(messages: list[Message]) -> list[Message]:
    """Append a lightweight resume prompt after emergency compaction."""
    if not messages:
        messages.append(create_user_message(content=_COMPACT_RESUME_PROMPT))
        return messages

    last = messages[-1]
    if isinstance(last.content, str) and last.content == _COMPACT_RESUME_PROMPT:
        return messages

    messages.append(create_user_message(content=_COMPACT_RESUME_PROMPT))
    return messages


async def query_loop(
    params: QueryParams,
) -> AsyncGenerator[CoreEvent, None]:
    """The main agentic turn loop.

    Sends messages to the API, processes streaming response, executes
    tool calls, and loops until no more tool calls are made.
    """
    from crabcode_core.compact.compact import (
        compact_conversation,
        estimate_token_count,
    )

    messages = list(params.messages)
    turn_count = 0
    _context_retries = 0
    _MAX_CONTEXT_RETRIES = 2
    _compact_resume_retries = 0
    _MAX_COMPACT_RESUME_RETRIES = 1
    _awaiting_compact_resume = False
    total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    cfg = params.api_config
    adapter_config = getattr(params.api_adapter, "config", None)
    effective_model = (
        (cfg.model if cfg else None)
        or (getattr(adapter_config, "model", None))
        or "claude-sonnet-4-20250514"
    )
    effective_max_tokens = cfg.max_tokens if cfg else 16384
    effective_thinking = cfg.thinking_enabled if cfg else True
    effective_thinking_budget = cfg.thinking_budget if cfg else 10000
    effective_timeout = (
        cfg.timeout if cfg
        else getattr(adapter_config, "timeout", 300)
    )
    context_window = params.context_window

    while True:
        turn_count += 1

        full_system = _append_system_context(
            params.system_prompt, params.system_context
        )
        messages_for_api = _prepend_user_context(messages, params.user_context)

        tool_schemas = [
            t.to_api_schema()
            for t in params.tools
            if t.is_enabled and (params.agent_mode != "plan" or t.is_read_only)
        ]

        max_tokens = effective_max_tokens

        # --- Pre-flight context window check ---
        if context_window > 0:
            estimated = estimate_token_count(messages_for_api, system=full_system)
            headroom = context_window - max_tokens
            if estimated > headroom:
                logger.warning(
                    "Estimated tokens (%d) exceed headroom (%d = %d - %d). "
                    "Attempting emergency compact.",
                    estimated, headroom, context_window, max_tokens,
                )
                compact_result = await compact_conversation(
                    messages, api_adapter=params.api_adapter
                )
                if compact_result:
                    messages = compact_result
                    _append_compact_resume_prompt(messages)
                    _awaiting_compact_resume = True
                    messages_for_api = _prepend_user_context(messages, params.user_context)
                    yield CompactEvent(
                        summary="Emergency compact: context approaching limit",
                        messages_before=-1,
                        messages_after=len(messages),
                    )
                    estimated = estimate_token_count(messages_for_api, system=full_system)

                if estimated > headroom:
                    logger.warning(
                        "Post-compact estimated %d still > headroom %d, truncating",
                        estimated, headroom,
                    )
                    try:
                        _truncate_to_fit_tokens(messages, headroom, system=full_system)
                    except Exception:
                        logger.exception("Truncation failed, falling back to keep only first+last")
                        if len(messages) > 2:
                            first, last = messages[0], messages[-1]
                            messages.clear()
                            messages.extend([first, last])
                    messages_for_api = _prepend_user_context(messages, params.user_context)
                    estimated = estimate_token_count(messages_for_api, system=full_system)
                    logger.warning(
                        "Post-truncate: estimated=%d, msgs=%d", estimated, len(messages)
                    )

                if estimated > context_window - 1024:
                    max_tokens = max(1024, context_window - estimated - 512)
                    logger.warning(
                        "Reducing max_tokens to %d to fit context window", max_tokens
                    )

        logger.warning(
            "Sending API request: model=%s, max_tokens=%d, msgs=%d, context_window=%d",
            effective_model, max_tokens, len(messages_for_api), context_window,
        )
        model_config = ModelConfig(
            model=effective_model,
            max_tokens=max_tokens,
            thinking_enabled=effective_thinking,
            thinking_budget=effective_thinking_budget,
            timeout=effective_timeout,
            context_window=context_window,
        )

        assistant_content: list[ContentBlock] = []
        tool_use_blocks: list[ToolUseBlock] = []
        current_text = ""
        current_thinking = ""
        current_tool: dict[str, str] = {}
        emitted_mode: str = ""
        _retry_after_compact = False

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
                    is_ctx_err = (
                        "maximum context length" in chunk.error.lower()
                        or ("input_tokens" in chunk.error and "400" in chunk.error)
                        or "context window" in chunk.error.lower()
                        or "prompt is too long" in chunk.error.lower()
                    )
                    if is_ctx_err and _context_retries < _MAX_CONTEXT_RETRIES:
                        _context_retries += 1
                        parsed_limit = _parse_context_limit_from_error(chunk.error)
                        if parsed_limit and parsed_limit > 0:
                            context_window = parsed_limit
                            logger.info("Parsed context window from error: %d", context_window)
                        logger.warning(
                            "Context length error in stream (retry %d/%d), compact + truncate",
                            _context_retries, _MAX_CONTEXT_RETRIES,
                        )
                        cr = await compact_conversation(
                            messages, api_adapter=params.api_adapter
                        )
                        if cr:
                            messages = cr
                            _append_compact_resume_prompt(messages)
                            _awaiting_compact_resume = True
                            yield CompactEvent(
                                summary="Emergency compact after context length error",
                                messages_before=-1,
                                messages_after=len(messages),
                            )
                        retry_headroom = context_window - max_tokens
                        try:
                            _truncate_to_fit_tokens(messages, retry_headroom, system=full_system)
                        except Exception:
                            logger.exception("Truncation failed in stream error handler")
                            if len(messages) > 2:
                                first, last = messages[0], messages[-1]
                                messages.clear()
                                messages.extend([first, last])
                        messages_for_api = _prepend_user_context(messages, params.user_context)
                        turn_count -= 1
                        _retry_after_compact = True
                        break
                    yield ErrorEvent(
                        message=chunk.error,
                        recoverable=False,
                    )
                    return

        except Exception as e:
            error_str = str(e)
            is_context_error = (
                "maximum context length" in error_str.lower()
                or ("input_tokens" in error_str and "400" in error_str)
                or "context window" in error_str.lower()
                or "prompt is too long" in error_str.lower()
            )
            if is_context_error and _context_retries < _MAX_CONTEXT_RETRIES:
                _context_retries += 1
                parsed_limit = _parse_context_limit_from_error(error_str)
                if parsed_limit and parsed_limit > 0:
                    context_window = parsed_limit
                    logger.info("Parsed context window from error: %d", context_window)
                logger.warning(
                    "Context length error (retry %d/%d), compact + truncate and retry",
                    _context_retries, _MAX_CONTEXT_RETRIES,
                )
                compact_result = await compact_conversation(
                    messages, api_adapter=params.api_adapter
                )
                if compact_result:
                    messages = compact_result
                    _append_compact_resume_prompt(messages)
                    _awaiting_compact_resume = True
                    yield CompactEvent(
                        summary="Emergency compact after context length error",
                        messages_before=-1,
                        messages_after=len(messages),
                    )
                retry_headroom = context_window - max_tokens
                try:
                    _truncate_to_fit_tokens(messages, retry_headroom, system=full_system)
                except Exception:
                    logger.exception("Truncation failed in exception handler")
                    if len(messages) > 2:
                        first, last = messages[0], messages[-1]
                        messages.clear()
                        messages.extend([first, last])
                messages_for_api = _prepend_user_context(messages, params.user_context)
                turn_count -= 1
                continue
            logger.exception("Query loop failed")
            yield ErrorEvent(message=error_str, recoverable=False)
            return

        if _retry_after_compact:
            continue

        _flush_thinking_block()
        if current_text:
            assistant_content.append(TextBlock(text=current_text))

        if assistant_content:
            assistant_msg = create_assistant_message(content=assistant_content)
            messages.append(assistant_msg)
            _awaiting_compact_resume = False
        else:
            if _awaiting_compact_resume and _compact_resume_retries < _MAX_COMPACT_RESUME_RETRIES:
                _compact_resume_retries += 1
                logger.warning(
                    "Empty response after compact, retrying (%d/%d)",
                    _compact_resume_retries, _MAX_COMPACT_RESUME_RETRIES,
                )
                messages.append(
                    create_user_message(content=_COMPACT_EMPTY_RESPONSE_RETRY_PROMPT)
                )
                turn_count -= 1
                continue
            logger.warning("Empty response, ending turn (awaiting_resume=%s)", _awaiting_compact_resume)
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

            effective_block = block.model_copy(deep=True)
            rule_perm = params.permission_manager.check(tool, effective_block.input)
            tool_perm = await tool.check_permissions(
                effective_block.input,
                params.tool_context,
            )
            if tool_perm.updated_input is not None:
                effective_block.input = tool_perm.updated_input

            permission_key = tool_perm.permission_key or tool.get_permission_key(effective_block.input)

            bypass_active = (
                hasattr(params.permission_manager, "mode")
                and params.permission_manager.mode == PermissionMode.BYPASS
            )
            if bypass_active and tool_perm.behavior == PermissionBehavior.ASK:
                tool_perm = PermissionResult(
                    behavior=PermissionBehavior.ALLOW,
                    updated_input=tool_perm.updated_input,
                    permission_key=permission_key,
                )

            merged_perm = _merge_permission_results(
                rule_perm,
                PermissionResult(
                    behavior=tool_perm.behavior,
                    reason=tool_perm.reason,
                    updated_input=tool_perm.updated_input,
                    permission_key=permission_key,
                ),
            )

            explicit_allow = params.permission_manager.has_explicit_allow(
                tool,
                effective_block.input,
                permission_key=permission_key,
            )
            if explicit_allow:
                approved_blocks.append(effective_block)
                continue

            if merged_perm.behavior == PermissionBehavior.DENY:
                reason = merged_perm.reason or "denied by policy"
                msg = create_tool_result_message(
                    tool_use_id=effective_block.id,
                    result=f"Permission denied: {reason}",
                    is_error=True,
                    source_tool_assistant_uuid=assistant_msg.uuid,
                )
                messages.append(msg)
                yield ToolResultEvent(
                    tool_use_id=effective_block.id,
                    tool_name=effective_block.name,
                    result=f"Permission denied: {reason}",
                    is_error=True,
                )
            elif merged_perm.behavior == PermissionBehavior.ASK:
                yield PermissionRequestEvent(
                    tool_name=effective_block.name,
                    tool_input=effective_block.input,
                    tool_use_id=effective_block.id,
                    reason=merged_perm.reason,
                    permission_key=permission_key,
                )
                response = await params.permission_queue.get()
                if response.allowed:
                    if response.always_allow:
                        params.permission_manager.add_allow_rule(permission_key)
                    approved_blocks.append(effective_block)
                else:
                    msg = create_tool_result_message(
                        tool_use_id=effective_block.id,
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
                        tool_use_id=effective_block.id,
                        tool_name=effective_block.name,
                        result="Permission denied by user.",
                        is_error=True,
                    )
            else:
                approved_blocks.append(effective_block)

        if approved_blocks:
            yield StreamModeEvent(mode="tool-running")
            should_end_turn_after_tools = False
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
                    if event.tool_name == "SwitchMode" and not event.is_error:
                        should_end_turn_after_tools = True
                    yield event
                else:
                    # Mid-execution event (e.g. ChoiceRequestEvent)
                    yield item

            if should_end_turn_after_tools:
                yield TurnCompleteEvent(
                    reason="mode_switch_requested",
                    turn_count=turn_count,
                    usage=total_usage,
                )
                params.messages[:] = messages
                return

        if params.max_turns and turn_count >= params.max_turns:
            yield TurnCompleteEvent(
                reason="max_turns_reached",
                turn_count=turn_count,
                usage=total_usage,
            )
            params.messages[:] = messages
            return
