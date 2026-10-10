from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from crabcode_cli.repl import _PersistentComposer, _alt_enter_label


class TerminalOutput(DummyOutput):
    size = Size(rows=24, columns=80)

    def get_size(self):
        return self.size


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def composer(self, *, busy=False):
        output = TerminalOutput()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            composer = _PersistentComposer(SimpleNamespace(), [])
            composer.set_busy(busy)
            if busy:
                composer.add_guidance("previous guidance")
            composer.start()
            try:
                await self.wait_for(lambda: composer.prompt_session.app.is_running)
                yield composer, pipe, output
            finally:
                await composer.close()

    async def wait_for(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 3)

    async def test_second_ctrl_c_exits_without_waiting_for_input_consumer(self):
        async with self.composer(busy=True) as (composer, pipe, _):
            # Leave the first interrupt queued: the session consumer could be
            # stuck in interrupt/close. The second key must still force exit.
            with patch("crabcode_cli.repl._force_exit") as force_exit:
                pipe.send_text("\x03")
                await self.wait_for(lambda: composer._events.qsize() == 1)
                pipe.send_text("\x03")
                await self.wait_for(lambda: force_exit.called)
                force_exit.assert_called_once_with()
                self.assertEqual(composer._events.qsize(), 1)

    async def test_status_labels_render_as_literal_text(self):
        async with self.composer() as (composer, pipe, _):
            def visible(text):
                screen = composer.prompt_session.app.renderer._last_screen
                return screen is not None and any(
                    text in "".join(cell.char for _, cell in sorted(row.items()))
                    for row in screen.data_buffer.values()
                )

            for label in (
                "Request failed · retry or use /model <name>",
                "401 <html> & [/provider]",
                "<b>literal tags</b> &amp;",
            ):
                with self.subTest(label=label):
                    composer.set_notice(label)
                    await self.wait_for(lambda: visible("● " + label))
                    composer.set_busy(True)
                    composer.start_activity(label)
                    await self.wait_for(lambda: visible(label + "…"))
                    composer.stop_activity()

            composer.set_busy(False)
            pipe.send_text("retry\r")
            self.assertEqual(
                await asyncio.wait_for(composer.next_event(), 3), ("submit", "retry")
            )
            await self.wait_for(lambda: visible("● Ready"))

    async def test_newline_keys_do_not_submit_in_idle_or_busy_state(self):
        for busy in (False, True):
            for newline in (
                "\n", "\x1b\r",
                "\x1b[13;2u", "\x1b[13;3u",
                "\x1b[27;2;13~", "\x1b[27;3;13~",
            ):
                with self.subTest(busy=busy, newline=repr(newline)):
                    async with self.composer(busy=busy) as (composer, pipe, _):
                        pipe.send_text("first" + newline + "second")
                        await self.wait_for(
                            lambda: composer.prompt_session.default_buffer.text
                            == "first\nsecond"
                        )
                        self.assertTrue(composer._events.empty())
                        pipe.send_text("\r")
                        self.assertEqual(
                            await asyncio.wait_for(composer.next_event(), 3),
                            ("submit", "first\nsecond"),
                        )
                        self.assertEqual(composer.prompt_session.default_buffer.text, "")

    async def test_reverse_follow_up_keys_only_override_while_busy(self):
        for busy in (False, True):
            for key in ("\x13", "\x1b[13;5u", "\x1b[27;5;13~"):
                with self.subTest(busy=busy, key=repr(key)):
                    async with self.composer(busy=busy) as (composer, pipe, _):
                        pipe.send_text("follow up" + key)
                        if busy:
                            self.assertEqual(await asyncio.wait_for(composer.next_event(), 3), ("submit_opposite", "follow up"))
                        else:
                            await self.wait_for(lambda: composer.prompt_session.default_buffer.text == "follow up\n")
                            self.assertTrue(composer._events.empty())

    async def test_modified_enter_sequence_can_arrive_in_chunks(self):
        async with self.composer() as (composer, pipe, _):
            pipe.send_text("first\x1b[13;")
            await self.wait_for(
                lambda: composer.prompt_session.default_buffer.text == "first"
            )
            pipe.send_text("2usecond")
            await self.wait_for(
                lambda: composer.prompt_session.default_buffer.text == "first\nsecond"
            )
            self.assertTrue(composer._events.empty())

    async def test_empty_override_promotes_captured_queue_identity_in_either_mode(self):
        for mode in ("queue", "steer"):
            for key in ("\x13", "\x1b[13;5u", "\x1b[27;5;13~"):
                with self.subTest(mode=mode, key=repr(key)):
                    async with self.composer(busy=True) as (composer, pipe, _):
                        composer._session.settings = SimpleNamespace(follow_up_mode=mode)
                        composer.add_queued_turn("first", "q1")
                        composer.add_queued_turn("second", "q2")
                        pipe.send_text(key)
                        kind, request_id = await asyncio.wait_for(composer.next_event(), 3)
                        self.assertEqual((kind, request_id), ("steer_queued", "q1"))
                        composer.mark_queued_turn_started("q1")
                        # A delayed shortcut cannot act on the new queue head.
                        self.assertFalse(await composer.promote_queued_turn(request_id))
                        self.assertEqual(composer._queued_turns[0].request_id, "q2")
                        self.assertEqual(composer.follow_up_mode, mode)
                        self.assertEqual(composer.prompt_session.default_buffer.text, "")

    async def test_empty_override_does_not_promote_with_unsent_images(self):
        async with self.composer(busy=True) as (composer, pipe, _):
            composer.add_queued_turn("first", "q1")
            composer._pending_images.append({"data": "unsent"})
            pipe.send_text("\x13next\r")
            self.assertEqual(await asyncio.wait_for(composer.next_event(), 3), ("submit", "next"))
            self.assertTrue(composer._events.empty())
            self.assertEqual(composer._queued_turns[0].request_id, "q1")
            self.assertEqual(composer._pending_images, [{"data": "unsent"}])

    async def test_queue_preview_keeps_shortcut_visible_and_multiline_text_on_one_row(self):
        async with self.composer(busy=True) as (composer, _, _):
            composer.add_queued_turn("中文" * 60 + "\nsecond line", "q1")
            await self.wait_for(lambda: composer.prompt_session.app.renderer._last_screen is not None)
            fragments = composer._queued_text()
            preview = "".join(value for _, value in fragments)
            self.assertNotIn("\n", preview)
            self.assertIn("empty Ctrl+S steers #1 · /queue", preview)
            from prompt_toolkit.utils import get_cwidth
            self.assertLessEqual(get_cwidth(preview), 80)

    async def test_paste_preserves_literal_modified_enter_sequences(self):
        async with self.composer() as (composer, pipe, _):
            text = "literal \x1b[13;2u sequence"
            pipe.send_text("\x1b[200~" + text + "\x1b[201~")
            await self.wait_for(lambda: composer.prompt_session.default_buffer.text == text)
            self.assertTrue(composer._events.empty())

    def test_alt_label_matches_platform(self):
        for platform, label in (("darwin", "Opt+Enter"), ("linux", "Alt+Enter"),
                                ("win32", "Alt+Enter")):
            with self.subTest(platform=platform), patch("crabcode_cli.repl.sys.platform", platform):
                self.assertEqual(_alt_enter_label(), label)

    async def test_paste_grows_frame_and_submit_shrinks_it(self):
        async with self.composer() as (composer, pipe, _):
            window = composer.prompt_session.layout.current_window
            pipe.send_text("\x1b[200~first\nsecond\nthird\x1b[201~")
            await self.wait_for(
                lambda: window.render_info is not None
                and window.render_info.window_height == 3
            )
            self.assertTrue(composer._events.empty())
            self.assertEqual(window.render_info.displayed_lines, [0, 1, 2])
            screen = composer.prompt_session.app.renderer._last_screen
            rows = [
                "".join(row[x].char for x in range(80))
                for row in screen.data_buffer.values()
            ]
            for word in ("first", "second", "third"):
                row = next(row for row in rows if word in row)
                self.assertTrue(row.startswith("│"), row)
                self.assertTrue(row.endswith("│"), row)
            pipe.send_text("\r")
            self.assertEqual(
                await asyncio.wait_for(composer.next_event(), 3),
                ("submit", "first\nsecond\nthird"),
            )
            await self.wait_for(lambda: window.render_info.window_height == 1)

    async def test_soft_wrap_and_terminal_resize(self):
        async with self.composer() as (composer, pipe, output):
            window = composer.prompt_session.layout.current_window
            pipe.send_text("中文" * 30)
            await self.wait_for(
                lambda: window.render_info is not None
                and window.render_info.window_height == 2
            )
            output.size = Size(rows=24, columns=40)
            composer.prompt_session.app._on_resize()
            await self.wait_for(lambda: window.render_info.window_height == 4)
            self.assertTrue(composer._events.empty())

    async def test_enter_accepts_history_search_without_submitting(self):
        async with self.composer() as (composer, pipe, _):
            pipe.send_text("first\nsecond\r")
            self.assertEqual(
                await asyncio.wait_for(composer.next_event(), 3),
                ("submit", "first\nsecond"),
            )
            # Accepting resets the buffer; the next render reloads history.
            # Wait for that boundary before sending search keys in one batch.
            await self.wait_for(
                lambda: "first\nsecond" in composer.prompt_session.default_buffer._working_lines
            )
            pipe.send_text("\x12first\r")
            await self.wait_for(
                lambda: composer.prompt_session.default_buffer.text == "first\nsecond"
                and composer.prompt_session.layout.current_buffer
                is composer.prompt_session.default_buffer
            )
            self.assertTrue(composer._events.empty())
            pipe.send_text("\r")
            self.assertEqual(
                await asyncio.wait_for(composer.next_event(), 3),
                ("submit", "first\nsecond"),
            )

    async def test_long_input_scrolls_to_cursor_and_can_be_edited(self):
        async with self.composer(busy=True) as (composer, pipe, output):
            window = composer.prompt_session.layout.current_window
            text = "\n".join(f"line {i}" for i in range(50))
            pipe.send_text("\x1b[200~" + text + "\x1b[201~")
            await self.wait_for(
                lambda: window.render_info is not None
                and 49 in window.render_info.displayed_lines
            )
            self.assertLess(window.render_info.window_height, output.size.rows)
            self.assertTrue(composer._events.empty())
            # Ctrl+Home, then edit the first line, and redraw at the top.
            pipe.send_text("\x1b[1;5Hedited ")
            await self.wait_for(
                lambda: composer.prompt_session.default_buffer.text.startswith("edited ")
                and 0 in window.render_info.displayed_lines
            )
            self.assertTrue(composer._events.empty())


if __name__ == "__main__":
    unittest.main()
