"""Drive the actual CLI input loop through model failures and recovery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from crabcode_cli import repl
from crabcode_core.events import CoreSession
from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import ErrorEvent, SteeringAppliedEvent, StreamModeEvent, TurnCompleteEvent
from crabcode_core.types.message import create_user_message
from crabcode_core.types.tool import ToolContext


class RecoverySession(CoreSession):
    """Keep real turn ownership/model switching; isolate tools and storage."""

    def __init__(self, failure, *, settings: CrabCodeSettings | None = None):
        super().__init__(settings=settings or CrabCodeSettings(
            default_model="healthy",
            models={name: ApiConfig(provider="openai", model=name)
                    for name in ("healthy", "broken", "invalid")},
        ))
        self.failure = failure
        self.finished = []
        self.completed = []

    async def initialize(self):
        self._initialized = True

    async def _send_message_impl(self, text, **kwargs):
        try:
            yield StreamModeEvent(mode="requesting")
            if self._current_model_name == "broken":
                if isinstance(self.failure, Exception):
                    raise self.failure
                yield self.failure
            else:
                self.completed.append(text)
                yield TurnCompleteEvent()
        finally:
            self.finished.append(text)


class CliApiRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def running_repl(self, session, adapter_factory=None):
        output = StringIO()
        composers = []
        loop_errors = []
        real_composer = repl._PersistentComposer

        def make_composer(*args):
            composer = real_composer(*args)
            composers.append(composer)
            return composer

        def make_adapter(config):
            if config.model == "invalid":
                raise ValueError("Invalid API URL [/provider]")
            return object()

        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
            patch.object(repl, "CoreSession", return_value=session),
            patch.object(repl, "_PersistentComposer", side_effect=make_composer),
            patch.object(repl, "console", Console(file=output, width=180)),
            patch("crabcode_core.api.create_adapter", side_effect=adapter_factory or make_adapter),
            # Turn renderer callback failures into test failures instead of
            # allowing prompt_toolkit's "Press ENTER" recovery prompt to hide them.
            patch.object(Application, "_handle_exception",
                         side_effect=lambda loop, context: loop_errors.append(context)),
        ):
            task = asyncio.create_task(repl.run_repl(settings=session.settings))

            async def wait_for(predicate):
                async def poll():
                    while True:
                        if loop_errors:
                            self.fail(f"Unhandled event loop exception: {loop_errors[0]}")
                        if predicate():
                            return
                        if task.done():
                            await task  # Surface an unexpected CLI exit immediately.
                            self.fail("CLI exited before the next command")
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(poll(), 3)

            try:
                await wait_for(lambda: composers and composers[0].prompt_session.app.is_running)
                yield pipe, composers[0], output, wait_for
                pipe.send_text("\x04")
                await asyncio.wait_for(task, 3)
                self.assertEqual(loop_errors, [])
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def assert_failure_notice_rendered(self, composer, wait_for):
        def notice_visible():
            screen = composer.prompt_session.app.renderer._last_screen
            if screen is None:
                return False
            return any(
                "Request failed · retry or use /model <name>" in "".join(
                    cell.char for _, cell in sorted(row.items())
                )
                for row in screen.data_buffer.values()
            )

        # Wait for the actual screen, not just output or _busy: the next input
        # clears the notice and can otherwise race past a broken status redraw.
        await wait_for(notice_visible)

    async def assert_recovery(self, failure, expected):
        session = RecoverySession(failure)
        async with self.running_repl(session) as (pipe, composer, output, wait_for):
            pipe.send_text("/model broken\r")
            await wait_for(lambda: "Switched to broken" in output.getvalue())
            pipe.send_text("first request\r")
            await wait_for(lambda: expected in output.getvalue() and not composer._busy)
            await self.assert_failure_notice_rendered(composer, wait_for)
            self.assertFalse(session._closed)
            self.assertFalse(session._turn_lock.locked())
            self.assertFalse(session._foreground_turn_active)
            self.assertFalse(composer._activity_running)
            self.assertEqual(session.finished, ["first request"])
            pipe.send_text("/model healthy\r")
            await wait_for(lambda: "Switched to healthy" in output.getvalue())
            pipe.send_text("second request\r")
            await wait_for(lambda: session.completed == ["second request"] and not composer._busy)
            self.assertEqual(session.finished, ["first request", "second request"])

    async def test_connection_exception_keeps_cli_usable(self):
        await self.assert_recovery(httpx.ConnectError("Connection refused"), "Connection refused")

    async def test_actual_repl_promotes_queued_messages_by_key_and_command(self):
        class QueuedSession(RecoverySession):
            def __init__(self):
                super().__init__(None)
                self.gate = asyncio.Event()
                self.guidance = []

            async def _send_message_impl(self, text, **kwargs):
                self.completed.append(text)
                yield StreamModeEvent(mode="requesting")
                if text == "first":
                    await self.gate.wait()
                guidance = self._drain_steering_messages_for_query()
                self.guidance.extend(message.text_content for message in guidance)
                if guidance:
                    yield SteeringAppliedEvent(count=len(guidance))
                yield TurnCompleteEvent()

        session = QueuedSession()
        async with self.running_repl(session) as (pipe, composer, output, wait_for):
            pipe.send_text("first\r")
            await wait_for(lambda: session.completed == ["first"])
            pipe.send_text("second\rthird\rfourth\r")
            await wait_for(lambda: len(composer._queued_turns) == 3)
            self.assertTrue(all(item[2] for item in session._queued_follow_ups))
            pipe.send_text("\x13")
            await wait_for(lambda: len(composer._queued_turns) == 2)
            pipe.send_text("/queue\r")
            await wait_for(lambda: "#2 fourth" in output.getvalue())
            pipe.send_text("/queue steer 2\r")
            await wait_for(lambda: len(composer._queued_turns) == 1)
            self.assertEqual(composer._queued_turns[0].text, "third")
            self.assertEqual(composer.follow_up_mode, "queue")
            session.gate.set()
            await wait_for(lambda: not composer._busy)
            self.assertEqual(session.guidance, ["second", "fourth"])
            self.assertEqual(session.completed, ["first", "third"])
            self.assertEqual(composer.cancel_pending_follow_ups(), [])
            pipe.send_text("/queue\r")
            await wait_for(lambda: "No queued messages." in output.getvalue())

    async def test_resume_picker_select_cancel_and_continue_in_actual_repl(self):
        from crabcode_cli import session_picker

        session = RecoverySession(None)
        pickers = []
        real_picker = session_picker.SessionPicker

        def make_picker(*args):
            picker = real_picker(*args)
            pickers.append(picker)
            return picker

        remote = dict(id="remote-session", cwd="/other-project", title="历史 session",
                      updated_at=100, created_at=50)
        with (
            patch.object(session_picker, "SessionPicker", side_effect=make_picker),
            patch.object(session_picker, "run_io", new=AsyncMock(return_value=[remote])),
            patch("crabcode_core.session.storage.SessionStorage.list_sessions", return_value=[]),
            patch("crabcode_core.session.meta_db.SessionMetaStore.get", return_value=remote),
            patch.object(session, "resume", new=AsyncMock(return_value=True)) as resume,
        ):
            async with self.running_repl(session) as (pipe, composer, output, wait_for):
                pipe.send_text("/resume\r")
                await wait_for(lambda: pickers and pickers[0].app.is_running and not pickers[0].loading)
                self.assertFalse(composer.prompt_session.app.is_running)
                # Browse all projects, return search focus, then resume.
                pipe.send_text("\t\x1b[C\t\t历史")
                await wait_for(lambda: len(pickers[0].matches) == 1)
                pipe.send_text("\r")
                await wait_for(lambda: "Resumed session" in output.getvalue())
                resume.assert_awaited_once_with("remote-session")
                self.assertIn("Found in project: /other-project", output.getvalue())
                pipe.send_text("/resume\r")
                await wait_for(lambda: len(pickers) == 2 and pickers[1].app.is_running)
                pipe.send_text("\x1b")
                await wait_for(lambda: not pickers[1].app.is_running)
                await wait_for(lambda: composer.prompt_session.app.is_running)
                resume.assert_awaited_once()
                pipe.send_text("after picker\r")
                await wait_for(lambda: session.completed == ["after picker"] and not composer._busy)

    async def test_resume_with_argument_does_not_open_picker(self):
        session = RecoverySession(None)
        with (
            patch("crabcode_cli.session_picker.select_session", new=AsyncMock()) as picker,
            patch("crabcode_core.session.storage.SessionStorage.list_sessions",
                  return_value=[{"session_id": "saved-session"}]),
            patch.object(session, "resume", new=AsyncMock(return_value=True)) as resume,
        ):
            for argument in ("saved-session", "saved", "1"):
                await repl._handle_command(f"/resume {argument}", session, session.settings, [])
                resume.assert_awaited_with("saved-session")
            picker.assert_not_awaited()

    async def test_model_picker_switch_failure_cancel_and_continue_in_actual_repl(self):
        from crabcode_cli import model_picker

        session = RecoverySession(None)
        pickers = []
        real_picker = model_picker.ModelPicker

        def make_picker(*args):
            picker = real_picker(*args)
            pickers.append(picker)
            return picker

        with patch.object(model_picker, "ModelPicker", side_effect=make_picker):
            async with self.running_repl(session) as (pipe, composer, output, wait_for):
                pipe.send_text("/model broken\r")
                await wait_for(lambda: "Switched to broken" in output.getvalue())
                self.assertEqual(pickers, [])
                adapter = session._api_adapter

                async def open_picker():
                    previous = len(pickers)
                    pipe.send_text("/model\r")
                    await wait_for(lambda: len(pickers) > previous and pickers[-1].app.is_running)
                    self.assertFalse(composer.prompt_session.app.is_running)
                    return pickers[-1]

                picker = await open_picker()
                pipe.send_text("invalid\r")
                await wait_for(lambda: "Failed to switch model" in output.getvalue()
                               and composer.prompt_session.app.is_running)
                self.assertEqual(session._current_model_name, "broken")
                self.assertIs(session._api_adapter, adapter)

                picker = await open_picker()
                pipe.send_text("healthy")
                await wait_for(lambda: len(picker.matches) == 1)
                pipe.send_text("\r")
                await wait_for(lambda: "Switched to healthy" in output.getvalue()
                               and composer.prompt_session.app.is_running)
                self.assertEqual(session._current_model_name, "healthy")
                adapter = session._api_adapter

                picker = await open_picker()
                self.assertEqual(picker.matches[picker.selected]["name"], "healthy")
                pipe.send_text("\x1b[B\x1b")
                await wait_for(lambda: not picker.app.is_running and composer.prompt_session.app.is_running)
                self.assertEqual(session._current_model_name, "healthy")
                self.assertIs(session._api_adapter, adapter)

                await open_picker()
                pipe.send_text("\r")  # Accepting the active model is a no-op.
                await wait_for(lambda: composer.prompt_session.app.is_running)
                self.assertIs(session._api_adapter, adapter)
                pipe.send_text("after model picker\r")
                await wait_for(lambda: session.completed == ["after model picker"] and not composer._busy)

    async def test_timeout_exception_keeps_cli_usable(self):
        await self.assert_recovery(httpx.ReadTimeout("Request timed out"), "Request timed out")

    async def test_non_retryable_error_finishes_turn_before_next_input(self):
        await self.assert_recovery(ErrorEvent(message="404 API endpoint gone", recoverable=False),
                                   "404 API endpoint gone")

    async def test_provider_error_markup_is_rendered_literally(self):
        await self.assert_recovery(ErrorEvent(message="401 [/provider] [red]expired key",
                                             recoverable=False),
                                   "401 [/provider] [red]expired key")

    async def test_invalid_model_configuration_preserves_current_model(self):
        session = RecoverySession(None)
        async with self.running_repl(session) as (pipe, composer, output, wait_for):
            pipe.send_text("/model healthy\r")
            await wait_for(lambda: "Switched to healthy" in output.getvalue())
            adapter = session._api_adapter
            pipe.send_text("/model invalid\r")
            await wait_for(lambda: "Failed to switch model" in output.getvalue())
            self.assertEqual(session._current_model_name, "healthy")
            self.assertIs(session._api_adapter, adapter)
            pipe.send_text("still working\r")
            await wait_for(lambda: session.completed == ["still working"] and not composer._busy)

    async def test_unexpected_model_command_failure_keeps_cli_usable(self):
        session = RecoverySession(None)
        async with self.running_repl(session) as (pipe, composer, output, wait_for):
            with patch.object(session, "switch_model", side_effect=RuntimeError("switch failed [/api]")):
                pipe.send_text("/model broken\r")
                await wait_for(lambda: "switch failed [/api]" in output.getvalue())
            pipe.send_text("/model healthy\r")
            await wait_for(lambda: "Switched to healthy" in output.getvalue())
            pipe.send_text("still working\r")
            await wait_for(lambda: session.completed == ["still working"] and not composer._busy)

    async def test_real_http_404_then_switch_and_successful_stream(self):
        await self.assert_http_error_recovery("404 Not Found", "Endpoint expired [/provider]")

    async def test_real_http_401_then_switch_and_successful_stream(self):
        await self.assert_http_error_recovery(
            "401 Unauthorized",
            "Authentication Fails, Your api key: ****test is invalid <key> & [/provider]",
        )

    async def assert_http_error_recovery(self, error_status, error_message):
        requests = []
        adapters = []

        async def handle_http(reader, writer):
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode()
                path = headers.split()[1]
                length = next(int(line.split(":", 1)[1]) for line in headers.splitlines()
                              if line.lower().startswith("content-length:"))
                requests.append((path, json.loads(await reader.readexactly(length))))
                if path.startswith("/broken/"):
                    status, content_type = error_status, "application/json"
                    body = json.dumps({"error": {"message": error_message}})
                else:
                    status, content_type = "200 OK", "text/event-stream"
                    chunk = {"id": "local-test", "object": "chat.completion.chunk", "created": 0,
                             "model": "healthy", "choices": [{"index": 0,
                                 "delta": {"content": "Recovered successfully"}, "finish_reason": "stop"}]}
                    body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
                payload = body.encode()
                writer.write((f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                              f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode()
                             + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        class HttpSession(RecoverySession):
            async def _send_message_impl(self, text, **kwargs):
                self.messages.append(create_user_message(content=text))
                params = QueryParams(
                    messages=self.messages, system_prompt=[], user_context={}, system_context={},
                    tools=[], tool_context=ToolContext(cwd=self.cwd), api_adapter=self._api_adapter,
                    api_config=self.settings.get_api_config(self._current_model_name),
                    auto_compact_enabled=False,
                )
                async for event in query_loop(params):
                    if isinstance(event, TurnCompleteEvent):
                        self.completed.append(text)
                    yield event

        def make_adapter(config):
            adapter = OpenAIAdapter(config)
            adapter.client.max_retries = 0
            adapters.append(adapter)
            return adapter

        server = await asyncio.start_server(handle_http, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # Model switches merge the constructor's settings snapshot, so the
        # local HTTP configuration must be complete before creating a session.
        settings = CrabCodeSettings(
            default_model="healthy",
            models={name: ApiConfig(
                provider="openai", model=name,
                base_url=f"http://127.0.0.1:{port}/{name}",
                api_key_env="CRABCODE_RECOVERY_TEST_KEY",
                network_mode="direct", max_retries=0, timeout=2,
            ) for name in ("healthy", "broken", "invalid")},
        )
        session = HttpSession(None, settings=settings)
        try:
            with patch.dict("os.environ", {"CRABCODE_RECOVERY_TEST_KEY": "local-test-only"}):
                async with self.running_repl(session, make_adapter) as (pipe, composer, output, wait_for):
                    pipe.send_text("/model broken\r")
                    await wait_for(lambda: "Switched to broken" in output.getvalue())
                    pipe.send_text("first request\r")
                    await wait_for(lambda: error_message in output.getvalue()
                                   and not composer._busy)
                    await self.assert_failure_notice_rendered(composer, wait_for)
                    self.assertFalse(session._turn_lock.locked())
                    pipe.send_text("/model healthy\r")
                    await wait_for(lambda: "Switched to healthy" in output.getvalue())
                    pipe.send_text("second request\r")
                    await wait_for(lambda: session.completed == ["second request"] and not composer._busy)
                    self.assertEqual([path for path, _ in requests],
                                     ["/broken/chat/completions", "/healthy/chat/completions"])
                    self.assertEqual(session.messages[-1].text_content, "Recovered successfully")
                    self.assertEqual([message["content"] for message in requests[-1][1]["messages"]],
                                     ["first request", "second request"])
        finally:
            server.close()
            await server.wait_closed()
            for adapter in adapters:
                await adapter.client.close()
