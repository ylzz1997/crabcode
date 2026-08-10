from __future__ import annotations

import asyncio
import contextlib
import unittest
from unittest.mock import patch

from crabcode_cli import repl
from crabcode_core.types.event import StreamTextEvent, TurnCompleteEvent


class _FakeSession:
    def __init__(self) -> None:
        self.events: asyncio.Queue[object] = asyncio.Queue()

    async def next_background_event(self):
        return await self.events.get()


class BackgroundEventConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_continuation_text_is_rendered(self) -> None:
        session = _FakeSession()
        completed = asyncio.Event()

        @contextlib.asynccontextmanager
        async def fake_in_terminal():
            yield

        def record_complete(_event: TurnCompleteEvent) -> None:
            completed.set()

        with (
            patch.object(repl, "in_terminal", fake_in_terminal),
            patch.object(repl.console, "print") as print_mock,
            patch.object(repl, "_render_context_usage", side_effect=record_complete),
        ):
            consumer = asyncio.create_task(repl._consume_background_events(session))
            await session.events.put(StreamTextEvent(text="automatic answer"))
            await session.events.put(TurnCompleteEvent())
            await asyncio.wait_for(completed.wait(), timeout=0.5)
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

        rendered = "\n".join(
            str(call.args[0])
            for call in print_mock.call_args_list
            if call.args
        )
        self.assertIn("Background agent continuation", rendered)
        self.assertIn("automatic answer", rendered)


if __name__ == "__main__":
    unittest.main()
