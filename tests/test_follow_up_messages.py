"""Follow-ups use separate turn boundaries, even when mixed with steering."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.output import DummyOutput

from crabcode_cli.repl import _PersistentComposer, _handle_queue_command

from crabcode_core.events import CoreSession
from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.event import (
    ErrorEvent, QueuedMessageStartedEvent, QueuedMessageUpdatedEvent, SteeringAppliedEvent, StreamModeEvent, TurnCompleteEvent,
)
from crabcode_gateway.routes.event import (
    _ACTIVE_SESSION_KEY, _WS_TASKS_KEY, _WS_TASK_SESSIONS_KEY,
    _handle_queue_message, _handle_queued_message_action, _handle_send_message, _handle_steer_message,
)
from crabcode_gateway.schemas import core_event_to_payload


class FollowUpSession(CoreSession):
    """Use real turn ownership and queueing with a controllable model boundary."""
    def __init__(self):
        super().__init__(settings=CrabCodeSettings())
        self.session_id = "follow-up-session"
        self.started = []
        self.guidance = []
        self.finished = []
        self.reason = "end_turn"
        self.extra_event = None
        self.gate = None
        self.initialize_gate = None
        self.ready = asyncio.Event()

    async def initialize(self):
        if self.initialize_gate:
            await self.initialize_gate.wait()
        self._initialized = True

    async def _send_message_impl(self, text, **kwargs):
        self._abort_controller.clear()
        self.started.append((text, kwargs.get("images")))
        try:
            self.ready.set()
            yield StreamModeEvent(mode="requesting")
            if self.gate:
                await self.gate.wait()
            if self.extra_event:
                yield self.extra_event
            guidance = self._drain_steering_messages_for_query()
            self.guidance.extend(message.text_content for message in guidance)
            if guidance:
                yield SteeringAppliedEvent(count=len(guidance))
            yield TurnCompleteEvent(reason=self.reason)
        finally:
            self.finished.append(text)


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_delete_and_promote_change_the_actual_queue(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        image = {"media_type": "image/png", "data": "aGVsbG8="}
        for request_id in ("edit", "remove", "steer", "keep"):
            await session.queue_message(request_id, [image], request_id=request_id)
        self.assertTrue(await session.update_queued_message("edit", "edit"))
        self.assertTrue(await session.update_queued_message("remove", "remove"))
        self.assertTrue(await session.update_queued_message("steer", "steer"))
        self.assertFalse(await session.update_queued_message("steer", "steer"))
        self.assertEqual(session._steering_messages[0].content[1].source["data"], image["data"])
        _ = [event async for event in stream]
        self.assertEqual([text for text, _ in session.started], ["first", "keep"])
        self.assertEqual(session.guidance, ["steer"])
        self.assertFalse(await session.update_queued_message("keep", "edit"))

    async def test_failed_promotion_preserves_queue_and_identical_promotions_are_not_coalesced(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        for index in range(100):
            await session.steer_message(str(index))
        await session.queue_message("queued", request_id="q")
        with self.assertRaisesRegex(RuntimeError, "Too many"):
            await session.update_queued_message("q", "steer")
        self.assertEqual(session._queued_follow_ups[0][2], "q")
        session._steering_messages.clear()
        await session.steer_message("queued")
        self.assertTrue(await session.update_queued_message("q", "steer"))
        self.assertEqual(len(session._steering_messages), 2)
        await stream.aclose()

    async def test_queue_is_fifo_and_steering_only_changes_current_turn(self):
        session = FollowUpSession()
        self.assertFalse(await session.queue_message("idle"))
        stream = session.send_message("first")
        await anext(stream)
        image = {"media_type": "image/png", "data": "aGVsbG8="}
        self.assertTrue(await session.queue_message("second", [image], request_id="q2"))
        self.assertTrue(await session.steer_message("correct first"))
        self.assertTrue(await session.queue_message("third", request_id="q3"))
        image["data"] = "changed after submission"
        self.assertEqual(session.started, [("first", None)])
        self.assertIsInstance(await anext(stream), SteeringAppliedEvent)
        self.assertEqual(session.guidance, ["correct first"])
        self.assertIsInstance(await anext(stream), TurnCompleteEvent)
        queued = await anext(stream)
        self.assertIsInstance(queued, QueuedMessageStartedEvent)
        self.assertEqual(session.finished, ["first"])
        self.assertEqual(queued.request_id, "q2")
        self.assertEqual(queued.images[0]["data"], "aGVsbG8=")
        self.assertEqual(core_event_to_payload(queued).type, "queued_message_started")
        rest = [event async for event in stream]
        self.assertEqual([text for text, _ in session.started], ["first", "second", "third"])
        self.assertEqual([event.text for event in rest if isinstance(event, QueuedMessageStartedEvent)], ["third"])
        self.assertFalse(await session.queue_message("too late"))

    async def test_queue_accepted_after_terminal_is_not_lost(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        await anext(stream)  # Terminal yielded; stream has not finished publishing.
        self.assertTrue(await session.queue_message("late follow up"))
        events = [event async for event in stream]
        self.assertTrue(any(isinstance(event, QueuedMessageStartedEvent) for event in events))
        self.assertEqual([text for text, _ in session.started], ["first", "late follow up"])

    async def test_stopped_or_failed_run_does_not_execute_pending_input_later(self):
        for reason in ("error", "interrupted", "context_overflow", "empty_response", "max_tokens", "length", "model_context_window_exceeded"):
            with self.subTest(reason=reason):
                session = FollowUpSession()
                session.reason = reason
                stream = session.send_message("first")
                await anext(stream)
                await session.queue_message("must not run")
                events = [event async for event in stream]
                self.assertFalse(any(isinstance(event, QueuedMessageStartedEvent) for event in events))
                session.reason = "end_turn"
                _ = [event async for event in session.send_message("fresh task")]
                self.assertEqual([text for text, _ in session.started], ["first", "fresh task"])

    async def test_interrupt_at_queue_start_and_generator_close_discard_queue(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        await session.queue_message("second")
        await anext(stream)
        self.assertIsInstance(await anext(stream), QueuedMessageStartedEvent)
        await session.interrupt()
        events = [event async for event in stream]
        self.assertEqual(events[-1].reason, "interrupted")
        self.assertEqual([text for text, _ in session.started], ["first"])
        stream = session.send_message("new")
        await anext(stream)
        await session.queue_message("discarded")
        await stream.aclose()
        self.assertFalse(session._queued_follow_ups)
        self.assertFalse(await session.queue_message("closed stream"))

    async def test_queue_limit_rejects_without_losing_accepted_input(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        for index in range(100):
            await session.queue_message(str(index))
        with self.assertRaisesRegex(RuntimeError, "Too many"):
            await session.queue_message("overflow")
        self.assertEqual(len(session._queued_follow_ups), 100)
        await stream.aclose()

    async def test_background_errors_do_not_discard_foreground_follow_ups(self):
        for agent_id, recoverable, expected in (
            (None, False, ["first"]), (None, True, ["first"]),
            ("worker", False, ["first", "second"]),
        ):
            session = FollowUpSession()
            session.extra_event = ErrorEvent(message="failed", recoverable=recoverable, agent_id=agent_id)
            stream = session.send_message("first")
            await anext(stream)
            await session.queue_message("second")
            _ = [event async for event in stream]
            self.assertEqual([text for text, _ in session.started], expected)


class CliFollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_promotes_selected_message_with_images_exactly_once(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        image = {"media_type": "image/png", "data": "aGVsbG8="}
        with create_app_session(output=DummyOutput()):
            composer = _PersistentComposer(session, [])
            for request_id in ("q1", "q2", "q3"):
                await session.queue_message("same text", [image], request_id=request_id)
                composer.add_queued_turn("same text", request_id)
            with patch("crabcode_cli.repl.console") as console:
                await _handle_queue_command("/queue", composer)
                self.assertIn("#2 same text", str(console.print.call_args_list))
                await _handle_queue_command("/queue steer 2", composer)
            self.assertEqual([item.request_id for item in composer._queued_turns], ["q1", "q3"])
            self.assertEqual([item[2] for item in session._queued_follow_ups], ["q1", "q3"])
            self.assertEqual(session._steering_messages[0].content[1].source["data"], image["data"])
            self.assertEqual(composer.follow_up_mode, "queue")
            events = [event async for event in stream]
            for event in events:
                if isinstance(event, SteeringAppliedEvent):
                    self.assertEqual(composer.mark_guidance_applied(event.count), ["same text"])
                if isinstance(event, QueuedMessageStartedEvent):
                    composer.mark_queued_turn_started(event.request_id)
            self.assertEqual(session.guidance, ["same text"])
            self.assertEqual([event.request_id for event in events if isinstance(event, QueuedMessageStartedEvent)], ["q1", "q3"])
            self.assertEqual(len(session.started), 3)
            self.assertEqual(composer.cancel_pending_follow_ups(), [])

    async def test_invalid_commands_and_failed_promotion_preserve_both_queues(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        with create_app_session(output=DummyOutput()):
            composer = _PersistentComposer(session, [])
            await session.queue_message("keep", request_id="q1")
            composer.add_queued_turn("keep", "q1")
            with patch("crabcode_cli.repl.console"):
                for text in ("/queue steer 0", "/queue steer -1", "/queue steer 2", "/queue steer bad", "/queue steer 1 extra", "/queue bad"):
                    await _handle_queue_command(text, composer)
                for index in range(100):
                    await session.steer_message(str(index))
                await _handle_queue_command("/queue steer", composer)
            self.assertEqual([item.request_id for item in composer._queued_turns], ["q1"])
            self.assertEqual(session._queued_follow_ups[0][2], "q1")
            self.assertEqual(composer.cancel_pending_follow_ups(), ["keep"])
        await stream.aclose()

    async def test_already_started_message_does_not_promote_the_next_message(self):
        session = FollowUpSession()
        stream = session.send_message("first")
        await anext(stream)
        with create_app_session(output=DummyOutput()):
            composer = _PersistentComposer(session, [])
            for request_id in ("q1", "q2"):
                await session.queue_message(request_id, request_id=request_id)
                composer.add_queued_turn(request_id, request_id)
            await anext(stream)  # First turn complete.
            event = await anext(stream)  # q1 was removed from the core queue.
            self.assertEqual(event.request_id, "q1")
            # The display has not yet handled QueuedMessageStartedEvent.
            self.assertFalse(await composer.promote_queued_turn("q1"))
            self.assertEqual(session._steering_messages, [])
            self.assertEqual(session._queued_follow_ups[0][2], "q2")
            composer.mark_queued_turn_started(event.request_id)
            self.assertEqual(composer.cancel_pending_follow_ups(), ["q2"])
        await stream.aclose()


class Socket:
    def __init__(self, session):
        self.errors = []
        self.events = []
        async def publish(_session_id, event, **_kwargs):
            self.events.append(event)
        def publish_nowait(_session_id, event, **_kwargs):
            self.events.append(event)
        self.app = SimpleNamespace(state=SimpleNamespace(
            sessions={session.session_id: session}, event_bus=SimpleNamespace(publish=publish, publish_nowait=publish_nowait),
        ))
        self.scope = {_ACTIVE_SESSION_KEY: session.session_id, _WS_TASKS_KEY: set(), _WS_TASK_SESSIONS_KEY: {}}

    async def send_text(self, data):
        self.errors.append(json.loads(data))


class GatewayFollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_actions_work_during_initialization_and_active_runs(self):
        for initializing in (True, False):
            with self.subTest(initializing=initializing):
                session = FollowUpSession()
                gate = asyncio.Event()
                if initializing:
                    session.initialize_gate = gate
                else:
                    session.gate = gate
                ws = Socket(session)
                await _handle_send_message(ws, {"type": "send_message", "text": "first", "operation_id": "op"})
                task = next(iter(ws.app.state.background_tasks[session.session_id]))
                try:
                    if not initializing:
                        await asyncio.wait_for(session.ready.wait(), 2)
                    for request_id in ("edit", "remove", "steer", "keep"):
                        await _handle_queue_message(ws, {"type": "queue_message", "text": request_id, "operation_id": "op", "request_id": request_id})
                    for action in ("edit", "remove", "steer"):
                        await _handle_queued_message_action(ws, {"type": "queued_message_action", "action": action, "operation_id": "op", "request_id": action})
                    updates = [event for event in ws.events if isinstance(event, QueuedMessageUpdatedEvent)]
                    self.assertEqual([event.action for event in updates], ["edit", "remove", "steer"])
                    self.assertEqual(core_event_to_payload(updates[-1]).type, "queued_message_updated")
                    gate.set()
                    await asyncio.wait_for(task, 2)
                    self.assertEqual(ws.errors, [])
                    self.assertEqual([text for text, _ in session.started], ["first", "keep"])
                    self.assertEqual(session.guidance, ["steer"])
                    self.assertLess(ws.events.index(updates[-1]), next(i for i, event in enumerate(ws.events) if isinstance(event, SteeringAppliedEvent)))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_stale_and_repeated_actions_cannot_recall_an_already_removed_message(self):
        session = FollowUpSession()
        session.gate = asyncio.Event()
        ws = Socket(session)
        await _handle_send_message(ws, {"type": "send_message", "text": "first", "operation_id": "op"})
        await asyncio.wait_for(session.ready.wait(), 2)
        task = next(iter(ws.app.state.background_tasks[session.session_id]))
        try:
            await _handle_queue_message(ws, {"type": "queue_message", "text": "queued", "operation_id": "op", "request_id": "q"})
            msg = {"type": "queued_message_action", "action": "edit", "operation_id": "old-op", "request_id": "q"}
            await _handle_queued_message_action(ws, msg)
            self.assertEqual(ws.errors[-1]["error_type"], "operation_not_found")
            msg["operation_id"] = "op"
            await _handle_queued_message_action(ws, msg)
            await _handle_queued_message_action(ws, msg)
            self.assertEqual(ws.errors[-1]["error_type"], "queued_message_not_found")
            self.assertEqual(ws.errors[-1]["request_id"], "q")
            self.assertEqual(sum(isinstance(event, QueuedMessageUpdatedEvent) for event in ws.events), 1)
            session.gate.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual([text for text, _ in session.started], ["first"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_follow_up_is_accepted_before_the_foreground_task_starts(self):
        session = FollowUpSession()
        session.initialize_gate = asyncio.Event()
        ws = Socket(session)
        await _handle_send_message(ws, {"type": "send_message", "text": "first", "operation_id": "op"})
        # Initialization is still in progress; Core cannot accept input yet.
        await _handle_queue_message(ws, {"type": "queue_message", "text": "early", "operation_id": "op", "request_id": "early-id"})
        session.initialize_gate.set()
        tasks = list(ws.app.state.background_tasks.get(session.session_id, []))
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.assertEqual(ws.errors, [])
        self.assertEqual([text for text, _ in session.started], ["first", "early"])
        self.assertEqual(sum(isinstance(event, QueuedMessageStartedEvent) for event in ws.events), 1)

    async def test_one_foreground_operation_executes_queue_without_competing_turns(self):
        session = FollowUpSession()
        session.gate = asyncio.Event()
        ws = Socket(session)
        await _handle_send_message(ws, {"type": "send_message", "text": "first", "operation_id": "op"})
        await asyncio.wait_for(session.ready.wait(), 2)
        task = next(iter(ws.app.state.background_tasks[session.session_id]))
        try:
            await _handle_queue_message(ws, {"type": "queue_message", "text": "second", "operation_id": "op", "request_id": "q2"})
            await _handle_steer_message(ws, {"type": "steer_message", "text": "correct first", "operation_id": "op"})
            self.assertEqual(session.started, [("first", None)])
            session.gate.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(ws.errors, [])
            self.assertEqual([text for text, _ in session.started], ["first", "second"])
            self.assertEqual(session.guidance, ["correct first"])
            self.assertEqual(sum(isinstance(event, TurnCompleteEvent) for event in ws.events), 1)
            self.assertTrue(any(isinstance(event, QueuedMessageStartedEvent) for event in ws.events))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stale_queue_is_rejected_with_request_id_instead_of_sent_to_new_turn(self):
        session = FollowUpSession()
        ws = Socket(session)
        await _handle_queue_message(ws, {"type": "queue_message", "text": "stale", "operation_id": "old-op", "request_id": "q1"})
        self.assertEqual(ws.errors[0]["command"], "queue_message")
        self.assertEqual(ws.errors[0]["request_id"], "q1")
        self.assertEqual(ws.errors[0]["error_type"], "operation_not_found")
        self.assertEqual(session.started, [])
